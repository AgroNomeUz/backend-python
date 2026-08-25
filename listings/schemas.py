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


# Column widths, restated here so pydantic refuses an over-long value before
# it reaches the database. Django does not enforce `max_length` on save, so
# without these Postgres raises DataError — which is neither an IntegrityError
# nor something `_integrity_error` can translate, and the caller gets a 500
# where they should get a 422.
TITLE_MAX = 255
CURRENCY_LEN = 3
AVAILABILITY_MAX = 255
DISTRICT_MAX = 120


class ListingCreateIn(Schema):
    asset_id: UUID
    listing_type: Listing.ListingType
    title: str = Field(max_length=TITLE_MAX)
    # `ge=0` mirrors the listing_price_non_negative constraint, the way
    # equipment.schemas states its own bounds on the schema: the database is
    # the guarantee, this is the readable error.
    price: Decimal = Field(ge=0)
    price_unit: Listing.PriceUnit
    currency: str = Field(default="UZS", min_length=CURRENCY_LEN, max_length=CURRENCY_LEN)
    description: str = ""
    availability: str = Field(default="", max_length=AVAILABILITY_MAX)
    has_operator: bool = False
    has_delivery: bool = False
    # Defaults to the organization's own region when omitted.
    region_id: UUID | None = None
    district: str = Field(default="", max_length=DISTRICT_MAX)
    # Only these two: `paused` and `archived` describe a listing that was once
    # live, and nothing is served by letting one be born there. Typed as the
    # enum so an unknown value is a 422 listing the valid choices, rather than
    # reaching the view's own check.
    status: Listing.Status = Listing.Status.DRAFT


class ListingUpdateIn(Schema):
    """
    Partial update — only the keys actually present in the body are applied,
    so every field is optional and none carries a default that could
    overwrite a value the caller never mentioned.

    `None` here means "not sent", not "set to null": every column below except
    `region` is NOT NULL, so an explicitly-sent `null` would reach the database
    and fail there. `NULLABLE_UPDATE_FIELDS` records the one field for which a
    null *is* meaningful, and the view rejects a null anywhere else.

    `asset_id` is absent on purpose: re-pointing a listing at a different
    machine is a different offer, and the same-org and one-active-per-asset
    invariants are checked at creation. Archive it and make a new one.
    """

    title: str | None = Field(default=None, max_length=TITLE_MAX)
    description: str | None = None
    availability: str | None = Field(default=None, max_length=AVAILABILITY_MAX)
    price: Decimal | None = Field(default=None, ge=0)
    price_unit: Listing.PriceUnit | None = None
    currency: str | None = Field(
        default=None, min_length=CURRENCY_LEN, max_length=CURRENCY_LEN
    )
    has_operator: bool | None = None
    has_delivery: bool | None = None
    region_id: UUID | None = None
    district: str | None = Field(default=None, max_length=DISTRICT_MAX)
    status: Listing.Status | None = None


# The only key of `ListingUpdateIn` whose `null` is a value rather than an
# omission: clearing a listing's region is a legitimate edit, and `region` is
# the one nullable column among them.
NULLABLE_UPDATE_FIELDS = frozenset({"region_id"})
