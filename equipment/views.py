"""
equipment/views.py
Catalog and asset endpoints.

Two very different kinds of data live here:

  * Catalog (Manufacturer, EquipmentCategory, EquipmentModel) is shared
    reference data — read-only over the API, populated by admins, and public
    (catalog_router opts out of the API-wide JWTBearer default) since none of
    it is sensitive and both the app and the public site need it.
  * Assets are org-owned. Every asset is scoped to the caller's organization,
    and every write is mirrored into core.ActivityLog with the acting user, so
    an organization can always answer "who added / changed / removed this?".
    Assets stay behind a bearer token — see api/public.py for the filtered,
    public-safe view of "available" assets shown on the marketing site.

Objects are addressed by `public_id` (UUID); integer PKs never leave here.
"""

from uuid import UUID

from django.contrib.gis.geos import Point
from django.db import transaction
from django.db.models import Prefetch, ProtectedError, Q
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from core.audit import diff, request_context, snapshot
from core.models import ActivityLog
from users.models import OrgPermission, Organization
from users.permissions import caller_organization, require_perm

from .models import (
    Asset,
    EquipmentCategory,
    EquipmentModel,
    Manufacturer,
)
from .schemas import (
    ActivityLogOut,
    AssetIn,
    AssetOut,
    AssetUpdateIn,
    CategoryOut,
    CategoryTreeOut,
    EquipmentModelOut,
    ManufacturerOut,
    SIZE_CLASS_BANDS,
)

catalog_router = Router(tags=["Catalog"], auth=None)
assets_router = Router(tags=["Assets"])
activity_router = Router(tags=["Activity"])


# ── helpers ───────────────────────────────────────────────────────────────────

def writable_organization(request) -> Organization:
    """
    The caller's organization, for an endpoint that is about to change it.

    Reading the asset registry is open to every member; changing it needs
    `equipment.manage`, which the org owner holds implicitly.
    """
    organization = caller_organization(request)
    require_perm(request, OrgPermission.MANAGE_EQUIPMENT)
    return organization


# Fields whose changes are worth showing in an organization's history.
ASSET_AUDIT_FIELDS = [
    "equipment_model",
    "serial_number",
    "vin_or_pin",
    "internal_inventory_number",
    "registration_number",
    "manufacture_year",
    "ownership_status",
    "operational_status",
    "current_meter_hours",
    "notes",
]


def asset_snapshot(asset: Asset) -> dict:
    """Audit-friendly view of an asset, including a readable location."""
    values = snapshot(asset, ASSET_AUDIT_FIELDS)
    point = asset.current_location
    values["location"] = f"{point.y}, {point.x}" if point else None
    return values


def log_activity(request, organization, action, target, changes=None) -> None:
    ActivityLog.record(
        organization=organization,
        actor=request.auth,
        action=action,
        target=target,
        changes=changes,
        context=request_context(request),
    )


def equipment_model_or_404(model_id: UUID) -> EquipmentModel:
    return get_object_or_404(
        EquipmentModel.objects.select_related("manufacturer", "category"),
        public_id=model_id,
    )


# ── catalog: manufacturers ────────────────────────────────────────────────────

@catalog_router.get("/manufacturers", response=list[ManufacturerOut])
@paginate(LimitOffsetPagination)
def list_manufacturers(request, search: str | None = None):
    """Brands available in the catalog. `search` matches on name."""
    qs = Manufacturer.objects.all()
    if search:
        qs = qs.filter(name__icontains=search)
    return qs


# ── catalog: categories ───────────────────────────────────────────────────────

@catalog_router.get("/categories", response=list[CategoryTreeOut])
def list_categories(request):
    """
    Category tree, top-level nodes with their children inlined — one call is
    enough to render the category picker.
    """
    children = EquipmentCategory.objects.select_related("parent")
    return (
        EquipmentCategory.objects
        .filter(parent__isnull=True)
        .prefetch_related(Prefetch("subcategories", queryset=children))
    )


@catalog_router.get("/categories/{slug}", response=CategoryOut)
def get_category(request, slug: str):
    return get_object_or_404(
        EquipmentCategory.objects.select_related("parent"), slug=slug
    )


# ── catalog: equipment models ─────────────────────────────────────────────────

@catalog_router.get("/equipment-models", response=list[EquipmentModelOut])
@paginate(LimitOffsetPagination)
def list_equipment_models(
    request,
    search: str | None = None,
    manufacturer_id: UUID | None = None,
    category: str | None = None,
    is_self_propelled: bool | None = None,
    hitch_category: str | None = None,
    min_power_kw: float | None = None,
    max_power_kw: float | None = None,
    size_class: str | None = None,
    year: int | None = None,
):
    """
    The picker feed: every model a user can choose when registering a machine.

    `category` takes a slug and includes that category's subcategories, so
    passing a top-level slug ("vehicle") returns everything beneath it.
    `size_class` (small / midsize / large) is a shorthand for the engine-power
    bands in schemas.SIZE_CLASS_BANDS.
    """
    qs = EquipmentModel.objects.select_related(
        "manufacturer", "category", "category__parent"
    )

    if search:
        qs = qs.filter(
            Q(name__icontains=search) | Q(manufacturer__name__icontains=search)
        )
    if manufacturer_id:
        qs = qs.filter(manufacturer__public_id=manufacturer_id)
    if category:
        qs = qs.filter(_category_q(category))
    if is_self_propelled is not None:
        qs = qs.filter(is_self_propelled=is_self_propelled)
    if hitch_category:
        qs = qs.filter(hitch_category=hitch_category)
    if min_power_kw is not None:
        qs = qs.filter(engine_power_kw__gte=min_power_kw)
    if max_power_kw is not None:
        qs = qs.filter(engine_power_kw__lte=max_power_kw)
    if size_class:
        qs = qs.filter(**_size_class_filter(size_class))
    if year:
        qs = qs.filter(
            Q(year_from__lte=year) | Q(year_from__isnull=True),
            Q(year_to__gte=year) | Q(year_to__isnull=True),
        )

    return qs


def _size_class_filter(size_class: str, prefix: str = "") -> dict:
    """
    Translate a size-class label back into an engine_power_kw range.
    `prefix` is the relation path to EquipmentModel for querysets that don't
    start there, e.g. "equipment_model__" when filtering Assets.
    """
    bands = {}
    lower = None
    for ceiling, label in SIZE_CLASS_BANDS:
        bands[label] = (lower, ceiling)
        lower = ceiling
    bands["large"] = (lower, None)

    if size_class not in bands:
        raise HttpError(400, f"Unknown size_class '{size_class}'")

    low, high = bands[size_class]
    lookup = {}
    if low is not None:
        lookup[f"{prefix}engine_power_kw__gte"] = low
    if high is not None:
        lookup[f"{prefix}engine_power_kw__lt"] = high
    return lookup


def _category_q(category: str, prefix: str = "") -> Q:
    """
    Category slug filter that also matches everything under it. `prefix` is
    the relation path to EquipmentCategory for querysets that don't start
    there, e.g. "equipment_model__" when filtering Assets.
    """
    return Q(**{f"{prefix}category__slug": category}) | Q(
        **{f"{prefix}category__parent__slug": category}
    )


@catalog_router.get("/equipment-models/{model_id}", response=EquipmentModelOut)
def get_equipment_model(request, model_id: UUID):
    return equipment_model_or_404(model_id)


# ── assets: the caller organization's machines ────────────────────────────────

def org_assets(organization: Organization):
    return Asset.objects.filter(organization=organization).select_related(
        "organization",
        "equipment_model",
        "equipment_model__manufacturer",
        "equipment_model__category",
    )


@assets_router.get("", response=list[AssetOut])
@paginate(LimitOffsetPagination)
def list_assets(
    request,
    search: str | None = None,
    operational_status: str | None = None,
    ownership_status: str | None = None,
    category: str | None = None,
    bookable: bool | None = None,
):
    """Machines registered to the caller's organization."""
    qs = org_assets(caller_organization(request))

    if search:
        qs = qs.filter(
            Q(serial_number__icontains=search)
            | Q(vin_or_pin__icontains=search)
            | Q(internal_inventory_number__icontains=search)
            | Q(registration_number__icontains=search)
            | Q(equipment_model__name__icontains=search)
            | Q(equipment_model__manufacturer__name__icontains=search)
        )
    if operational_status:
        qs = qs.filter(operational_status=operational_status)
    if ownership_status:
        qs = qs.filter(ownership_status=ownership_status)
    if category:
        qs = qs.filter(_category_q(category, "equipment_model__"))
    if bookable is not None:
        available = Asset.OperationalStatus.AVAILABLE
        qs = (
            qs.filter(operational_status=available)
            if bookable
            else qs.exclude(operational_status=available)
        )

    return qs


@assets_router.post("", response={201: AssetOut})
def create_asset(request, data: AssetIn):
    """Register a physical machine into the caller's organization."""
    organization = writable_organization(request)
    equipment_model = equipment_model_or_404(data.equipment_model_id)

    payload = data.dict(exclude={"equipment_model_id", "location"})
    with transaction.atomic():
        asset = Asset.objects.create(
            organization=organization,
            equipment_model=equipment_model,
            current_location=_point(data.location),
            **payload,
        )
        log_activity(
            request,
            organization,
            ActivityLog.Action.CREATED,
            asset,
            changes=diff({}, asset_snapshot(asset)),
        )

    return 201, asset


@assets_router.get("/{asset_id}", response=AssetOut)
def get_asset(request, asset_id: UUID):
    return get_object_or_404(
        org_assets(caller_organization(request)), public_id=asset_id
    )


@assets_router.patch("/{asset_id}", response=AssetOut)
def update_asset(request, asset_id: UUID, data: AssetUpdateIn):
    """
    Partial update — only the keys present in the request body are applied.
    Records the field-level diff against the organization's activity log.
    """
    organization = writable_organization(request)
    asset = get_object_or_404(org_assets(organization), public_id=asset_id)

    fields = data.dict(exclude_unset=True)
    if not fields:
        return asset

    before = asset_snapshot(asset)

    if "equipment_model_id" in fields:
        asset.equipment_model = equipment_model_or_404(fields.pop("equipment_model_id"))
    if "location" in fields:
        fields.pop("location")
        asset.current_location = _point(data.location)
    for name, value in fields.items():
        setattr(asset, name, value)

    changes = diff(before, asset_snapshot(asset))
    if not changes:
        return asset

    action = (
        ActivityLog.Action.STATUS_CHANGED
        if set(changes) == {"operational_status"}
        else ActivityLog.Action.UPDATED
    )

    with transaction.atomic():
        asset.save()
        log_activity(request, organization, action, asset, changes=changes)

    return asset


@assets_router.delete("/{asset_id}", response={204: None})
def delete_asset(request, asset_id: UUID):
    """
    Remove an asset. Machines referenced by bookings or work sessions are
    protected at the DB level — retire those instead of deleting them.
    """
    organization = writable_organization(request)
    asset = get_object_or_404(org_assets(organization), public_id=asset_id)

    try:
        with transaction.atomic():
            # Log before the delete: the row must outlive its target, and the
            # generic FK needs the pk while it still exists.
            log_activity(
                request,
                organization,
                ActivityLog.Action.DELETED,
                asset,
                changes=diff(asset_snapshot(asset), {}),
            )
            asset.delete()
    except ProtectedError:
        raise HttpError(
            409,
            "This asset is referenced by bookings or work sessions. "
            "Set its operational_status to 'retired' instead of deleting it.",
        )

    return 204, None


def _point(location) -> Point | None:
    if location is None:
        return None
    return Point(location.longitude, location.latitude, srid=4326)


# ── activity log ──────────────────────────────────────────────────────────────

@assets_router.get("/{asset_id}/activity", response=list[ActivityLogOut])
@paginate(LimitOffsetPagination)
def asset_activity(request, asset_id: UUID):
    """Everything that ever happened to one asset, newest first."""
    organization = caller_organization(request)
    asset = get_object_or_404(org_assets(organization), public_id=asset_id)
    return _org_activity(organization).filter(
        content_type__model="asset", object_id=asset.pk
    )


@activity_router.get("", response=list[ActivityLogOut])
@paginate(LimitOffsetPagination)
def list_activity(
    request,
    action: str | None = None,
    target_type: str | None = None,
):
    """The organization's full change history, newest first."""
    qs = _org_activity(caller_organization(request))
    if action:
        qs = qs.filter(action=action)
    if target_type:
        qs = qs.filter(content_type__model=target_type)
    return qs


def _org_activity(organization: Organization):
    return ActivityLog.objects.filter(organization=organization).select_related(
        "actor", "content_type"
    )
