"""
farms/field_schemas.py
Request/response contracts for the field, field-boundary and crop-zone endpoints.

All `id` fields carry `public_id` (UUID). Areas are `Quantity` objects and
geometry is GeoJSON (EPSG:4326), see farms/common.py.
"""

from datetime import date, datetime
from uuid import UUID

from ninja import Field, Schema

from .common import Name255, Quantity, geojson_out, quantity_out
from .external_refs import ExternalReferenceOut
from .models import FieldBoundary


class NamedRef(Schema):
    """A parent shown by id and name, e.g. a field's farm."""

    id: UUID = Field(alias="public_id")
    name: str


# ── Boundaries ────────────────────────────────────────────────────────────────

class BoundaryIn(Schema):
    geometry: dict
    # Defaults to today when absent.
    valid_from: date | None = None
    source: FieldBoundary.Source = FieldBoundary.Source.DRAWN


class CurrentBoundaryOut(Schema):
    """The open boundary, as embedded in a field."""

    id: UUID = Field(alias="public_id")
    geometry: dict
    area: Quantity
    valid_from: date
    valid_to: date | None = None
    source: str

    @staticmethod
    def resolve_geometry(obj) -> dict:
        return geojson_out(obj.geometry)

    @staticmethod
    def resolve_area(obj) -> dict:
        return quantity_out(obj.area, obj.area_unit)


class BoundaryOut(CurrentBoundaryOut):
    """One row of a field's boundary history."""

    created_by_id: UUID | None = None
    created_at: datetime

    @staticmethod
    def resolve_created_by_id(obj) -> UUID | None:
        return obj.created_by.public_id if obj.created_by_id else None


# ── Fields ────────────────────────────────────────────────────────────────────

class FieldIn(Schema):
    farm_id: UUID
    name: Name255
    soato: str = Field("", max_length=20)
    # Given explicitly or left null; never derived from the boundary behind
    # the user's back.
    area: Quantity | None = None
    soil_type: str = Field("", max_length=100)
    notes: str = ""
    # GeoJSON Polygon/MultiPolygon: creates the first boundary (valid from today).
    boundary: dict | None = None


class FieldUpdateIn(Schema):
    name: Name255 | None = None
    soato: str | None = Field(None, max_length=20)
    area: Quantity | None = None
    soil_type: str | None = Field(None, max_length=100)
    notes: str | None = None


class FieldOut(Schema):
    id: UUID = Field(alias="public_id")
    farm: NamedRef
    name: str
    soato: str
    area: Quantity | None = None
    soil_type: str
    notes: str
    current_boundary: CurrentBoundaryOut | None = None
    external_references: list[ExternalReferenceOut] = []

    @staticmethod
    def resolve_area(obj) -> dict | None:
        return quantity_out(obj.area, obj.area_unit)

    @staticmethod
    def resolve_current_boundary(obj):
        # Loaded by `org_fields` into `open_boundaries` (at most one row).
        opened = getattr(obj, "open_boundaries", [])
        return opened[0] if opened else None


# ── Crop zones ────────────────────────────────────────────────────────────────

class CropZoneIn(Schema):
    field_id: UUID
    season_id: UUID
    crop_code: str = Field(..., min_length=1, max_length=10)
    crop_name: str = Field(..., min_length=1, max_length=255)
    geometry: dict | None = None
    area: Quantity | None = None
    planting_date: date | None = None
    expected_harvest_date: date | None = None
    harvest_date: date | None = None
    notes: str = ""


class CropZoneUpdateIn(Schema):
    field_id: UUID | None = None
    season_id: UUID | None = None
    crop_code: str | None = Field(None, min_length=1, max_length=10)
    crop_name: str | None = Field(None, min_length=1, max_length=255)
    geometry: dict | None = None
    area: Quantity | None = None
    planting_date: date | None = None
    expected_harvest_date: date | None = None
    harvest_date: date | None = None
    notes: str | None = None


class CropZoneOut(Schema):
    id: UUID = Field(alias="public_id")
    field: NamedRef
    farm: NamedRef
    season: NamedRef
    crop_code: str
    crop_name: str
    geometry: dict | None = None
    area: Quantity | None = None
    planting_date: date | None = None
    expected_harvest_date: date | None = None
    harvest_date: date | None = None
    notes: str
    external_references: list[ExternalReferenceOut] = []

    @staticmethod
    def resolve_farm(obj):
        return obj.field.farm

    @staticmethod
    def resolve_geometry(obj) -> dict | None:
        return geojson_out(obj.geometry)

    @staticmethod
    def resolve_area(obj) -> dict | None:
        return quantity_out(obj.area, obj.area_unit)
