"""
farms/farm_api.py
Farm clusters, farms and seasons.

Same shape as equipment/views.py: every endpoint is async, and each write keeps
its transactional body in a sync `_apply_*` function under `sync_to_async`, so
the row and its ActivityLog entry commit together. Another organization's id,
or a cluster of theirs named in a farm body, is a 404. Name clashes and the
composite foreign keys surface as `IntegrityError` and are answered 409/400.
"""

from uuid import UUID

from asgiref.sync import sync_to_async
from django.contrib.gis.gdal import GDALException
from django.db import IntegrityError, transaction
from django.db.models import Count, ProtectedError, Q, RestrictedError, Sum
from django.shortcuts import aget_object_or_404, get_object_or_404
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from core.audit import diff, log_activity, snapshot
from core.models import ActivityLog
from core.schemas import ActivityLogOut
from core.views import org_activity
from users.models import Organization, Region
from users.permissions import caller_organization

from .common import geometry_from_geojson, writable_organization
from .external_refs import add_reference_routes, attach_references, references_of
from .farm_schemas import (
    ClusterIn,
    ClusterOut,
    ClusterUpdateIn,
    FarmIn,
    FarmOut,
    FarmUpdateIn,
    SeasonIn,
    SeasonOut,
    SeasonUpdateIn,
)
from .models import Farm, FarmCluster, Season
from .units import HECTARE, SQUARE_METRE

clusters_router = Router(tags=["Farm clusters"])
farms_router = Router(tags=["Farms"])
seasons_router = Router(tags=["Seasons"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _integrity_error(error: IntegrityError, duplicate: str) -> HttpError:
    """A unique-constraint hit is a 409 with `duplicate`; any other is a 400."""
    if "unique" in str(error).lower():
        return HttpError(409, duplicate)
    return HttpError(400, "The request violates a data constraint")


# ── clusters ──────────────────────────────────────────────────────────────────

CLUSTER_AUDIT_FIELDS = ["name", "description"]


def org_clusters(organization: Organization):
    return FarmCluster.objects.filter(organization=organization).annotate(
        farm_count=Count("farms")
    )


@clusters_router.get("", response=list[ClusterOut])
@paginate(LimitOffsetPagination)
async def list_clusters(request, search: str | None = None):
    """Farm clusters of the caller's organization. `search` matches on name."""
    qs = org_clusters(caller_organization(request))
    if search:
        qs = qs.filter(name__icontains=search)
    return qs


def _apply_create_cluster(request, organization, data: ClusterIn) -> FarmCluster:
    try:
        with transaction.atomic():
            cluster = FarmCluster.objects.create(
                organization=organization,
                name=data.name.strip(),
                description=data.description,
            )
            log_activity(
                request,
                organization,
                ActivityLog.Action.CREATED,
                cluster,
                changes=diff({}, snapshot(cluster, CLUSTER_AUDIT_FIELDS)),
            )
    except IntegrityError as error:
        raise _integrity_error(error, "A cluster with this name already exists")
    return org_clusters(organization).get(pk=cluster.pk)


@clusters_router.post("", response={201: ClusterOut})
async def create_cluster(request, data: ClusterIn):
    organization = writable_organization(request)
    cluster = await sync_to_async(_apply_create_cluster)(request, organization, data)
    return 201, cluster


@clusters_router.get("/{cluster_id}", response=ClusterOut)
async def get_cluster(request, cluster_id: UUID):
    return await aget_object_or_404(
        org_clusters(caller_organization(request)), public_id=cluster_id
    )


def _apply_update_cluster(request, organization, cluster, data: ClusterUpdateIn):
    fields = {
        name: value
        for name, value in data.dict(exclude_unset=True).items()
        if value is not None
    }
    if "name" in fields:
        fields["name"] = fields["name"].strip()
    before = snapshot(cluster, CLUSTER_AUDIT_FIELDS)
    for name, value in fields.items():
        setattr(cluster, name, value)
    changes = diff(before, snapshot(cluster, CLUSTER_AUDIT_FIELDS))
    if not changes:
        return cluster

    try:
        with transaction.atomic():
            cluster.save()
            log_activity(
                request, organization, ActivityLog.Action.UPDATED, cluster, changes=changes
            )
    except IntegrityError as error:
        raise _integrity_error(error, "A cluster with this name already exists")
    return org_clusters(organization).get(pk=cluster.pk)


@clusters_router.patch("/{cluster_id}", response=ClusterOut)
async def update_cluster(request, cluster_id: UUID, data: ClusterUpdateIn):
    organization = writable_organization(request)
    cluster = await aget_object_or_404(
        org_clusters(organization), public_id=cluster_id
    )
    return await sync_to_async(_apply_update_cluster)(
        request, organization, cluster, data
    )


def _apply_delete_cluster(request, organization, cluster) -> None:
    with transaction.atomic():
        # A composite FK cannot SET NULL just one of its two columns, so the
        # farms are detached explicitly before the cluster goes.
        detached = Farm.objects.filter(
            organization=organization, cluster=cluster
        ).update(cluster=None)
        changes = diff(snapshot(cluster, CLUSTER_AUDIT_FIELDS), {})
        if detached:
            changes["farms_detached"] = {"from": detached, "to": 0}
        log_activity(
            request, organization, ActivityLog.Action.DELETED, cluster, changes=changes
        )
        cluster.delete()


@clusters_router.delete("/{cluster_id}", response={204: None})
async def delete_cluster(request, cluster_id: UUID):
    """Remove a cluster. Its farms stay, with no cluster."""
    organization = writable_organization(request)
    cluster = await aget_object_or_404(
        org_clusters(organization), public_id=cluster_id
    )
    await sync_to_async(_apply_delete_cluster)(request, organization, cluster)
    return 204, None


@clusters_router.get("/{cluster_id}/activity", response=list[ActivityLogOut])
@paginate(LimitOffsetPagination)
async def cluster_activity(request, cluster_id: UUID):
    organization = caller_organization(request)
    cluster = await aget_object_or_404(
        org_clusters(organization), public_id=cluster_id
    )
    return org_activity(organization).filter(
        content_type__model="farmcluster", object_id=cluster.pk
    )


# ── farms ─────────────────────────────────────────────────────────────────────

FARM_AUDIT_FIELDS = ["name", "address", "notes"]


def farm_snapshot(farm: Farm) -> dict:
    """Audit-friendly view of a farm: readable cluster, region and location."""
    values = snapshot(farm, FARM_AUDIT_FIELDS)
    values["cluster"] = farm.cluster.name if farm.cluster_id else None
    values["region"] = farm.region.name if farm.region_id else None
    point = farm.location
    values["location"] = f"{point.y}, {point.x}" if point else None
    return values


def org_farms(organization: Organization):
    """
    The organization's farms with everything `FarmOut` serialises. The field
    count and the area sums (per unit, folded to hectares by the schema) are
    annotated; both read the same single join to `fields`.
    """
    return (
        Farm.objects.filter(organization=organization)
        .select_related("cluster", "region")
        .annotate(
            field_count=Count("fields"),
            area_har=Sum("fields__area", filter=Q(fields__area_unit=HECTARE)),
            area_m2=Sum("fields__area", filter=Q(fields__area_unit=SQUARE_METRE)),
        )
    )


class FarmPagination(LimitOffsetPagination):
    """Limit/offset, plus the external references of the page's farms."""

    async def apaginate_queryset(self, queryset, pagination, request, **params):
        page = await super().apaginate_queryset(queryset, pagination, request, **params)
        await attach_references(page["items"])
        return page


@farms_router.get("", response=list[FarmOut])
@paginate(FarmPagination)
async def list_farms(
    request,
    search: str | None = None,
    region_id: UUID | None = None,
    cluster_id: UUID | None = None,
):
    """Farms of the caller's organization. `search` matches name and address."""
    qs = org_farms(caller_organization(request))
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(address__icontains=search))
    if region_id:
        qs = qs.filter(region__public_id=region_id)
    if cluster_id:
        qs = qs.filter(cluster__public_id=cluster_id)
    return qs


def _point(data: dict | None):
    # `geometry_from_geojson` lets GDAL's own error through for a "type" it
    # does not know; that is still the client's bad GeoJSON, not a 500.
    try:
        return geometry_from_geojson(data, expect="point")
    except GDALException:
        raise HttpError(400, "Geometry is not valid GeoJSON")


def _resolve_cluster(organization, cluster_id: UUID | None):
    if cluster_id is None:
        return None
    return get_object_or_404(
        FarmCluster.objects.filter(organization=organization), public_id=cluster_id
    )


def _resolve_region(region_id: UUID | None):
    if region_id is None:
        return None
    region = Region.objects.filter(public_id=region_id).first()
    if region is None:
        raise HttpError(400, "Unknown region")
    return region


def _apply_create_farm(request, organization, data: FarmIn) -> Farm:
    cluster = _resolve_cluster(organization, data.cluster_id)
    region = _resolve_region(data.region_id)
    location = _point(data.location)

    try:
        with transaction.atomic():
            farm = Farm.objects.create(
                organization=organization,
                cluster=cluster,
                region=region,
                location=location,
                name=data.name.strip(),
                address=data.address,
                notes=data.notes,
            )
            log_activity(
                request,
                organization,
                ActivityLog.Action.CREATED,
                farm,
                changes=diff({}, farm_snapshot(farm)),
            )
    except IntegrityError as error:
        raise _integrity_error(error, "A farm with this name already exists")
    return org_farms(organization).get(pk=farm.pk)


@farms_router.post("", response={201: FarmOut})
async def create_farm(request, data: FarmIn):
    organization = writable_organization(request)
    farm = await sync_to_async(_apply_create_farm)(request, organization, data)
    await attach_references([farm])
    return 201, farm


@farms_router.get("/{farm_id}", response=FarmOut)
async def get_farm(request, farm_id: UUID):
    farm = await aget_object_or_404(
        org_farms(caller_organization(request)), public_id=farm_id
    )
    await attach_references([farm])
    return farm


def _apply_update_farm(request, organization, farm: Farm, data: FarmUpdateIn) -> Farm:
    fields = data.dict(exclude_unset=True)
    # name/address/notes cannot be nulled; for them null means "not sent".
    for name in ("name", "address", "notes"):
        if name in fields and fields[name] is None:
            del fields[name]
    if not fields:
        return farm

    before = farm_snapshot(farm)

    if "cluster_id" in fields:
        farm.cluster = _resolve_cluster(organization, fields.pop("cluster_id"))
    if "region_id" in fields:
        farm.region = _resolve_region(fields.pop("region_id"))
    if "location" in fields:
        farm.location = _point(fields.pop("location"))
    if "name" in fields:
        fields["name"] = fields["name"].strip()
    for name, value in fields.items():
        setattr(farm, name, value)

    changes = diff(before, farm_snapshot(farm))
    if not changes:
        return farm

    try:
        with transaction.atomic():
            farm.save()
            log_activity(
                request, organization, ActivityLog.Action.UPDATED, farm, changes=changes
            )
    except IntegrityError as error:
        raise _integrity_error(error, "A farm with this name already exists")
    return org_farms(organization).get(pk=farm.pk)


@farms_router.patch("/{farm_id}", response=FarmOut)
async def update_farm(request, farm_id: UUID, data: FarmUpdateIn):
    """Partial update: only the keys present in the body are applied."""
    organization = writable_organization(request)
    farm = await aget_object_or_404(org_farms(organization), public_id=farm_id)
    farm = await sync_to_async(_apply_update_farm)(request, organization, farm, data)
    await attach_references([farm])
    return farm


def _apply_delete_farm(request, organization, farm: Farm) -> None:
    if farm.fields.exists():
        raise HttpError(409, "This farm still has fields; remove its fields first")
    try:
        with transaction.atomic():
            log_activity(
                request,
                organization,
                ActivityLog.Action.DELETED,
                farm,
                changes=diff(farm_snapshot(farm), {}),
            )
            # References are generic, so no cascade reaches them.
            references_of(farm).delete()
            farm.delete()
    except (ProtectedError, RestrictedError):
        raise HttpError(409, "This farm is still referenced by other records")


@farms_router.delete("/{farm_id}", response={204: None})
async def delete_farm(request, farm_id: UUID):
    """Remove a farm. A farm that still has fields must be emptied first."""
    organization = writable_organization(request)
    farm = await aget_object_or_404(org_farms(organization), public_id=farm_id)
    await sync_to_async(_apply_delete_farm)(request, organization, farm)
    return 204, None


@farms_router.get("/{farm_id}/activity", response=list[ActivityLogOut])
@paginate(LimitOffsetPagination)
async def farm_activity(request, farm_id: UUID):
    organization = caller_organization(request)
    farm = await aget_object_or_404(org_farms(organization), public_id=farm_id)
    return org_activity(organization).filter(
        content_type__model="farm", object_id=farm.pk
    )


add_reference_routes(farms_router, "farm", org_farms)


# ── seasons ───────────────────────────────────────────────────────────────────

SEASON_AUDIT_FIELDS = ["name", "start_date", "end_date"]


def org_seasons(organization: Organization):
    return Season.objects.filter(organization=organization).annotate(
        crop_zone_count=Count("crop_zones")
    )


@seasons_router.get("", response=list[SeasonOut])
@paginate(LimitOffsetPagination)
async def list_seasons(request, search: str | None = None):
    """Seasons of the caller's organization, newest first."""
    qs = org_seasons(caller_organization(request))
    if search:
        qs = qs.filter(name__icontains=search)
    return qs


def _check_season_dates(start_date, end_date) -> None:
    if end_date < start_date:
        raise HttpError(400, "end_date cannot be before start_date")


def _apply_create_season(request, organization, data: SeasonIn) -> Season:
    _check_season_dates(data.start_date, data.end_date)
    try:
        with transaction.atomic():
            season = Season.objects.create(
                organization=organization,
                name=data.name.strip(),
                start_date=data.start_date,
                end_date=data.end_date,
            )
            log_activity(
                request,
                organization,
                ActivityLog.Action.CREATED,
                season,
                changes=diff({}, snapshot(season, SEASON_AUDIT_FIELDS)),
            )
    except IntegrityError as error:
        raise _integrity_error(error, "A season with this name already exists")
    return org_seasons(organization).get(pk=season.pk)


@seasons_router.post("", response={201: SeasonOut})
async def create_season(request, data: SeasonIn):
    organization = writable_organization(request)
    season = await sync_to_async(_apply_create_season)(request, organization, data)
    return 201, season


@seasons_router.get("/{season_id}", response=SeasonOut)
async def get_season(request, season_id: UUID):
    return await aget_object_or_404(
        org_seasons(caller_organization(request)), public_id=season_id
    )


def _apply_update_season(request, organization, season, data: SeasonUpdateIn):
    fields = {
        name: value
        for name, value in data.dict(exclude_unset=True).items()
        if value is not None
    }
    if "name" in fields:
        fields["name"] = fields["name"].strip()
    before = snapshot(season, SEASON_AUDIT_FIELDS)
    for name, value in fields.items():
        setattr(season, name, value)
    changes = diff(before, snapshot(season, SEASON_AUDIT_FIELDS))
    if not changes:
        return season
    _check_season_dates(season.start_date, season.end_date)

    try:
        with transaction.atomic():
            season.save()
            log_activity(
                request, organization, ActivityLog.Action.UPDATED, season, changes=changes
            )
    except IntegrityError as error:
        raise _integrity_error(error, "A season with this name already exists")
    return org_seasons(organization).get(pk=season.pk)


@seasons_router.patch("/{season_id}", response=SeasonOut)
async def update_season(request, season_id: UUID, data: SeasonUpdateIn):
    organization = writable_organization(request)
    season = await aget_object_or_404(org_seasons(organization), public_id=season_id)
    return await sync_to_async(_apply_update_season)(
        request, organization, season, data
    )


def _apply_delete_season(request, organization, season) -> None:
    try:
        with transaction.atomic():
            log_activity(
                request,
                organization,
                ActivityLog.Action.DELETED,
                season,
                changes=diff(snapshot(season, SEASON_AUDIT_FIELDS), {}),
            )
            season.delete()
    except (RestrictedError, ProtectedError):
        raise HttpError(
            409, "This season still has crop zones; remove them first"
        )


@seasons_router.delete("/{season_id}", response={204: None})
async def delete_season(request, season_id: UUID):
    organization = writable_organization(request)
    season = await aget_object_or_404(org_seasons(organization), public_id=season_id)
    await sync_to_async(_apply_delete_season)(request, organization, season)
    return 204, None
