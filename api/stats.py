"""
api/stats.py
Platform numbers (§7): the public landing page's, and an organization's own.

Two endpoints that share nothing but a prefix, and the split is deliberate:

  * `GET /stats/landing` 🌐 is anonymous, identical for everybody, and cached
    for five minutes — it touches most tables in the schema and none of its
    numbers are worth a fresh dozen aggregates per visitor.
  * `GET /stats/owner` is one organization's dashboard, resolved from the
    token (§0.1) and **never** cached: `unread_inquiries` is a badge someone
    is watching, and a five-minute-old badge is a bug report.

Neither is behind a permission code. Both are reads, and §0.2 gates only
writes — every member of an organization may see how it is doing.

This module supersedes `_compute_public_stats` in `api/public.py`. The two
differ in exactly one thing, which is the whole reason §7 marked the old one
🟠: the numbers that say "listings" are counted off `Listing` here, and off
available `Asset` rows there. `/public/stats` keeps answering as it always has
so the deployed frontend is not broken by this branch.
"""

from datetime import timedelta

from asgiref.sync import sync_to_async
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db.models import (
    Avg,
    Count,
    DurationField,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    Subquery,
)
from django.utils import timezone
from ninja import Router

from core.models import ActivityLog
from equipment.models import Asset, Booking, BookingStatusHistory, EquipmentCategory
from inquiries.models import Inquiry
from listings.models import Listing, ListingView, active_listing_q, active_listings
from users.models import Organization, Region, User
from users.permissions import caller_organization

from .stats_schemas import LandingStatsOut, OwnerStatsOut

stats_router = Router(tags=["Stats"])

LANDING_CACHE_KEY = "stats:landing"
LANDING_CACHE_SECONDS = 300

# Windows are inclusive of today, so "last 7 days" is today and the six days
# before it rather than eight partial days.
WEEK = 6
MONTH = 29


# ── the landing page ─────────────────────────────────────────────────────────

@stats_router.get("/landing", response=LandingStatsOut, auth=None)
async def landing_stats(request):
    """
    Headline numbers for the marketing site.

    Spelled out rather than `cache.aget_or_set`, because the default that
    takes is a *sync* callable and `_compute_landing_stats` runs a dozen ORM
    aggregates — it has to go through `sync_to_async` to be legal here.
    """
    stats = await cache.aget(LANDING_CACHE_KEY)
    if stats is None:
        stats = await sync_to_async(_compute_landing_stats)()
        await cache.aset(LANDING_CACHE_KEY, stats, LANDING_CACHE_SECONDS)
    return stats


def _compute_landing_stats() -> dict:
    now = timezone.now()
    on_market = active_listings()

    listings_by_region = list(
        Region.objects.annotate(
            listing_count=Count(
                "listings", filter=active_listing_q("listings__"), distinct=True
            )
        )
        .order_by("name")
        .values("public_id", "name", "code", "slug", "listing_count")
    )

    listings_by_category = list(
        EquipmentCategory.objects.annotate(
            listing_count=Count(
                "equipment_models__assets__listings",
                filter=active_listing_q("equipment_models__assets__listings__"),
                distinct=True,
            )
        )
        .order_by("name")
        .values("public_id", "name", "slug", "listing_count")
    )

    completed_bookings_by_region = list(
        Region.objects.annotate(
            completed_orders=Count(
                "organizations__bookings_as_provider",
                filter=Q(
                    organizations__bookings_as_provider__status=Booking.Status.COMPLETED
                ),
                distinct=True,
            )
        )
        .order_by("name")
        .values("public_id", "name", "code", "completed_orders")
    )

    return {
        "total_active_listings": on_market.count(),
        # Asset-rooted on purpose: the fleet behind the market, not the market.
        "total_equipment": Asset.objects.count(),
        "total_owner_organizations": Asset.objects.values("organization_id")
        .distinct()
        .count(),
        "total_users": User.objects.filter(is_active=True).count(),
        # Listings with no region are counted nowhere, here or in
        # `listings_by_region` — `Listing.region` is nullable and the map has
        # nothing to draw for them.
        "regions_with_listings": on_market.exclude(region__isnull=True)
        .values("region_id")
        .distinct()
        .count(),
        "listings_by_region": listings_by_region,
        "listings_by_category": listings_by_category,
        "completed_bookings": Booking.objects.filter(
            status=Booking.Status.COMPLETED
        ).count(),
        "completed_bookings_by_region": completed_bookings_by_region,
        "verified_owners": Organization.objects.filter(is_verified=True).count(),
        "average_rating": None,
        # Counted on `published_at`, not `created_at`: a listing is new to the
        # public when it reaches the public. A draft written three weeks ago
        # and activated this morning is new; one drafted this morning and
        # never activated is not, and nobody outside the org could have seen
        # it to disagree.
        "new_listings_last_7_days": Listing.objects.filter(
            published_at__gte=now - timedelta(days=7)
        ).count(),
        "new_listings_last_30_days": Listing.objects.filter(
            published_at__gte=now - timedelta(days=30)
        ).count(),
        "average_owner_response_minutes": average_owner_response_minutes(),
    }


def average_owner_response_minutes() -> float | None:
    """
    Mean time between a booking entering 'requested' and the provider moving
    it to 'confirmed' or 'rejected', read off the status history Booking
    already writes on every transition (see Booking.transition_to). A booking
    can only pass through 'requested' once — REQUESTED never re-appears as a
    target in ALLOWED_TRANSITIONS — so there's at most one match per side.

    Public rather than private, and here rather than in `api/public.py`, so
    the frozen endpoint and this one share the single implementation: it is
    the one number on the landing payload that §7 did not ask to change, and
    two copies of it would be free to disagree.
    """
    requested_at = (
        BookingStatusHistory.objects.filter(
            booking=OuterRef("booking"), to_status=Booking.Status.REQUESTED
        )
        .order_by("changed_at")
        .values("changed_at")[:1]
    )
    responses = (
        BookingStatusHistory.objects.filter(
            from_status=Booking.Status.REQUESTED,
            to_status__in=[Booking.Status.CONFIRMED, Booking.Status.REJECTED],
        )
        .annotate(requested_at=Subquery(requested_at))
        .exclude(requested_at__isnull=True)
        .annotate(
            wait=ExpressionWrapper(
                F("changed_at") - F("requested_at"), output_field=DurationField()
            )
        )
    )
    average_wait = responses.aggregate(value=Avg("wait"))["value"]
    if average_wait is None:
        return None
    return round(average_wait.total_seconds() / 60, 1)


# ── the owner dashboard ──────────────────────────────────────────────────────

@stats_router.get("/owner", response=OwnerStatsOut)
async def owner_stats(request):
    """
    How the caller's own organization is doing.

    Org resolved from the token and nowhere else (§0.1), so there is no id to
    tamper with and no cross-org read to defend against — an account without
    an organization gets the 403 `caller_organization` raises.
    """
    organization = caller_organization(request)
    return await sync_to_async(_compute_owner_stats)(organization)


def _compute_owner_stats(organization: Organization) -> dict:
    today = timezone.localdate()

    counts_by_status = dict(
        Listing.objects.filter(organization=organization)
        .values_list("status")
        .annotate(total=Count("pk"))
    )
    listings_by_status = {
        status: counts_by_status.get(status, 0) for status in Listing.Status.values
    }

    received = Inquiry.objects.filter(provider_organization=organization)
    views = ListingView.objects.filter(organization=organization)

    return {
        "active_listings": listings_by_status[Listing.Status.ACTIVE],
        "listings_on_market": active_listings()
        .filter(organization=organization)
        .count(),
        "listings_by_status": listings_by_status,
        # The partial `inquiry_unread_idx` exists for exactly this count.
        "unread_inquiries": received.filter(read_at__isnull=True).count(),
        "inquiries_received": received.count(),
        "inquiries_sent": Inquiry.objects.filter(
            customer_organization=organization
        ).count(),
        "views": {
            "total": views.count(),
            "last_7_days": views.filter(
                viewed_on__gte=today - timedelta(days=WEEK)
            ).count(),
            "last_30_days": views.filter(
                viewed_on__gte=today - timedelta(days=MONTH)
            ).count(),
        },
        "members": _member_activity(organization),
    }


def _member_activity(organization: Organization) -> list[dict]:
    """
    Per-member contributions, in two queries rather than two per member.

    Read off `ActivityLog` because that is where §0.3 already put the answer:
    a listing carries `created_by`, but an inquiry carries no "who dealt with
    it in *this* org" column, and the audit trail carries both without a
    migration. It also means the next such question — images uploaded, offers
    withdrawn — is a filter rather than a schema change.

    Current members only. A deactivated account's work stays in `/activity`,
    which is the full record; this is a dashboard of the team as it stands,
    and a leaver quietly padding the roster is not what an admin is reading it
    for.
    """
    members = list(
        User.objects.filter(organization=organization, is_active=True).order_by(
            "full_name", "username"
        )
    )
    if not members:
        return []

    listing_type = ContentType.objects.get_for_model(Listing)
    inquiry_type = ContentType.objects.get_for_model(Inquiry)

    tallies = {
        row["actor_id"]: row
        for row in ActivityLog.objects.filter(
            organization=organization, actor__in=members
        )
        .values("actor_id")
        .annotate(
            listings_created=Count(
                "pk",
                filter=Q(
                    content_type=listing_type, action=ActivityLog.Action.CREATED
                ),
            ),
            # An inquiry is logged against the actor's own organization (§0.3),
            # so within this org's history `status_changed` on an inquiry is
            # "we picked one up" — the only thing `mark_inquiry_read` writes —
            # while `created` on one would be "we sent it".
            inquiries_handled=Count(
                "pk",
                filter=Q(
                    content_type=inquiry_type,
                    action=ActivityLog.Action.STATUS_CHANGED,
                ),
            ),
        )
    }

    return [
        {
            "public_id": member.public_id,
            "name": member.get_full_name() or member.username,
            "listings_created": tallies.get(member.pk, {}).get("listings_created", 0),
            "inquiries_handled": tallies.get(member.pk, {}).get("inquiries_handled", 0),
        }
        for member in members
    ]
