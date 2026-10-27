"""
farms/field_api.py
Field, field-boundary and crop-zone endpoints.

A field belongs to a farm; its outline lives in `FieldBoundary` rows, one per
range of dates, of which at most one is open (the current outline). A crop zone
is a crop on a field in a season. All of it is owned by the caller's
organization: another organization's ids are a 404, never a 403 (§0.1), reads
are open to every member and writes need `farms.manage`.

Every write goes through a sync `_apply_*` function under `sync_to_async`, so
the row and its ActivityLog entry commit together (see equipment/views.py for
why). Boundaries are append-only over the API: a new one closes the open one
and is audited as an `updated` on the field.
"""

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, ProtectedError, Q, RestrictedError
from django.shortcuts import aget_object_or_404, get_object_or_404
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from core.audit import diff, log_activity, snapshot
from core.models import ActivityLog
from core.schemas import ActivityLogOut
from core.views import org_activity
from users.models import OrgPermission, Organization
from users.permissions import readable_organization

from .common import describe_geometry, geometry_from_geojson, writable_organization
from .external_refs import add_reference_routes, attach_references, references_of
from .field_schemas import (
    BoundaryIn,
    BoundaryOut,
    CropZoneIn,
    CropZoneOut,
    CropZoneUpdateIn,
    FieldIn,
    FieldOut,
    FieldUpdateIn,
)
from .models import CropZone, Farm, Field, FieldBoundary, Season, geometry_area_hectares
from .units import HECTARE

class ReferencePagination(LimitOffsetPagination):
    """Limit/offset, plus the external references of the page's objects."""

    async def apaginate_queryset(self, queryset, pagination, request, **params):
        page = await super().apaginate_queryset(queryset, pagination, request, **params)
        await attach_references(page["items"])
        return page


fields_router = Router(tags=["Fields"])
crop_zones_router = Router(tags=["Crop zones"])


# ── helpers ───────────────────────────────────────────────────────────────────

FIELD_AUDIT_FIELDS = ["name", "soato", "area", "area_unit", "soil_type", "notes"]

CROP_ZONE_AUDIT_FIELDS = [
    "crop_code",
    "crop_name",
    "area",
    "area_unit",
    "planting_date",
    "expected_harvest_date",
    "harvest_date",
    "notes",
]


def field_snapshot(field: Field) -> dict:
    values = snapshot(field, FIELD_AUDIT_FIELDS)
    values["farm"] = field.farm.name
    return values


def crop_zone_snapshot(zone: CropZone) -> dict:
    values = snapshot(zone, CROP_ZONE_AUDIT_FIELDS)
    values["field"] = zone.field.name
    values["season"] = zone.season.name
    values["geometry"] = describe_geometry(zone.geometry)
    return values


def boundary_ref(boundary: FieldBoundary | None) -> dict | None:
    """Audit-friendly view of a boundary: when it took effect, and its area."""
    if boundary is None:
        return None
    return {
        "valid_from": boundary.valid_from.isoformat(),
        "area": float(boundary.area),
        "area_unit": boundary.area_unit,
    }


def integrity_error(exc: IntegrityError, conflicts: dict[str, str]) -> HttpError:
    """
    Translate a constraint violation into a response.

    `conflicts` maps a unique-constraint name to its 409 message; any other
    violation (a CHECK, a composite FK) is the caller's bad input, a 400.
    """
    text = str(exc)
    for constraint, message in conflicts.items():
        if constraint in text:
            return HttpError(409, message)
    return HttpError(400, "The values violate a constraint of this resource.")


FIELD_CONFLICTS = {
    "field_farm_name_unique": "This farm already has a field with that name.",
}


def _require_not_null(fields: dict, *names: str) -> None:
    for name in names:
        if name in fields and fields[name] is None:
            raise HttpError(400, f"{name} cannot be null")


# ── fields ────────────────────────────────────────────────────────────────────

def org_fields(organization: Organization):
    open_boundary = Prefetch(
        "boundaries",
        queryset=FieldBoundary.objects.filter(valid_to__isnull=True),
        to_attr="open_boundaries",
    )
    return (
        Field.objects.filter(organization=organization)
        .select_related("farm")
        .prefetch_related(open_boundary)
    )


def _field_or_404(organization: Organization, field_id: UUID) -> Field:
    """Sync — for use inside the `_apply_*` functions."""
    return get_object_or_404(org_fields(organization), public_id=field_id)


@fields_router.get("", response=list[FieldOut])
@paginate(ReferencePagination)
async def list_fields(
    request,
    search: str | None = None,
    farm_id: UUID | None = None,
    cluster_id: UUID | None = None,
):
    """Fields of the caller's organization. `search` matches field or farm name."""
    qs = org_fields(readable_organization(request, OrgPermission.VIEW_FARMS))
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(farm__name__icontains=search))
    if farm_id:
        qs = qs.filter(farm__public_id=farm_id)
    if cluster_id:
        qs = qs.filter(farm__cluster__public_id=cluster_id)
    return qs


def _insert_boundary(
    request, field: Field, geometry, valid_from: date, source: str
) -> FieldBoundary:
    """Create a boundary row; the caller has already closed any open one."""
    try:
        return FieldBoundary.objects.create(
            field=field,
            geometry=geometry,
            valid_from=valid_from,
            source=source,
            created_by=request.auth,
        )
    except ValidationError as exc:
        # The computed area does not fit the column.
        raise HttpError(400, "; ".join(exc.messages))


def _apply_create_field(request, organization, data: FieldIn) -> Field:
    farm = get_object_or_404(Farm, organization=organization, public_id=data.farm_id)
    geometry = geometry_from_geojson(data.boundary)

    try:
        with transaction.atomic():
            field = Field.objects.create(
                farm=farm,
                organization=organization,
                name=data.name,
                soato=data.soato,
                area=data.area.value if data.area else None,
                **({"area_unit": data.area.unit} if data.area else {}),
                soil_type=data.soil_type,
                notes=data.notes,
            )
            changes = diff({}, field_snapshot(field))
            if geometry is not None:
                boundary = _insert_boundary(
                    request, field, geometry, date.today(), FieldBoundary.Source.DRAWN
                )
                changes["boundary"] = {"from": None, "to": boundary_ref(boundary)}
            log_activity(
                request, organization, ActivityLog.Action.CREATED, field, changes=changes
            )
    except IntegrityError as exc:
        raise integrity_error(exc, FIELD_CONFLICTS)

    return _field_or_404(organization, field.public_id)


@fields_router.post("", response={201: FieldOut})
async def create_field(request, data: FieldIn):
    """
    Add a field to one of the caller's farms. An optional `boundary` (GeoJSON
    Polygon or MultiPolygon) becomes the field's first boundary.
    """
    organization = writable_organization(request)
    field = await sync_to_async(_apply_create_field)(request, organization, data)
    await attach_references([field])
    return 201, field


@fields_router.get("/{field_id}", response=FieldOut)
async def get_field(request, field_id: UUID):
    field = await aget_object_or_404(
        org_fields(readable_organization(request, OrgPermission.VIEW_FARMS)), public_id=field_id
    )
    await attach_references([field])
    return field


def _apply_update_field(request, organization, field: Field, data: FieldUpdateIn) -> Field:
    fields = data.dict(exclude_unset=True)
    _require_not_null(fields, "name", "soato", "soil_type", "notes")
    if not fields:
        return field

    before = field_snapshot(field)
    if "area" in fields:
        area = data.area
        field.area = area.value if area else None
        if area:
            field.area_unit = area.unit
        fields.pop("area")
    for name, value in fields.items():
        setattr(field, name, value)

    changes = diff(before, field_snapshot(field))
    if not changes:
        return field

    try:
        with transaction.atomic():
            field.save()
            log_activity(
                request, organization, ActivityLog.Action.UPDATED, field, changes=changes
            )
    except IntegrityError as exc:
        raise integrity_error(exc, FIELD_CONFLICTS)

    # Re-read: the response shows the stored (quantized) area.
    return _field_or_404(organization, field.public_id)


@fields_router.patch("/{field_id}", response=FieldOut)
async def update_field(request, field_id: UUID, data: FieldUpdateIn):
    """
    Partial update, only the keys present are applied. `area: null` clears the
    stored area. The boundary changes through `POST /fields/{id}/boundaries`.
    """
    organization = writable_organization(request)
    field = await aget_object_or_404(org_fields(organization), public_id=field_id)
    field = await sync_to_async(_apply_update_field)(request, organization, field, data)
    await attach_references([field])
    return field


def _apply_delete_field(request, organization, field: Field) -> None:
    if field.crop_zones.exists():
        raise HttpError(409, "This field has crop zones; remove its crop zones first.")
    try:
        with transaction.atomic():
            log_activity(
                request,
                organization,
                ActivityLog.Action.DELETED,
                field,
                changes=diff(field_snapshot(field), {}),
            )
            # References are generic, so no cascade reaches them.
            references_of(field).delete()
            field.delete()
    except (ProtectedError, RestrictedError, IntegrityError):
        raise HttpError(409, "This field is still referenced; remove its crop zones first.")


@fields_router.delete("/{field_id}", response={204: None})
async def delete_field(request, field_id: UUID):
    """Remove a field and its boundary history. Refused while it has crop zones."""
    organization = writable_organization(request)
    field = await aget_object_or_404(org_fields(organization), public_id=field_id)
    await sync_to_async(_apply_delete_field)(request, organization, field)
    return 204, None


# ── boundaries ────────────────────────────────────────────────────────────────

@fields_router.get("/{field_id}/boundaries", response=list[BoundaryOut])
@paginate(LimitOffsetPagination)
async def list_boundaries(request, field_id: UUID):
    """The field's boundary history, newest first."""
    field = await aget_object_or_404(
        org_fields(readable_organization(request, OrgPermission.VIEW_FARMS)), public_id=field_id
    )
    return FieldBoundary.objects.filter(field=field).select_related("created_by")


def _apply_add_boundary(request, organization, field: Field, data: BoundaryIn):
    geometry = geometry_from_geojson(data.geometry)
    valid_from = data.valid_from or date.today()

    try:
        with transaction.atomic():
            # Serialise concurrent boundary changes on the same field.
            Field.objects.select_for_update().get(pk=field.pk)
            current = FieldBoundary.objects.filter(
                field=field, valid_to__isnull=True
            ).first()

            if current is not None:
                if valid_from < current.valid_from:
                    raise HttpError(
                        400,
                        "valid_from is before the start of the current boundary "
                        f"({current.valid_from}).",
                    )
                # Close the open boundary the day before, or the same day when
                # both start on one date (the range check forbids valid_to <
                # valid_from).
                current.valid_to = max(
                    current.valid_from, valid_from - timedelta(days=1)
                )
                current.save(update_fields=["valid_to"])
            elif FieldBoundary.objects.filter(
                field=field, valid_to__gte=valid_from
            ).exists():
                raise HttpError(
                    400, "valid_from overlaps an earlier boundary of this field."
                )

            boundary = _insert_boundary(
                request, field, geometry, valid_from, data.source
            )
            log_activity(
                request,
                organization,
                ActivityLog.Action.UPDATED,
                field,
                changes={
                    "boundary": {
                        "from": boundary_ref(current),
                        "to": boundary_ref(boundary),
                    }
                },
            )
    except IntegrityError as exc:
        raise integrity_error(exc, {})

    return FieldBoundary.objects.select_related("created_by").get(pk=boundary.pk)


@fields_router.post("/{field_id}/boundaries", response={201: BoundaryOut})
async def add_boundary(request, field_id: UUID, data: BoundaryIn):
    """
    Give the field a new boundary. The open one is closed the day before
    `valid_from` (default today); `valid_from` earlier than the open boundary's
    start, or inside an earlier closed range, is a 400. The area is computed
    from the geometry, in hectares.
    """
    organization = writable_organization(request)
    field = await aget_object_or_404(org_fields(organization), public_id=field_id)
    boundary = await sync_to_async(_apply_add_boundary)(
        request, organization, field, data
    )
    return 201, boundary


# ── activity ──────────────────────────────────────────────────────────────────

@fields_router.get("/{field_id}/activity", response=list[ActivityLogOut])
@paginate(LimitOffsetPagination)
async def field_activity(request, field_id: UUID):
    """Everything that ever happened to one field, newest first."""
    organization = readable_organization(request, OrgPermission.VIEW_FARMS)
    field = await aget_object_or_404(org_fields(organization), public_id=field_id)
    return org_activity(organization).filter(
        content_type__model="field", object_id=field.pk
    )


# ── crop zones ────────────────────────────────────────────────────────────────

def org_crop_zones(organization: Organization):
    return CropZone.objects.filter(organization=organization).select_related(
        "field", "field__farm", "season"
    )


def _crop_code(value: str) -> str:
    code = value.strip().upper()
    if not code:
        raise HttpError(400, "crop_code cannot be empty")
    return code


# About a metre: a zone drawn up to the field's edge should not be refused for
# the last digit of a coordinate. Degrees, because the geometry is EPSG:4326.
FIELD_EDGE_TOLERANCE = 0.00001


def _check_inside_field(field: Field, geometry) -> None:
    """
    A zone's shape must lie within its field's current outline.

    A field with no boundary yet has nothing to measure against, so it passes —
    refusing would make drawing zones depend on drawing the field first.
    """
    if geometry is None:
        return
    outline = (
        FieldBoundary.objects.filter(field=field, valid_to__isnull=True)
        .values_list("geometry", flat=True)
        .first()
    )
    if outline is not None and not outline.buffer(FIELD_EDGE_TOLERANCE).covers(geometry):
        raise HttpError(400, "The crop zone's geometry must lie within its field's boundary.")


def _computed_area(geometry) -> Decimal:
    """Hectares of a zone's geometry, a 400 if it cannot be measured."""
    try:
        return geometry_area_hectares(geometry)
    except ValidationError as exc:
        raise HttpError(400, "; ".join(exc.messages))


def _check_harvest_dates(planting_date, expected_harvest_date) -> None:
    if planting_date and expected_harvest_date and expected_harvest_date < planting_date:
        raise HttpError(400, "expected_harvest_date cannot be before planting_date")


@crop_zones_router.get("", response=list[CropZoneOut])
@paginate(ReferencePagination)
async def list_crop_zones(
    request,
    field_id: UUID | None = None,
    farm_id: UUID | None = None,
    season_id: UUID | None = None,
    crop_code: str | None = None,
):
    """Crop zones of the caller's organization."""
    qs = org_crop_zones(readable_organization(request, OrgPermission.VIEW_FARMS))
    if field_id:
        qs = qs.filter(field__public_id=field_id)
    if farm_id:
        qs = qs.filter(field__farm__public_id=farm_id)
    if season_id:
        qs = qs.filter(season__public_id=season_id)
    if crop_code:
        qs = qs.filter(crop_code=crop_code.strip().upper())
    return qs


def _own_field(organization, field_id: UUID) -> Field:
    return get_object_or_404(
        Field.objects.select_related("farm"),
        organization=organization,
        public_id=field_id,
    )


def _own_season(organization, season_id: UUID) -> Season:
    return get_object_or_404(Season, organization=organization, public_id=season_id)


CROP_ZONE_CONFLICTS: dict[str, str] = {}


def _apply_create_crop_zone(request, organization, data: CropZoneIn) -> CropZone:
    field = _own_field(organization, data.field_id)
    season = _own_season(organization, data.season_id)
    _check_harvest_dates(data.planting_date, data.expected_harvest_date)
    geometry = geometry_from_geojson(data.geometry)
    _check_inside_field(field, geometry)
    if geometry is not None and data.area is not None:
        raise HttpError(
            400, "area is computed from the geometry; send one or the other."
        )

    try:
        with transaction.atomic():
            zone = CropZone.objects.create(
                organization=organization,
                field=field,
                season=season,
                crop_code=_crop_code(data.crop_code),
                crop_name=data.crop_name,
                geometry=geometry,
                **(
                    {"area": _computed_area(geometry), "area_unit": HECTARE}
                    if geometry is not None
                    else {
                        "area": data.area.value if data.area else None,
                        **({"area_unit": data.area.unit} if data.area else {}),
                    }
                ),
                planting_date=data.planting_date,
                expected_harvest_date=data.expected_harvest_date,
                harvest_date=data.harvest_date,
                notes=data.notes,
            )
            log_activity(
                request,
                organization,
                ActivityLog.Action.CREATED,
                zone,
                changes=diff({}, crop_zone_snapshot(zone)),
            )
    except IntegrityError as exc:
        raise integrity_error(exc, CROP_ZONE_CONFLICTS)

    return org_crop_zones(organization).get(pk=zone.pk)


@crop_zones_router.post("", response={201: CropZoneOut})
async def create_crop_zone(request, data: CropZoneIn):
    """Plant a crop on one of the caller's fields in one of its seasons."""
    organization = writable_organization(request)
    zone = await sync_to_async(_apply_create_crop_zone)(request, organization, data)
    await attach_references([zone])
    return 201, zone


@crop_zones_router.get("/{zone_id}", response=CropZoneOut)
async def get_crop_zone(request, zone_id: UUID):
    zone = await aget_object_or_404(
        org_crop_zones(readable_organization(request, OrgPermission.VIEW_FARMS)), public_id=zone_id
    )
    await attach_references([zone])
    return zone


def _apply_update_crop_zone(
    request, organization, zone: CropZone, data: CropZoneUpdateIn
) -> CropZone:
    fields = data.dict(exclude_unset=True)
    _require_not_null(
        fields, "field_id", "season_id", "crop_code", "crop_name", "notes"
    )
    if not fields:
        return zone

    before = crop_zone_snapshot(zone)

    field_changed = "field_id" in fields
    if field_changed:
        zone.field = _own_field(organization, fields.pop("field_id"))
    if "season_id" in fields:
        zone.season = _own_season(organization, fields.pop("season_id"))
    geometry_changed = "geometry" in fields
    if geometry_changed:
        fields.pop("geometry")
        zone.geometry = geometry_from_geojson(data.geometry)
    if "area" in fields:
        fields.pop("area")
        if zone.geometry is not None:
            raise HttpError(
                400,
                "area is computed from the geometry; clear the geometry to set it by hand.",
            )
        zone.area = data.area.value if data.area else None
        if data.area:
            zone.area_unit = data.area.unit
    if zone.geometry is not None and (geometry_changed or field_changed):
        # Re-checked when the field moves too: the old shape has to fit the
        # new field.
        _check_inside_field(zone.field, zone.geometry)
    if zone.geometry is not None and geometry_changed:
        zone.area = _computed_area(zone.geometry)
        zone.area_unit = HECTARE
    if "crop_code" in fields:
        fields["crop_code"] = _crop_code(fields["crop_code"])
    for name, value in fields.items():
        setattr(zone, name, value)

    _check_harvest_dates(zone.planting_date, zone.expected_harvest_date)

    changes = diff(before, crop_zone_snapshot(zone))
    if not changes:
        return zone

    try:
        with transaction.atomic():
            zone.save()
            log_activity(
                request, organization, ActivityLog.Action.UPDATED, zone, changes=changes
            )
    except IntegrityError as exc:
        raise integrity_error(exc, CROP_ZONE_CONFLICTS)

    return org_crop_zones(organization).get(pk=zone.pk)


@crop_zones_router.patch("/{zone_id}", response=CropZoneOut)
async def update_crop_zone(request, zone_id: UUID, data: CropZoneUpdateIn):
    """Partial update, only the keys present are applied."""
    organization = writable_organization(request)
    zone = await aget_object_or_404(org_crop_zones(organization), public_id=zone_id)
    zone = await sync_to_async(_apply_update_crop_zone)(
        request, organization, zone, data
    )
    await attach_references([zone])
    return zone


def _apply_delete_crop_zone(request, organization, zone: CropZone) -> None:
    try:
        with transaction.atomic():
            log_activity(
                request,
                organization,
                ActivityLog.Action.DELETED,
                zone,
                changes=diff(crop_zone_snapshot(zone), {}),
            )
            references_of(zone).delete()
            zone.delete()
    except (ProtectedError, RestrictedError, IntegrityError):
        raise HttpError(409, "This crop zone is still referenced and cannot be deleted.")


@crop_zones_router.delete("/{zone_id}", response={204: None})
async def delete_crop_zone(request, zone_id: UUID):
    organization = writable_organization(request)
    zone = await aget_object_or_404(org_crop_zones(organization), public_id=zone_id)
    await sync_to_async(_apply_delete_crop_zone)(request, organization, zone)
    return 204, None


@crop_zones_router.get("/{zone_id}/activity", response=list[ActivityLogOut])
@paginate(LimitOffsetPagination)
async def crop_zone_activity(request, zone_id: UUID):
    """Everything that ever happened to one crop zone, newest first."""
    organization = readable_organization(request, OrgPermission.VIEW_FARMS)
    zone = await aget_object_or_404(org_crop_zones(organization), public_id=zone_id)
    return org_activity(organization).filter(
        content_type__model="cropzone", object_id=zone.pk
    )


add_reference_routes(fields_router, "field", org_fields)
add_reference_routes(crop_zones_router, "crop_zone", org_crop_zones)
