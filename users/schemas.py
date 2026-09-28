"""
users/schemas.py
Request/response contracts for organization members, the caller's own
profile, and the organization profile.

All `id` fields carry `public_id` (UUID). Integer PKs never leave the backend.
"""

from datetime import datetime
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from ninja import Field, Schema
from pydantic import EmailStr

from listings.schemas import absolute_file_url

from .models import OrgPermission
from .services import organization_display_name


class MemberOut(Schema):
    """A member of the caller's organization."""

    id: UUID = Field(alias="public_id")
    username: str
    email: str
    phone: str | None = None
    full_name: str
    telegram: str
    first_name: str
    last_name: str
    is_owner: bool = Field(alias="is_organization_owner")
    # Effective, not stored: an owner reads back the full permission set.
    permissions: list[str] = Field(alias="org_permissions")
    must_change_password: bool
    is_active: bool
    date_joined: datetime


class MemberCreateIn(Schema):
    """
    Add a staff account to the caller's organization.

    Only the email is required — the employee fills in the rest after their
    first login. No password field: the server issues a one-time one.

    `phone` is optional here and becomes the member's login identifier once
    OTP auth ships; until then it is contact detail only.
    """

    email: EmailStr
    phone: str | None = None
    full_name: str = ""
    telegram: str = ""
    first_name: str = ""
    last_name: str = ""
    permissions: list[OrgPermission] = []


class MemberCreateOut(Schema):
    """
    The created member plus their one-time password.

    `temporary_password` is returned exactly once, at creation, and is never
    readable again — only a fresh reset produces a new one.
    """

    member: MemberOut
    temporary_password: str


class MemberUpdateIn(Schema):
    """
    Partial update — only the keys present in the request body are applied.

    `permissions` replaces the whole set rather than merging, so a client can
    revoke by sending the codes that remain.
    """

    phone: str | None = None
    full_name: str | None = None
    telegram: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    permissions: list[OrgPermission] | None = None
    is_active: bool | None = None


class PasswordResetOut(Schema):
    """Result of an admin-issued password reset."""

    member: MemberOut
    temporary_password: str


# ── the caller's own profile ──────────────────────────────────────────────────

class SelfUpdateIn(Schema):
    """
    Partial self-edit — what a member may change about themselves without
    holding any permission code.

    Deliberately three fields. `permissions` is granted by an admin (§2b),
    `is_active` is an admin action, and `phone` is a credential: it is the
    OTP login identifier, so it moves only through `POST /users/me/phone`,
    which proves the caller owns the new number first.

    `email` carries no such proof today — there is no mail gateway in this
    project (§0b) — and is accepted unverified, because an email logs you in
    only *with* a password. That stops being true the moment a password reset
    by email ships; this field needs a verification step before it does.
    """

    full_name: str | None = Field(None, max_length=255)
    telegram: str | None = Field(None, max_length=64)
    email: EmailStr | None = Field(None, max_length=254)


class PhoneChangeIn(Schema):
    """The number the caller wants to move to. A code is sent to it, not to
    the number they hold now — the point is to prove they own the new one."""

    phone: str


class PhoneChangeOut(Schema):
    """Seconds until a resend is allowed, so the UI can run a countdown."""

    retry_after: int


class PhoneChangeVerifyIn(Schema):
    phone: str
    code: str


# ── the caller's organization ─────────────────────────────────────────────────

class OrgRegionOut(Schema):
    """
    The organization's region, read straight off the model.

    Not `api.schemas.RegionOut`: that one is only ever built from the hand-made
    dicts in the auth responses, so its `id` needs no alias. Serialising a
    `Region` instance without one would hand out the integer PK.
    """

    id: UUID = Field(alias="public_id")
    name: str
    code: str
    # Same two additive fields `api.schemas.RegionOut` carries, so a region
    # read from `/org` matches the one nested in every auth response.
    slug: str = ""
    soato: str | None = None


class OrganizationDetailOut(Schema):
    """
    The caller's own organization — `api.schemas.OrganizationOut` plus the two
    fields only a member ever sees: how many people are in it, and when it
    was created.
    """

    id: UUID = Field(alias="public_id")
    name: str
    address: str
    region: OrgRegionOut | None = None
    tax_number: str
    phone: str
    email: str
    entity_type: str
    # Read-only here by construction: it drives the verified badge on public
    # listings and is set by staff, never by an endpoint (§2).
    is_verified: bool
    member_count: int
    created_at: datetime


class OrganizationUpdateIn(Schema):
    """
    Partial update of the organization profile. Requires `users.manage`.

    `is_verified` and `entity_type` are **absent rather than filtered**: the
    first drives the public badge and the second is what ONEID verification
    promotes (§2b, `/org/verify-legal`). A field that isn't in the schema
    can't be forgotten in a guard.
    """

    name: str | None = Field(None, max_length=255)
    address: str | None = Field(None, max_length=255)
    region_id: UUID | None = None
    tax_number: str | None = Field(None, max_length=32)
    phone: str | None = Field(None, max_length=32)
    email: EmailStr | None = Field(None, max_length=254)


# ── the public organization profile (seller-profile-proposal.md §2/§3) ───────
#
# Everything below is read-only and unauthenticated except
# `OrganizationContactsOut` (behind a token, no permission code — see
# `users/organizations.py`). Every hand-built dict a resolver below returns
# for an aliased field is keyed by the *alias* ("public_id"), not the field
# name ("id") — django-ninja's `Schema` has no `populate_by_name`, so a dict
# validates against declared aliases, unlike an ORM instance read through
# `from_attributes` (which resolves the alias by attribute name transparently).

class PublicOrganizationRegionOut(Schema):
    """Where the organization is registered — the profile's public region."""

    id: UUID = Field(alias="public_id")
    name: str
    code: str
    slug: str = ""


class OrganizationStatsOut(Schema):
    """
    `rating_avg`/`review_count` are null/0 until this org has a review — the
    same "present but null" treatment `ListingOut.rating` gets elsewhere
    until reviews exist.
    """

    active_listings: int
    completed_deals: int
    rating_avg: float | None = None
    review_count: int = 0


class OrganizationProfileOut(Schema):
    """`GET /organizations/{id}` — the public seller profile."""

    id: UUID = Field(alias="public_id")
    name: str
    entity_type: str
    is_verified: bool
    region: PublicOrganizationRegionOut | None = None
    member_since: datetime = Field(alias="created_at")
    # No such column exists on `Organization` (§6 of the proposal keeps logo
    # upload out of scope) — always null.
    logo_url: str | None = None
    stats: OrganizationStatsOut


class OrganizationContactsOut(Schema):
    """`GET /organizations/{id}/contacts` — any logged-in user, no permission code."""

    phone: str | None = None
    email: str | None = None
    address: str | None = None
    telegram: str | None = None


class DealListingBriefOut(Schema):
    """Enough of the listing to render one row of a deal-history list."""

    id: UUID = Field(alias="public_id")
    title: str
    equipment_type: str | None = None
    image_url: str | None = None


class DealCounterpartyOut(Schema):
    """
    The other organization on a deal, named the way §2's display-name rule
    requires — never the raw `Organization.name` for an `individual`.
    """

    id: UUID = Field(alias="public_id")
    display_name: str


class DealReviewBriefOut(Schema):
    id: UUID = Field(alias="public_id")
    rating: int
    comment: str
    created_at: datetime


class OrganizationDealOut(Schema):
    """One row of `GET /organizations/{id}/deals` — completed deals only."""

    id: UUID = Field(alias="public_id")
    deal_type: str
    completed_at: datetime | None = None
    listing: DealListingBriefOut | None = None
    counterparty: DealCounterpartyOut
    review: DealReviewBriefOut | None = None

    @staticmethod
    def resolve_listing(obj, context):
        """
        `None` if the listing was later deleted (`Deal.listing` is
        `SET_NULL`). Requires `listing__asset__equipment_model__category`
        select_related and `listing__images` prefetched — see
        `users/organizations.py`'s queryset.
        """
        listing = obj.listing
        if listing is None:
            return None
        category = listing.asset.equipment_model.category
        image = next((img for img in listing.images.all() if img.is_primary), None)
        return {
            "public_id": listing.public_id,
            "title": listing.title,
            "equipment_type": category.slug if category else None,
            "image_url": absolute_file_url(image, context) if image else None,
        }

    @staticmethod
    def resolve_counterparty(obj):
        org = obj.customer_organization
        return {"public_id": org.public_id, "display_name": organization_display_name(org)}

    @staticmethod
    def resolve_review(obj):
        """
        `None` until the counterparty leaves one. The reverse `OneToOneField`
        accessor raises `DoesNotExist` rather than returning `None` — even
        with `select_related("review")` joined, so this can't be a plain
        `getattr(obj, "review", None)`.
        """
        try:
            review = obj.review
        except ObjectDoesNotExist:
            return None
        return {
            "public_id": review.public_id,
            "rating": review.rating,
            "comment": review.comment,
            "created_at": review.created_at,
        }


class ReviewAuthorOut(Schema):
    id: UUID = Field(alias="public_id")
    display_name: str


class ReviewDealBriefOut(Schema):
    id: UUID = Field(alias="public_id")
    deal_type: str
    listing_title: str | None = None


class OrganizationReviewOut(Schema):
    """One row of `GET /organizations/{id}/reviews`."""

    id: UUID = Field(alias="public_id")
    rating: int
    comment: str
    created_at: datetime
    author: ReviewAuthorOut
    deal: ReviewDealBriefOut

    @staticmethod
    def resolve_author(obj):
        org = obj.author_organization
        return {"public_id": org.public_id, "display_name": organization_display_name(org)}

    @staticmethod
    def resolve_deal(obj):
        deal = obj.deal
        return {
            "public_id": deal.public_id,
            "deal_type": deal.deal_type,
            "listing_title": deal.listing.title if deal.listing_id else None,
        }
