"""
users/organizations.py
The public seller profile — seller-profile-proposal.md §2/§3/§8.

Everything here is read-only. `GET /organizations/{id}` and its `/contacts`
sibling read straight off `Organization`; `/deals` and `/reviews` read the
`deals` app's `Deal`/`Review` models, sourced from `Inquiry` rather than
`equipment.Booking` — see `deals/models.py`'s docstring for why.

An org with nothing public — no active listing, no completed deal — 404s
rather than confirming it exists at all, the same "don't confirm existence
of another org's object" posture `api_contract.md` §0.1 states for every
other org-scoped lookup in this API, extended here to "an org with nothing
public to show".

All four endpoints are unauthenticated except `/contacts`, which requires a
token but no permission code and no organization-membership check — the
same "read is free" posture `/inquiries/{id}/messages` already uses: no
`auth=` override on that one operation, so it inherits the API's default
`JWTBearer()` and nothing below it checks who the caller is. Kept as its own
endpoint, separate from the profile, so the profile itself stays
cacheable/ISR-friendly and contacts never end up in crawlable HTML.
"""

from uuid import UUID

from django.db.models import Avg, Count
from django.http import Http404
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.pagination import LimitOffsetPagination, paginate

from deals.models import Deal, Review
from listings.models import active_listings

from .models import Organization
from .schemas import (
    OrganizationContactsOut,
    OrganizationDealOut,
    OrganizationProfileOut,
    OrganizationReviewOut,
)

organizations_router = Router(tags=["Organizations"])


async def public_organization_or_404(org_id: UUID) -> Organization:
    """
    The org behind a public profile id, or 404 — for an org that doesn't
    exist *or* has nothing public to show. `region`/`owner` are joined up
    front: every endpoint here ends up needing one or the other, and an
    async response can't lazily load either.
    """
    org = await aget_object_or_404(
        Organization.objects.select_related("region", "owner"), public_id=org_id
    )
    if await active_listings().filter(organization=org).aexists():
        return org
    if await Deal.objects.filter(
        provider_organization=org, status=Deal.Status.COMPLETED
    ).aexists():
        return org
    raise Http404()


def _org_deals_queryset(org: Organization):
    """
    Everything `OrganizationDealOut` serialises, joined up front — an async
    response cannot lazily load a relation.
    """
    return (
        Deal.objects.filter(provider_organization=org, status=Deal.Status.COMPLETED)
        .select_related(
            "listing",
            "listing__asset__equipment_model__category",
            "customer_organization",
            "customer_organization__owner",
            "review",
        )
        .prefetch_related("listing__images")
        .order_by("-completed_at")
    )


def _org_reviews_queryset(org: Organization):
    return (
        Review.objects.filter(deal__provider_organization=org)
        .select_related(
            "deal",
            "deal__listing",
            "author_organization",
            "author_organization__owner",
        )
        .order_by("-created_at")
    )


@organizations_router.get("/{org_id}", response=OrganizationProfileOut, auth=None)
async def get_organization_profile(request, org_id: UUID):
    """The public seller profile behind a listing's `owner.id`."""
    org = await public_organization_or_404(org_id)

    active = await active_listings().filter(organization=org).acount()
    completed = await Deal.objects.filter(
        provider_organization=org, status=Deal.Status.COMPLETED
    ).acount()
    review_stats = await Review.objects.filter(
        deal__provider_organization=org
    ).aaggregate(rating_avg=Avg("rating"), review_count=Count("id"))

    region = None
    if org.region:
        region = {
            "public_id": org.region.public_id,
            "name": org.region.name,
            "code": org.region.code,
            "slug": org.region.slug,
        }

    return {
        "public_id": org.public_id,
        "name": org.name,
        "entity_type": org.entity_type,
        "is_verified": org.is_verified,
        "region": region,
        "created_at": org.created_at,
        "logo_url": None,
        "stats": {
            "active_listings": active,
            "completed_deals": completed,
            "rating_avg": review_stats["rating_avg"],
            "review_count": review_stats["review_count"] or 0,
        },
    }


@organizations_router.get("/{org_id}/contacts", response=OrganizationContactsOut)
async def get_organization_contacts(request, org_id: UUID):
    """
    Contact details. No `auth=` override (see module docstring) and no
    permission check — any authenticated user, regardless of organization.
    """
    org = await public_organization_or_404(org_id)
    return {
        "phone": org.phone or None,
        "email": org.email or None,
        "address": org.address or None,
        # `Organization` has no `telegram` column of its own — the owner's
        # is the closest available contact channel without a migration this
        # proposal doesn't ask for. Exact for an `individual` org (the owner
        # *is* the whole organization); a stand-in for a `legal_entity` with
        # staff, worth revisiting if the org profile ever gets its own field.
        "telegram": (org.owner.telegram or None) if org.owner_id else None,
    }


@organizations_router.get(
    "/{org_id}/deals", response=list[OrganizationDealOut], auth=None
)
@paginate(LimitOffsetPagination)
async def list_organization_deals(request, org_id: UUID):
    """Completed deals only, newest-completed first."""
    org = await public_organization_or_404(org_id)
    return _org_deals_queryset(org)


@organizations_router.get(
    "/{org_id}/reviews", response=list[OrganizationReviewOut], auth=None
)
@paginate(LimitOffsetPagination)
async def list_organization_reviews(request, org_id: UUID):
    """Reviews received, newest first."""
    org = await public_organization_or_404(org_id)
    return _org_reviews_queryset(org)
