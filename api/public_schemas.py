"""
api/public_schemas.py
Response contracts for the token-free public endpoints in api/public.py.

These are deliberately separate from equipment.schemas.AssetOut and
api.schemas.OrganizationOut: those carry serial numbers, VIN, exact GPS and
contact details that must never reach an anonymous visitor. Every schema
below is a strict allowlist of what's safe to publish.
"""

from datetime import datetime
from uuid import UUID

from ninja import Field, Schema

from equipment.schemas import EquipmentModelBriefOut


class PublicRegionOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    code: str


class PublicProviderOut(Schema):
    """The owning organization, stripped down to what's safe to show."""

    name: str
    region: PublicRegionOut | None = None
    is_verified: bool


class PublicPriceOut(Schema):
    amount: float
    currency: str
    unit: str
    includes_operator: bool
    includes_fuel: bool


class PublicListingOut(Schema):
    id: UUID = Field(alias="public_id")
    equipment_model: EquipmentModelBriefOut
    manufacture_year: int | None = None
    current_meter_hours: float
    price: PublicPriceOut | None = None
    provider: PublicProviderOut
    created_at: datetime

    @staticmethod
    def resolve_provider(obj) -> dict:
        org = obj.organization
        return {"name": org.name, "region": org.region, "is_verified": org.is_verified}

    @staticmethod
    def resolve_price(obj) -> dict | None:
        return _cheapest_price(obj)


def _cheapest_price(asset) -> dict | None:
    """
    Asset-level pricing takes precedence over the equipment model's default,
    matching PricingRule's own docstring. Model-level rules are filtered down
    to the asset's own organization first — equipment_model is shared catalog
    data, so other organizations' rules can be sitting on the same relation,
    and picking the cheapest across all of them would advertise a competing
    org's price as this listing's own. Both relations are expected to be
    prefetched (see _public_assets in api/public.py) so this doesn't hit the
    database per listing.
    """
    rules = list(asset.pricing_rules.all())
    if not rules:
        rules = [
            rule
            for rule in asset.equipment_model.pricing_rules.all()
            if rule.organization_id == asset.organization_id
        ]
    if not rules:
        return None
    cheapest = min(rules, key=lambda rule: rule.price)
    return {
        "amount": cheapest.price,
        "currency": cheapest.currency,
        "unit": cheapest.pricing_unit,
        "includes_operator": cheapest.includes_operator,
        "includes_fuel": cheapest.includes_fuel,
    }


# ── Regions ───────────────────────────────────────────────────────────────────

class PublicRegionListingsOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    code: str
    listing_count: int


# ── Platform stats ───────────────────────────────────────────────────────────

class CategoryListingsOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    slug: str
    listing_count: int


class RegionOrdersOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    code: str
    completed_orders: int


class PublicStatsOut(Schema):
    total_active_listings: int
    total_equipment: int
    total_owner_organizations: int
    total_users: int
    regions_with_listings: int
    listings_by_region: list[PublicRegionListingsOut]
    listings_by_category: list[CategoryListingsOut]
    completed_bookings: int
    completed_bookings_by_region: list[RegionOrdersOut]
    verified_owners: int
    # No review/rating model exists yet — always null until that's built.
    average_rating: float | None = None
    new_listings_last_7_days: int
    new_listings_last_30_days: int
    average_owner_response_minutes: float | None = None
