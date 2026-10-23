"""
farms/common.py
Helpers shared by the farms endpoints: the write guard, the `Quantity` schema
and the GeoJSON <-> geometry translation.

Quantities travel as `{"value": "12.5000", "unit": "har"}` and geometry as
GeoJSON (EPSG:4326), never as bare numbers or raw WKT: the model stays ours,
and the ADAPT-shaped edge is where standards-shaped data is read and written.
"""

import json
from decimal import Decimal

from django.contrib.gis.gdal import GDALException
from django.contrib.gis.geos import GEOSException, GEOSGeometry, MultiPolygon, Point, Polygon
from ninja import Schema
from ninja.errors import HttpError
from pydantic import field_validator

from users.models import OrgPermission, Organization
from users.permissions import caller_organization, require_perm

from .units import AREA_UNITS, HECTARE


def writable_organization(request) -> Organization:
    """
    The caller's organization, for an endpoint that is about to change it.

    Reading farms is open to every member; changing them needs `farms.manage`,
    which the org owner holds implicitly.
    """
    organization = caller_organization(request)
    require_perm(request, OrgPermission.MANAGE_FARMS)
    return organization


class Quantity(Schema):
    """A value with its UCUM unit code."""

    value: Decimal
    unit: str = HECTARE

    @field_validator("unit")
    @classmethod
    def _known_unit(cls, unit: str) -> str:
        if unit not in AREA_UNITS:
            raise ValueError(f"Unknown unit {unit!r}; use one of {sorted(AREA_UNITS)}.")
        return unit

    @field_validator("value")
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("An area must be positive.")
        return value


def quantity_out(value, unit) -> dict | None:
    """`{"value", "unit"}` for a stored pair, or None when nothing is stored."""
    if value is None:
        return None
    return {"value": value, "unit": unit}


def geometry_from_geojson(data: dict | None, *, expect: str = "polygon"):
    """
    Parse a GeoJSON mapping into a GEOS geometry, or raise a 400.

    `expect="polygon"` accepts a Polygon or MultiPolygon and always returns a
    MultiPolygon, which is what the columns hold; `expect="point"` accepts a
    Point. Invalid, empty or out-of-range geometry is a readable 400, not a 500.
    """
    if data is None:
        return None
    try:
        geometry = GEOSGeometry(json.dumps(data), srid=4326)
    except (GEOSException, GDALException, ValueError, TypeError):
        raise HttpError(400, "Geometry is not valid GeoJSON")

    if expect == "point":
        if not isinstance(geometry, Point):
            raise HttpError(400, "Expected a GeoJSON Point")
    else:
        if isinstance(geometry, Polygon):
            geometry = MultiPolygon(geometry, srid=4326)
        if not isinstance(geometry, MultiPolygon):
            raise HttpError(400, "Expected a GeoJSON Polygon or MultiPolygon")

    if geometry.empty:
        raise HttpError(400, "Geometry is empty")
    if not geometry.valid:
        raise HttpError(400, f"Geometry is not valid: {geometry.valid_reason}")
    xmin, ymin, xmax, ymax = geometry.extent
    if not (-180 <= xmin <= xmax <= 180 and -90 <= ymin <= ymax <= 90):
        raise HttpError(400, "Coordinates must be longitude/latitude (EPSG:4326)")
    return geometry


def geojson_out(geometry) -> dict | None:
    """A GEOS geometry as a GeoJSON mapping, or None."""
    if geometry is None:
        return None
    return json.loads(geometry.geojson)
