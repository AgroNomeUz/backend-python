"""
listings/views.py
The marketplace: the public feed, and the organization's own offers.

Three audiences, one router:

  * Anyone (no token) reads `GET /listings` and, for a published offer,
    `GET /listings/{id}`. The feed is the intersection §0.5 defines —
    `Listing.status = active` **and** `asset.operational_status = available` —
    so a machine going in for repair leaves the market without its listing
    having to pretend to be unpublished.
  * Any member of the owning organization reads `GET /listings/my` in every
    status, and `GET /listings/{id}` widens for them to drafts and paused
    offers. Reads are never permission-gated (§0.2).
  * A member holding `equipment.manage` writes. Every write resolves the org
    from the token, never from the body, and is mirrored into ActivityLog
    (§0.3).

An id belonging to another organization returns **404, not 403** (§0.1): a
403 would confirm the object exists.

Async endpoints with the transactional body in a sync `_apply_*` helper, for
the reason equipment/views.py sets out — Django has no async
`transaction.atomic()`, and a write plus its audit row have to commit
together.
"""

from uuid import UUID

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import aget_object_or_404, get_object_or_404
from django.utils import timezone
from ninja import File, Router
from ninja.errors import HttpError
from ninja.files import UploadedFile
from ninja.pagination import LimitOffsetPagination, paginate

from api.auth import OptionalJWTBearer, authenticated_user
from core.audit import diff, log_activity, snapshot
from core.models import ActivityLog
from equipment.models import Asset
from equipment.views import _category_q, _size_class_filter, writable_organization
from users.models import Organization, Region
from users.permissions import caller_organization

from .models import IMAGE_EXTENSIONS, Listing, ListingImage
from .schemas import ListingCreateIn, ListingImageOut, ListingOut, ListingUpdateIn

listings_router = Router(tags=["Listings"])

# Fields whose changes are worth showing in an organization's history.
LISTING_AUDIT_FIELDS = [
    "title",
    "description",
    "availability",
    "listing_type",
    "status",
    "price",
    "currency",
    "price_unit",
    "has_operator",
    "has_delivery",
    "district",
]

def listing_snapshot(listing: Listing) -> dict:
    """
    Audit-friendly view of a listing.

    `region` is a relation, so it can't come out of `snapshot()` with the
    plain fields; it is added by hand as a label, the way `asset_snapshot`
    handles an asset's location.
    """
    values = snapshot(listing, LISTING_AUDIT_FIELDS)
    values["region"] = str(listing.region) if listing.region else None
    return values


SORT_OPTIONS = {
    "newest": "-created_at",
    "price_asc": "price",
    "price_desc": "-price",
    # No review model exists, so there is nothing to sort on (§4). Accepted
    # rather than rejected so the frontend's sort control doesn't have to know
    # that, and mapped to the default order until /rentals lands.
    "rating": "-created_at",
}

# First bytes of each format we accept. The extension is whatever the client
# chose to call the file, so it is not evidence of anything on its own.
IMAGE_SIGNATURES = (
    b"\xff\xd8\xff",                    # JPEG
    b"\x89PNG\r\n\x1a\n",               # PNG
)


# ── querysets ─────────────────────────────────────────────────────────────────

def _listing_relations(queryset):
    """
    Everything ListingOut serialises, joined up front.

    An async view cannot lazily load a relation — it raises
    SynchronousOnlyOperation at serialisation time — so this is correctness,
    not just an N+1 guard.
    """
    return queryset.select_related(
        "organization",
        "organization__region",
        "region",
        "asset",
        "asset__equipment_model",
        "asset__equipment_model__manufacturer",
        "asset__equipment_model__category",
        "asset__equipment_model__category__parent",
    ).prefetch_related("images")


def published_listings():
    """The public feed: an active offer on an available machine (§0.5)."""
    return _listing_relations(
        Listing.objects.filter(
            status=Listing.Status.ACTIVE,
            asset__operational_status=Asset.OperationalStatus.AVAILABLE,
        )
    )


def org_listings(organization: Organization):
    """Everything one organization has ever listed, in any status."""
    return _listing_relations(Listing.objects.filter(organization=organization))


# ── public feed ───────────────────────────────────────────────────────────────

@listings_router.get("", response=list[ListingOut], auth=None)
@paginate(LimitOffsetPagination)
async def list_listings(
    request,
    search: str | None = None,
    equipment_type: str | None = None,
    category: str | None = None,
    listing_type: str | None = None,
    region: str | None = None,
    manufacturer_id: UUID | None = None,
    min_power_kw: float | None = None,
    max_power_kw: float | None = None,
    size_class: str | None = None,
    is_self_propelled: bool | None = None,
    has_operator: bool | None = None,
    has_delivery: bool | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    verified_only: bool | None = None,
    sort: str | None = None,
):
    """
    Equipment currently on the market — the public listings page feed.

    The machine-shaped filters reach through `asset__equipment_model__` into
    the shared catalogue; the offer-shaped ones (`has_operator`, price, sort)
    are plain columns on the listing, which is the whole reason §0.5 moved
    the price here.
    """
    qs = published_listings()

    if search:
        qs = qs.filter(
            Q(title__icontains=search)
            | Q(description__icontains=search)
            | Q(asset__equipment_model__name__icontains=search)
            | Q(asset__equipment_model__manufacturer__name__icontains=search)
        )
    # `category` is the name the asset endpoints use, `equipment_type` the one
    # the listings contract uses. Same filter, both spellings accepted.
    slug = equipment_type or category
    if slug:
        qs = qs.filter(_category_q(slug, "asset__equipment_model__"))
    if listing_type:
        qs = qs.filter(listing_type=listing_type)
    if region:
        # Slug or ISO code — the map sends one, the old filter chips the other.
        qs = qs.filter(Q(region__slug=region) | Q(region__code=region))
    if manufacturer_id:
        qs = qs.filter(asset__equipment_model__manufacturer__public_id=manufacturer_id)
    if min_power_kw is not None:
        qs = qs.filter(asset__equipment_model__engine_power_kw__gte=min_power_kw)
    if max_power_kw is not None:
        qs = qs.filter(asset__equipment_model__engine_power_kw__lte=max_power_kw)
    if size_class:
        qs = qs.filter(**_size_class_filter(size_class, "asset__equipment_model__"))
    if is_self_propelled is not None:
        qs = qs.filter(asset__equipment_model__is_self_propelled=is_self_propelled)
    if has_operator is not None:
        qs = qs.filter(has_operator=has_operator)
    if has_delivery is not None:
        qs = qs.filter(has_delivery=has_delivery)
    if min_price is not None:
        qs = qs.filter(price__gte=min_price)
    if max_price is not None:
        qs = qs.filter(price__lte=max_price)
    if verified_only:
        qs = qs.filter(organization__is_verified=True)

    if sort:
        if sort not in SORT_OPTIONS:
            raise HttpError(400, f"Unknown sort '{sort}'")
        qs = qs.order_by(SORT_OPTIONS[sort])

    return qs


# ── the caller organization's own listings ────────────────────────────────────
#
# Registered before /{listing_id} for readability. It would resolve correctly
# either way: the path parameter is typed as a UUID, so "my" cannot match it.

@listings_router.get("/my", response=list[ListingOut])
@paginate(LimitOffsetPagination)
async def list_my_listings(
    request,
    status: str | None = None,
    listing_type: str | None = None,
    created_by: UUID | None = None,
):
    """
    Everything the caller's organization has listed, in every status.

    Distinct from `GET /assets`, which is the fleet register: a machine can
    appear here twice (offered for rent *and* for sale) or not at all.
    `created_by` answers "what has this member published?" (§0.3).
    """
    qs = org_listings(caller_organization(request))
    if status:
        qs = qs.filter(status=status)
    if listing_type:
        qs = qs.filter(listing_type=listing_type)
    if created_by:
        qs = qs.filter(created_by__public_id=created_by)
    return qs


def _region_or_400(region_id: UUID) -> Region:
    """Sync — called from inside the `_apply_*` helpers, under a transaction."""
    region = Region.objects.filter(public_id=region_id).first()
    if region is None:
        raise HttpError(400, "Unknown region")
    return region


def _apply_create_listing(request, organization, data: ListingCreateIn) -> Listing:
    """Sync transactional core of `create_listing`."""
    if data.status not in (Listing.Status.DRAFT, Listing.Status.ACTIVE):
        raise HttpError(400, "A new listing can only be created as a draft or active")

    # Scoped to the caller's org, so another organization's asset is simply
    # not found — never a 403 confirming it exists (§0.1). This is also the
    # application-level half of the same-org invariant the database enforces
    # with a composite foreign key.
    asset = get_object_or_404(
        Asset.objects.filter(organization=organization), public_id=data.asset_id
    )

    region = organization.region
    if data.region_id is not None:
        region = _region_or_400(data.region_id)

    if data.status == Listing.Status.ACTIVE and _has_active_listing(
        asset, data.listing_type
    ):
        raise HttpError(
            409,
            "This machine already has an active listing of that type. "
            "Archive it first, or create this one as a draft.",
        )

    payload = data.dict(exclude={"asset_id", "region_id", "status"})
    with transaction.atomic():
        try:
            listing = Listing.objects.create(
                organization=organization,
                asset=asset,
                created_by=request.auth,
                region=region,
                status=data.status,
                published_at=(
                    timezone.now() if data.status == Listing.Status.ACTIVE else None
                ),
                **payload,
            )
        except IntegrityError as exc:
            raise _integrity_error(exc)

        log_activity(
            request,
            organization,
            ActivityLog.Action.CREATED,
            listing,
            changes=diff({}, listing_snapshot(listing)),
        )

    # Re-read through the joined queryset: ListingOut serialises the asset,
    # its equipment model and the manufacturer, none of which an async
    # response can fetch lazily.
    return org_listings(organization).get(pk=listing.pk)


def _has_active_listing(asset: Asset, listing_type: str, exclude_pk=None) -> bool:
    qs = Listing.objects.filter(
        asset=asset, listing_type=listing_type, status=Listing.Status.ACTIVE
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def _integrity_error(exc: IntegrityError) -> HttpError:
    """
    Turn a constraint violation into the status it deserves.

    The checks above catch these first; this is the backstop for two requests
    racing, where only the database can arbitrate.
    """
    message = str(exc)
    if "listing_one_active_per_asset_type" in message:
        return HttpError(409, "This machine already has an active listing of that type.")
    if "listing_total_price_unit_iff_sale" in message:
        return HttpError(
            400,
            "price_unit 'total' is for sale listings, and a sale must be "
            "priced as a total.",
        )
    if "listing_asset_same_org_fk" in message:
        return HttpError(400, "That machine belongs to another organization.")
    raise exc


@listings_router.post("", response={201: ListingOut})
async def create_listing(request, data: ListingCreateIn):
    """
    Put one of the organization's machines on the market.

    The listing is a separate object from the machine (§0.5): creating one
    changes nothing about the asset, and the same asset can carry a rental
    offer and a sale offer at once.
    """
    organization = writable_organization(request)
    listing = await sync_to_async(_apply_create_listing)(request, organization, data)
    return 201, listing


@listings_router.get("/{listing_id}", response=ListingOut, auth=OptionalJWTBearer())
async def get_listing(request, listing_id: UUID):
    """
    One listing.

    Optional auth, not public: a stranger sees it only while it is active and
    the machine is available, but the organization that owns it sees its own
    drafts and paused offers here too — otherwise the edit screen would have
    to read from a different endpoint than the public page it previews.
    """
    user = authenticated_user(request)
    visible = Q(
        status=Listing.Status.ACTIVE,
        asset__operational_status=Asset.OperationalStatus.AVAILABLE,
    )
    if user is not None and user.organization_id:
        visible |= Q(organization_id=user.organization_id)

    return await aget_object_or_404(
        _listing_relations(Listing.objects.filter(visible)), public_id=listing_id
    )


def _apply_update_listing(request, organization, listing: Listing, data: ListingUpdateIn):
    """Sync transactional core of `update_listing`."""
    fields = data.dict(exclude_unset=True)
    if not fields:
        return listing

    before = listing_snapshot(listing)

    if "region_id" in fields:
        region_id = fields.pop("region_id")
        listing.region = _region_or_400(region_id) if region_id else None
    for name, value in fields.items():
        setattr(listing, name, value)

    going_live = (
        listing.status == Listing.Status.ACTIVE
        and before["status"] != Listing.Status.ACTIVE
    )
    if going_live and _has_active_listing(
        listing.asset, listing.listing_type, exclude_pk=listing.pk
    ):
        raise HttpError(
            409, "This machine already has an active listing of that type."
        )

    changes = diff(before, listing_snapshot(listing))
    if not changes:
        return listing

    action = (
        ActivityLog.Action.STATUS_CHANGED
        if set(changes) == {"status"}
        else ActivityLog.Action.UPDATED
    )

    with transaction.atomic():
        # Set once, on the first activation, and never cleared: it is when
        # the offer first went to market, not when it was last unpaused.
        if going_live and listing.published_at is None:
            listing.published_at = timezone.now()
        try:
            listing.save()
        except IntegrityError as exc:
            raise _integrity_error(exc)
        log_activity(request, organization, action, listing, changes=changes)

    return listing


@listings_router.patch("/{listing_id}", response=ListingOut)
async def update_listing(request, listing_id: UUID, data: ListingUpdateIn):
    """
    Partial update — only the keys present in the body are applied.

    Also the pause / activate / archive route, via `status`: publication state
    is a field on the listing, which is exactly what having a separate model
    buys (§0.5).
    """
    organization = writable_organization(request)
    listing = await aget_object_or_404(org_listings(organization), public_id=listing_id)
    return await sync_to_async(_apply_update_listing)(
        request, organization, listing, data
    )


def _apply_archive_listing(request, organization, listing: Listing) -> None:
    """Sync transactional core of `delete_listing`."""
    before = listing_snapshot(listing)
    listing.status = Listing.Status.ARCHIVED
    with transaction.atomic():
        listing.save(update_fields=["status", "updated_at"])
        log_activity(
            request,
            organization,
            ActivityLog.Action.DELETED,
            listing,
            changes=diff(before, listing_snapshot(listing)),
        )


@listings_router.delete("/{listing_id}", response={204: None})
async def delete_listing(request, listing_id: UUID):
    """
    Take a listing off the market.

    A soft delete — the row moves to `archived`. Inquiries will reference
    listings, and hard-deleting would tear a hole in the organization's own
    history; the asset it points at is untouched either way.
    """
    organization = writable_organization(request)
    listing = await aget_object_or_404(org_listings(organization), public_id=listing_id)
    await sync_to_async(_apply_archive_listing)(request, organization, listing)
    return 204, None


# ── images ────────────────────────────────────────────────────────────────────

def _validate_image(upload: UploadedFile) -> None:
    """
    Refuse anything that isn't one of the image formats we accept.

    Three separate checks, because each catches what the others don't: the
    size cap before anything reaches disk, the extension because that is what
    the file will be served as, and the leading bytes because the extension is
    just a name the client chose.
    """
    if upload.size and upload.size > settings.LISTING_IMAGE_MAX_BYTES:
        limit_mb = settings.LISTING_IMAGE_MAX_BYTES // (1024 * 1024)
        raise HttpError(400, f"Images must be {limit_mb} MB or smaller")

    name = (upload.name or "").lower()
    if not any(name.endswith(f".{ext}") for ext in IMAGE_EXTENSIONS):
        raise HttpError(
            400, f"Images must be one of: {', '.join(IMAGE_EXTENSIONS)}"
        )

    head = upload.read(16)
    upload.seek(0)
    is_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if not is_webp and not head.startswith(IMAGE_SIGNATURES):
        raise HttpError(400, "That file is not a JPEG, PNG or WebP image")


def _apply_add_image(request, organization, listing: Listing, upload: UploadedFile):
    """Sync transactional core of `add_listing_image`."""
    _validate_image(upload)

    existing = listing.images.count()
    if existing >= settings.LISTING_IMAGE_MAX_COUNT:
        raise HttpError(
            400,
            f"A listing can have at most {settings.LISTING_IMAGE_MAX_COUNT} images",
        )

    with transaction.atomic():
        image = ListingImage.objects.create(
            listing=listing,
            file=upload,
            sort_order=existing,
            # The first photo is the card image until someone says otherwise.
            is_primary=existing == 0,
        )
        log_activity(
            request,
            organization,
            ActivityLog.Action.UPDATED,
            listing,
            changes={"images": {"from": existing, "to": existing + 1}},
        )
    return image


@listings_router.post("/{listing_id}/images", response={201: ListingImageOut})
async def add_listing_image(request, listing_id: UUID, file: UploadedFile = File(...)):
    """
    Attach a photo to a listing.

    Photos hang off the offer, not the machine (§0.5) — they are taken to sell
    this listing, and an asset relisted next season deserves new ones. Stored
    on local disk for now; the URL that comes back is absolute, so moving to
    object storage later is not a change to this contract (§0b).
    """
    organization = writable_organization(request)
    listing = await aget_object_or_404(org_listings(organization), public_id=listing_id)
    return 201, await sync_to_async(_apply_add_image)(
        request, organization, listing, file
    )


def _apply_delete_image(request, organization, listing: Listing, image: ListingImage):
    """Sync transactional core of `delete_listing_image`."""
    was_primary = image.is_primary
    with transaction.atomic():
        image.file.delete(save=False)
        image.delete()
        if was_primary:
            # Inside the transaction and after the delete, so the partial
            # unique index never sees two primaries at once.
            successor = listing.images.order_by("sort_order", "pk").first()
            if successor is not None:
                successor.is_primary = True
                successor.save(update_fields=["is_primary"])
        log_activity(
            request,
            organization,
            ActivityLog.Action.UPDATED,
            listing,
            changes={"images": {"from": "removed", "to": str(image.public_id)}},
        )


@listings_router.delete("/{listing_id}/images/{image_id}", response={204: None})
async def delete_listing_image(request, listing_id: UUID, image_id: UUID):
    """Remove a photo. If it was the card image, the next one takes over."""
    organization = writable_organization(request)
    listing = await aget_object_or_404(org_listings(organization), public_id=listing_id)
    image = await aget_object_or_404(listing.images.all(), public_id=image_id)
    await sync_to_async(_apply_delete_image)(request, organization, listing, image)
    return 204, None
