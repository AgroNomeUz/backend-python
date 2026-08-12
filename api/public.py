"""
api/public.py
Token-free endpoints for the public marketing site: real equipment offers,
regional coverage and headline platform numbers.

`public_router` is mounted with auth=None, which overrides the API-wide
JWTBearer default (see api/views.py) — nothing here needs a bearer token, and
nothing here should ever return data that requires one. Equipment listings
are pulled through PublicListingOut, a strict allowlist that leaves out
serial numbers, VIN, exact GPS and contact details; see api/public_schemas.py.
"""

from datetime import timedelta
from uuid import UUID

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.db.models import (
    Avg,
    Count,
    DurationField,
    ExpressionWrapper,
    F,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
)
from django.shortcuts import aget_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.pagination import LimitOffsetPagination, paginate

from equipment.models import (
    Asset,
    Booking,
    BookingStatusHistory,
    EquipmentCategory,
    PricingRule,
)
from equipment.views import _category_q, _size_class_filter
from users.models import Organization, Region, User

from .public_schemas import PublicListingOut, PublicRegionListingsOut, PublicStatsOut

public_router = Router(tags=["Public"], auth=None)


# ── listings ──────────────────────────────────────────────────────────────────

def _public_assets():
    """
    Base queryset for anything the public site can show: available machines
    only, with everything a listing card needs already select/prefetched so
    the list endpoint doesn't N+1 per row.
    """
    today = timezone.localdate()
    valid_pricing = PricingRule.objects.filter(
        Q(valid_from__isnull=True) | Q(valid_from__lte=today),
        Q(valid_to__isnull=True) | Q(valid_to__gte=today),
    )
    return (
        Asset.objects.filter(operational_status=Asset.OperationalStatus.AVAILABLE)
        .select_related(
            "organization",
            "organization__region",
            "equipment_model",
            "equipment_model__manufacturer",
            "equipment_model__category",
            "equipment_model__category__parent",
        )
        .prefetch_related(
            Prefetch("pricing_rules", queryset=valid_pricing),
            Prefetch("equipment_model__pricing_rules", queryset=valid_pricing),
        )
        .order_by("-created_at")
    )


@public_router.get("/listings", response=list[PublicListingOut])
@paginate(LimitOffsetPagination)
async def list_public_listings(
    request,
    search: str | None = None,
    category: str | None = None,
    region: str | None = None,
    manufacturer_id: UUID | None = None,
    min_power_kw: float | None = None,
    max_power_kw: float | None = None,
    size_class: str | None = None,
    is_self_propelled: bool | None = None,
):
    """Equipment currently available to rent — the public listings page feed."""
    qs = _public_assets()

    if search:
        qs = qs.filter(
            Q(equipment_model__name__icontains=search)
            | Q(equipment_model__manufacturer__name__icontains=search)
        )
    if category:
        qs = qs.filter(_category_q(category, "equipment_model__"))
    if region:
        qs = qs.filter(organization__region__code=region)
    if manufacturer_id:
        qs = qs.filter(equipment_model__manufacturer__public_id=manufacturer_id)
    if min_power_kw is not None:
        qs = qs.filter(equipment_model__engine_power_kw__gte=min_power_kw)
    if max_power_kw is not None:
        qs = qs.filter(equipment_model__engine_power_kw__lte=max_power_kw)
    if size_class:
        qs = qs.filter(**_size_class_filter(size_class, "equipment_model__"))
    if is_self_propelled is not None:
        qs = qs.filter(equipment_model__is_self_propelled=is_self_propelled)

    return qs


@public_router.get("/listings/{listing_id}", response=PublicListingOut)
async def get_public_listing(request, listing_id: UUID):
    """
    A single listing. 404s once the machine is booked, retired or otherwise
    no longer available — same as it dropping out of the list feed.
    """
    return await aget_object_or_404(_public_assets(), public_id=listing_id)


# ── regions ───────────────────────────────────────────────────────────────────

def _region_listing_counts():
    return (
        Region.objects.annotate(
            listing_count=Count(
                "organizations__assets",
                filter=Q(
                    organizations__assets__operational_status=Asset.OperationalStatus.AVAILABLE
                ),
                distinct=True,
            )
        )
        .order_by("name")
        .values("public_id", "name", "code", "listing_count")
    )


@public_router.get("/regions", response=list[PublicRegionListingsOut])
async def list_public_regions(request):
    """Every region with its current public listing count, for the map/filter."""
    return [region async for region in _region_listing_counts()]


# ── platform stats ───────────────────────────────────────────────────────────

STATS_CACHE_KEY = "public:stats"
STATS_CACHE_SECONDS = 300


@public_router.get("/stats", response=PublicStatsOut)
async def public_stats(request):
    """
    Headline numbers for the landing page. The payload touches most tables in
    the schema, so it's cached for a few minutes rather than computed fresh
    on every hit.

    Spelled out rather than using `cache.aget_or_set`, because the default it
    takes is a *sync* callable: `_compute_public_stats` runs a dozen ORM
    aggregates and has to go through `sync_to_async` to be legal here.
    """
    stats = await cache.aget(STATS_CACHE_KEY)
    if stats is None:
        stats = await sync_to_async(_compute_public_stats)()
        await cache.aset(STATS_CACHE_KEY, stats, STATS_CACHE_SECONDS)
    return stats


def _compute_public_stats() -> dict:
    available = Asset.OperationalStatus.AVAILABLE
    now = timezone.now()

    listings_by_category = list(
        EquipmentCategory.objects.annotate(
            listing_count=Count(
                "equipment_models__assets",
                filter=Q(equipment_models__assets__operational_status=available),
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
        "total_active_listings": Asset.objects.filter(operational_status=available).count(),
        "total_equipment": Asset.objects.count(),
        "total_owner_organizations": Asset.objects.values("organization_id")
        .distinct()
        .count(),
        "total_users": User.objects.filter(is_active=True).count(),
        "regions_with_listings": Asset.objects.filter(operational_status=available)
        .exclude(organization__region__isnull=True)
        .values("organization__region_id")
        .distinct()
        .count(),
        "listings_by_region": list(_region_listing_counts()),
        "listings_by_category": listings_by_category,
        "completed_bookings": Booking.objects.filter(status=Booking.Status.COMPLETED).count(),
        "completed_bookings_by_region": completed_bookings_by_region,
        "verified_owners": Organization.objects.filter(is_verified=True).count(),
        "average_rating": None,
        "new_listings_last_7_days": Asset.objects.filter(
            created_at__gte=now - timedelta(days=7)
        ).count(),
        "new_listings_last_30_days": Asset.objects.filter(
            created_at__gte=now - timedelta(days=30)
        ).count(),
        "average_owner_response_minutes": _average_owner_response_minutes(),
    }


def _average_owner_response_minutes() -> float | None:
    """
    Mean time between a booking entering 'requested' and the provider moving
    it to 'confirmed' or 'rejected', read off the status history Booking
    already writes on every transition (see Booking.transition_to). A booking
    can only pass through 'requested' once — REQUESTED never re-appears as a
    target in ALLOWED_TRANSITIONS — so there's at most one match per side.
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
