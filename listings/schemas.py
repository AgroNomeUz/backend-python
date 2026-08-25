"""
listings/schemas.py
Request and response contracts for /listings.

`ListingOut` is used by both the public feed and the organization's own
views, and is safe for either: everything on it is either the offer itself
(which is published on purpose) or read *through* `asset.equipment_model`,
which is shared catalogue data. Nothing private to the asset — serial number,
VIN, meter hours, notes, GPS — has a field here. The fleet register keeps
those, behind a token, in equipment.schemas.AssetOut.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from ninja import Field, Schema

from api.public_schemas import PublicProviderOut

from .models import Listing


class ListingRegionOut(Schema):
    """
    Where the machine is offered.

    Carries `slug` and `soato` — unlike the region nested in `owner`, which is
    the organization's registered address — because this is the one the map
    and the region filter key off.
    """

    id: UUID = Field(alias="public_id")
    name: str
    code: str
    slug: str
    soato: str | None = None


class ListingImageOut(Schema):
    id: UUID = Field(alias="public_id")
    url: str
    sort_order: int
    is_primary: bool

    @staticmethod
    def resolve_url(obj, context) -> str:
        return absolute_file_url(obj, context)


def absolute_file_url(image, context) -> str:
    """
    An absolute URL for a stored image.

    Absolute rather than relative because §0b commits to that shape: when
    these move from local disk to MinIO the URL changes host, and a client
    that had been prefixing its own origin would break. Falls back to the
    stored path if there is no request in context (schema generation).
    """
    url = image.file.url
    request = (context or {}).get("request")
    return request.build_absolute_uri(url) if request else url


class ListingOut(Schema):
    """One offer, as both the public feed and /listings/my render it."""

    id: UUID = Field(alias="public_id")
    title: str
    listing_type: str
    status: str

    # Read through the asset's equipment model — never copied onto the
    # listing, so a correction to the catalogue reaches every offer (§0.5).
    equipment_type: str | None = None
    brand: str | None = None
    model: str | None = None
    year: int | None = None

    region: ListingRegionOut | None = None
    district: str
    description: str
    availability: str

    price: Decimal
    currency: str
    price_unit: str
    has_operator: bool
    has_delivery: bool

    images: list[ListingImageOut] = []
    owner: PublicProviderOut

    # No review model exists yet, so there is nowhere for this to come from.
    # Present and null rather than absent: the frontend already renders it,
    # and it starts returning numbers when /rentals lands.
    rating: float | None = None

    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def resolve_equipment_type(obj) -> str | None:
        category = obj.asset.equipment_model.category
        return category.slug if category else None

    @staticmethod
    def resolve_brand(obj) -> str | None:
        manufacturer = obj.asset.equipment_model.manufacturer
        return manufacturer.name if manufacturer else None

    @staticmethod
    def resolve_model(obj) -> str:
        return obj.asset.equipment_model.name

    @staticmethod
    def resolve_year(obj) -> int | None:
        return obj.asset.manufacture_year

    @staticmethod
    def resolve_owner(obj) -> dict:
        org = obj.organization
        return {"name": org.name, "region": org.region, "is_verified": org.is_verified}


class ListingCreateIn(Schema):
    asset_id: UUID
    listing_type: Listing.ListingType
    title: str
    price: Decimal
    price_unit: Listing.PriceUnit
    currency: str = "UZS"
    description: str = ""
    availability: str = ""
    has_operator: bool = False
    has_delivery: bool = False
    # Defaults to the organization's own region when omitted.
    region_id: UUID | None = None
    district: str = ""
    # Only these two: `paused` and `archived` describe a listing that was once
    # live, and nothing is served by letting one be born there.
    status: str = Listing.Status.DRAFT


class ListingUpdateIn(Schema):
    """
    Partial update — only the keys actually present in the body are applied,
    so every field is optional and none carries a default that could
    overwrite a value the caller never mentioned.

    `asset_id` is absent on purpose: re-pointing a listing at a different
    machine is a different offer, and the same-org and one-active-per-asset
    invariants are checked at creation. Archive it and make a new one.
    """

    title: str | None = None
    description: str | None = None
    availability: str | None = None
    price: Decimal | None = None
    price_unit: Listing.PriceUnit | None = None
    currency: str | None = None
    has_operator: bool | None = None
    has_delivery: bool | None = None
    region_id: UUID | None = None
    district: str | None = None
    status: Listing.Status | None = None
