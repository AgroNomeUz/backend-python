"""
farms/farm_schemas.py
Request and response shapes for clusters, farms and seasons.
"""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from ninja import Field, Schema

from .common import Name100, Name255, Quantity, geojson_out, quantity_out
from .external_refs import ExternalReferenceOut
from .units import HECTARE, to_hectares


class ClusterRefOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str


class RegionRefOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    code: str | None = None
    slug: str = ""
    soato: str | None = None


# ── clusters ──────────────────────────────────────────────────────────────────

class ClusterIn(Schema):
    name: Name255
    description: str = ""


class ClusterUpdateIn(Schema):
    name: Name255 | None = None
    description: str | None = None


class ClusterOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    description: str
    farm_count: int = 0
    created_at: datetime


# ── farms ─────────────────────────────────────────────────────────────────────

class FarmIn(Schema):
    name: Name255
    address: str = Field("", max_length=512)
    notes: str = ""
    cluster_id: UUID | None = None
    region_id: UUID | None = None
    # GeoJSON Point
    location: dict | None = None


class FarmUpdateIn(Schema):
    name: Name255 | None = None
    address: str | None = Field(None, max_length=512)
    notes: str | None = None
    cluster_id: UUID | None = None
    region_id: UUID | None = None
    location: dict | None = None


class FarmOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    address: str
    notes: str
    cluster: ClusterRefOut | None = None
    region: RegionRefOut | None = None
    location: dict | None = None
    field_count: int = 0
    total_area: Quantity | None = None
    external_references: list[ExternalReferenceOut] = []
    created_at: datetime

    @staticmethod
    def resolve_location(obj):
        return geojson_out(obj.location)

    @staticmethod
    def resolve_total_area(obj):
        """Sum of the farm's field areas in hectares, or None without any."""
        hectares = getattr(obj, "area_har", None)
        square_metres = getattr(obj, "area_m2", None)
        if hectares is None and square_metres is None:
            return None
        total = to_hectares(hectares or Decimal(0), HECTARE) + to_hectares(
            square_metres or Decimal(0), "m2"
        )
        return quantity_out(
            total.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), HECTARE
        )


# ── seasons ───────────────────────────────────────────────────────────────────

class SeasonIn(Schema):
    name: Name100
    start_date: date
    end_date: date


class SeasonUpdateIn(Schema):
    name: Name100 | None = None
    start_date: date | None = None
    end_date: date | None = None


class SeasonOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    start_date: date
    end_date: date
    crop_zone_count: int = 0
