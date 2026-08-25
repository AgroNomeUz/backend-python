"""
api/regions.py
Uzbekistan's administrative regions — the map, the location filter, and the
region landing pages.

Token-free and admin-curated: `Region` is shared reference data, one of the
deliberate exceptions to "everything is organization-owned" (§0.1), because
every organization picks from the same 14 units.

Separate from api/public.py on purpose. That module is the deprecated
`/public/*` surface, frozen so the deployed frontend keeps working; this is
the canonical `/regions` the contract asks for, and it counts **listings**
where the old endpoint counts available assets.
"""

from asgiref.sync import sync_to_async
from django.db.models import Count, Q
from django.shortcuts import aget_object_or_404
from ninja import Router

from equipment.models import Asset
from listings.models import Listing
from users.models import Region

from .public_schemas import RegionDetailOut

regions_router = Router(tags=["Regions"], auth=None)

# How many categories a region's `popular_equipment` lists.
POPULAR_EQUIPMENT_LIMIT = 5


def _active_listings():
    """The same intersection the public feed uses (§0.5)."""
    return Listing.objects.filter(
        status=Listing.Status.ACTIVE,
        asset__operational_status=Asset.OperationalStatus.AVAILABLE,
    )


def _regions_with_counts(**filters) -> list[dict]:
    """
    Regions with their active-listing count and top categories.

    Two queries regardless of how many regions match, not two per region: the
    per-category breakdown is one grouped query over every region at once,
    bucketed here. A version that annotated each region separately would be
    14 queries on the list endpoint and grow with the seed.
    """
    regions = list(
        Region.objects.filter(**filters)
        .annotate(
            listing_count=Count(
                "listings",
                filter=Q(
                    listings__status=Listing.Status.ACTIVE,
                    listings__asset__operational_status=Asset.OperationalStatus.AVAILABLE,
                ),
                distinct=True,
            )
        )
        .order_by("name")
    )

    popular = _popular_equipment_by_region(
        [region.pk for region in regions]
    )
    for region in regions:
        region.popular_equipment = popular.get(region.pk, [])
    return regions


def _popular_equipment_by_region(region_pks: list[int]) -> dict[int, list[dict]]:
    """
    {region_pk: [{"type": category_slug, "count": n}, …]} — the busiest
    categories first, capped at POPULAR_EQUIPMENT_LIMIT.
    """
    rows = (
        _active_listings()
        .filter(region_id__in=region_pks)
        .exclude(asset__equipment_model__category__isnull=True)
        .values("region_id", "asset__equipment_model__category__slug")
        .annotate(count=Count("pk"))
        .order_by("region_id", "-count")
    )

    grouped: dict[int, list[dict]] = {}
    for row in rows:
        bucket = grouped.setdefault(row["region_id"], [])
        if len(bucket) < POPULAR_EQUIPMENT_LIMIT:
            bucket.append(
                {
                    "type": row["asset__equipment_model__category__slug"],
                    "count": row["count"],
                }
            )
    return grouped


@regions_router.get("", response=list[RegionDetailOut])
async def list_regions(request):
    """
    Every region, with how much equipment is currently on offer in it.

    Unpaginated — there are fourteen, and the map needs all of them at once.
    """
    return await sync_to_async(_regions_with_counts)()


@regions_router.get("/{slug}", response=RegionDetailOut)
async def get_region(request, slug: str):
    """One region, addressed by its slug — the region landing page."""
    # 404s on an unknown slug before the counting queries run.
    await aget_object_or_404(Region.objects.all(), slug=slug)
    regions = await sync_to_async(_regions_with_counts)(slug=slug)
    return regions[0]
