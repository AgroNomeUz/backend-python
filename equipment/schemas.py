"""
equipment/schemas.py
Request/response contracts for the catalog and asset endpoints.

All `id` fields carry `public_id` (UUID). Integer PKs never leave the backend.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from ninja import Field, Schema

from .models import Asset, EquipmentModel

# Power bands used to derive a tractor "size class" — deliberately not stored
# on the model (see SESSIONS.md 2026-07-11).
SIZE_CLASS_BANDS = ((60, "small"), (150, "midsize"))


def size_class_for(power_kw: Decimal | None) -> str | None:
    if power_kw is None:
        return None
    for ceiling, label in SIZE_CLASS_BANDS:
        if power_kw < ceiling:
            return label
    return "large"


# ── Catalog ───────────────────────────────────────────────────────────────────

class ManufacturerOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    country: str
    website: str


class CategoryOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    slug: str
    is_self_propelled: bool
    parent_id: UUID | None = None
    parent_slug: str | None = None

    @staticmethod
    def resolve_parent_id(obj) -> UUID | None:
        return obj.parent.public_id if obj.parent_id else None

    @staticmethod
    def resolve_parent_slug(obj) -> str | None:
        return obj.parent.slug if obj.parent_id else None


class CategoryTreeOut(CategoryOut):
    """Top-level category with its children inlined — one call fills a picker."""

    subcategories: list[CategoryOut] = []


class EquipmentModelOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    full_name: str
    manufacturer: ManufacturerOut
    category: CategoryOut

    year_from: int | None = None
    year_to: int | None = None

    engine_power_kw: float | None = None
    engine_power_hp: int | None = None
    size_class: str | None = None

    weight_kg: int | None = None
    length_mm: int | None = None
    width_mm: int | None = None
    height_mm: int | None = None
    working_width_mm: int | None = None

    fuel_type: str
    is_self_propelled: bool
    hitch_category: str
    specifications: dict = {}

    @staticmethod
    def resolve_full_name(obj) -> str:
        return str(obj)

    @staticmethod
    def resolve_engine_power_hp(obj) -> int | None:
        if obj.engine_power_kw is None:
            return None
        return round(float(obj.engine_power_kw) * 1.35962)

    @staticmethod
    def resolve_size_class(obj) -> str | None:
        return size_class_for(obj.engine_power_kw)


class EquipmentModelBriefOut(Schema):
    """Compact form embedded in asset responses."""

    id: UUID = Field(alias="public_id")
    name: str
    full_name: str
    manufacturer: str = Field(alias="manufacturer.name")
    category: str = Field(alias="category.name")
    category_slug: str = Field(alias="category.slug")
    engine_power_kw: float | None = None
    is_self_propelled: bool

    @staticmethod
    def resolve_full_name(obj) -> str:
        return str(obj)


# ── Assets ────────────────────────────────────────────────────────────────────

class LocationIn(Schema):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class LocationOut(Schema):
    latitude: float
    longitude: float


class AssetIn(Schema):
    """Payload for registering a physical machine into the caller's org."""

    equipment_model_id: UUID
    serial_number: str = ""
    vin_or_pin: str = ""
    internal_inventory_number: str = ""
    registration_number: str = ""
    manufacture_year: int | None = Field(default=None, ge=1900, le=2100)
    ownership_status: Asset.OwnershipStatus = Asset.OwnershipStatus.OWNED
    operational_status: Asset.OperationalStatus = Asset.OperationalStatus.AVAILABLE
    current_meter_hours: Decimal = Field(default=Decimal("0"), ge=0)
    location: LocationIn | None = None
    notes: str = ""


class AssetUpdateIn(Schema):
    """Partial update — only the keys present in the request body are applied."""

    equipment_model_id: UUID | None = None
    serial_number: str | None = None
    vin_or_pin: str | None = None
    internal_inventory_number: str | None = None
    registration_number: str | None = None
    manufacture_year: int | None = Field(default=None, ge=1900, le=2100)
    ownership_status: Asset.OwnershipStatus | None = None
    operational_status: Asset.OperationalStatus | None = None
    current_meter_hours: Decimal | None = Field(default=None, ge=0)
    location: LocationIn | None = None
    notes: str | None = None


class AssetOut(Schema):
    id: UUID = Field(alias="public_id")
    organization_id: UUID = Field(alias="organization.public_id")
    equipment_model: EquipmentModelBriefOut

    serial_number: str
    vin_or_pin: str
    internal_inventory_number: str
    registration_number: str
    manufacture_year: int | None = None

    ownership_status: str
    operational_status: str
    is_bookable: bool

    current_meter_hours: float
    location: LocationOut | None = None

    notes: str
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def resolve_location(obj) -> dict | None:
        if obj.current_location is None:
            return None
        return {"latitude": obj.current_location.y, "longitude": obj.current_location.x}


# ── Activity log ──────────────────────────────────────────────────────────────

class ActivityLogOut(Schema):
    id: UUID = Field(alias="public_id")
    action: str
    actor_id: UUID | None = None
    actor_username: str | None = None
    target_type: str | None = None
    target_repr: str
    changes: dict = {}
    created_at: datetime

    @staticmethod
    def resolve_actor_id(obj) -> UUID | None:
        return obj.actor.public_id if obj.actor_id else None

    @staticmethod
    def resolve_actor_username(obj) -> str | None:
        return obj.actor.username if obj.actor_id else None

    @staticmethod
    def resolve_target_type(obj) -> str | None:
        return obj.content_type.model if obj.content_type_id else None
