"""
marketplace/offer_api.py
The product catalogue, the public feed of offers, and a seller's own offers.

The same three audiences as `listings/views.py`: anyone reads the feed, members
holding `market.view` read their organization's offers in any status, and
`market.manage` writes. Async endpoints wrap a sync `_apply_*`, for the reason
equipment/views.py gives: a write and its audit row must commit together.
"""

import os
from datetime import date
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
from core.audit import log_activity
from core.models import ActivityLog
from farms.models import CropZone
from listings.views import _validate_image
from users.models import OrgPermission, Organization, Region
from users.permissions import caller_organization, readable_organization, require_perm

from .models import (
    Offer,
    OfferImage,
    Product,
    ProductCategory,
    active_offer_q,
    active_offers,
    market_today,
)
from .reliability import provider_reliability
from .schemas import (
    NULLABLE_OFFER_FIELDS,
    OfferCreateIn,
    OfferImageOut,
    OfferOut,
    OfferUpdateIn,
    ProductCategoryOut,
    ProductOut,
    ReliabilityOut,
)

catalog_router = Router(tags=["Market catalogue"])
offers_router = Router(tags=["Market offers"])

AUDIT_FIELDS = [
    "title",
    "description",
    "variety",
    "grade",
    "price",
    "quantity_available",
    "min_order_quantity",
    "available_from",
    "available_until",
    "offers_pickup",
    "offers_delivery",
    "district",
    "status",
]
SORT_OPTIONS = {
    "newest": "-created_at",
    "price_asc": "price",
    "price_desc": "-price",
}


def writable_organization(request) -> Organization:
    organization = caller_organization(request)
    require_perm(request, OrgPermission.MANAGE_MARKET)
    return organization


def offer_snapshot(offer: Offer) -> dict:
    """Strings, so a price goes into the audit trail exactly as it was."""
    return {name: str(getattr(offer, name)) for name in AUDIT_FIELDS}


def diff_snapshots(before: dict, after: dict) -> dict:
    return {
        name: {"from": before.get(name), "to": value}
        for name, value in after.items()
        if before.get(name) != value
    }


def offer_relations(queryset):
    """Everything `OfferOut` serialises, joined up front."""
    return queryset.select_related(
        "organization",
        "organization__region",
        "region",
        "product",
        "product__category",
        "crop_zone",
    ).prefetch_related("images")


def org_offers(organization: Organization):
    return offer_relations(Offer.objects.filter(organization=organization))


def active_product_or_400(product_id: UUID) -> Product:
    product = Product.objects.filter(public_id=product_id, is_active=True).first()
    if product is None:
        raise HttpError(400, "Unknown product")
    return product


def region_or_400(region_id: UUID) -> Region:
    region = Region.objects.filter(public_id=region_id).first()
    if region is None:
        raise HttpError(400, "Unknown region")
    return region


# ── catalogue ────────────────────────────────────────────────────────────────

@catalog_router.get("/categories", response=list[ProductCategoryOut], auth=None)
async def list_categories(request):
    """The category tree, children inlined."""
    return [
        category
        async for category in ProductCategory.objects.filter(
            parent__isnull=True
        ).prefetch_related("children")
    ]


@catalog_router.get("/products", response=list[ProductOut], auth=None)
@paginate(LimitOffsetPagination)
async def list_products(request, search: str | None = None, category: str | None = None):
    """What can be sold. Rows are populated by admins, never by API clients."""
    qs = Product.objects.filter(is_active=True).select_related("category")
    if search:
        qs = qs.filter(name__icontains=search)
    if category:
        qs = qs.filter(Q(category__slug=category) | Q(category__parent__slug=category))
    return qs


@catalog_router.get(
    "/organizations/{org_id}/reliability", response=ReliabilityOut, auth=None
)
async def get_reliability(request, org_id: UUID):
    """How dependable a seller has been. Public, like the rest of a seller's profile."""
    organization = await aget_object_or_404(Organization, public_id=org_id)
    return await sync_to_async(provider_reliability)(organization)


# ── the public feed ──────────────────────────────────────────────────────────

@offers_router.get("", response=list[OfferOut], auth=None)
@paginate(LimitOffsetPagination)
async def list_offers(
    request,
    search: str | None = None,
    category: str | None = None,
    product: str | None = None,
    region: str | None = None,
    offer_type: str | None = None,
    grade: str | None = None,
    unit: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    available_on: date | None = None,
    has_delivery: bool | None = None,
    verified_only: bool | None = None,
    owner: UUID | None = None,
    sort: str | None = None,
):
    """
    Produce on the market now. Prices are only comparable within one `unit`, so
    a client sorting by price should filter by unit as well.
    """
    qs = offer_relations(active_offers())
    if search:
        qs = qs.filter(
            Q(title__icontains=search)
            | Q(description__icontains=search)
            | Q(variety__icontains=search)
            | Q(product__name__icontains=search)
        )
    if category:
        qs = qs.filter(
            Q(product__category__slug=category)
            | Q(product__category__parent__slug=category)
        )
    if product:
        qs = qs.filter(product__slug=product)
    if region:
        qs = qs.filter(Q(region__slug=region) | Q(region__code=region))
    if offer_type:
        qs = qs.filter(offer_type=offer_type)
    if grade:
        qs = qs.filter(grade=grade)
    if unit:
        qs = qs.filter(unit=unit)
    if min_price is not None:
        qs = qs.filter(price__gte=min_price)
    if max_price is not None:
        qs = qs.filter(price__lte=max_price)
    if available_on:
        qs = qs.filter(available_from__lte=available_on, available_until__gte=available_on)
    if has_delivery is not None:
        qs = qs.filter(offers_delivery=has_delivery)
    if verified_only:
        qs = qs.filter(organization__is_verified=True)
    if owner:
        qs = qs.filter(organization__public_id=owner)
    if sort:
        if sort not in SORT_OPTIONS:
            raise HttpError(400, f"Unknown sort '{sort}'")
        qs = qs.order_by(SORT_OPTIONS[sort], "-created_at")
    return qs


@offers_router.get("/my", response=list[OfferOut])
@paginate(LimitOffsetPagination)
async def list_my_offers(request, status: str | None = None):
    """Everything the caller's organization has offered, in every status."""
    qs = org_offers(readable_organization(request, OrgPermission.VIEW_MARKET))
    if status:
        qs = qs.filter(status=status)
    return qs


@offers_router.get("/{offer_id}", response=OfferOut, auth=OptionalJWTBearer())
async def get_offer(request, offer_id: UUID):
    """One offer. The organization that owns it also sees its drafts here."""
    user = authenticated_user(request)
    visible = active_offer_q()
    if (
        user is not None
        and user.organization_id
        and user.has_org_perm(OrgPermission.VIEW_MARKET)
    ):
        visible |= Q(organization_id=user.organization_id)
    return await aget_object_or_404(
        offer_relations(Offer.objects.filter(visible)), public_id=offer_id
    )


# ── writing ──────────────────────────────────────────────────────────────────

def _check_publishable(offer: Offer) -> None:
    """Called on the way to `active`, the only state in which money can follow."""
    if offer.available_until < market_today():
        raise HttpError(400, "This offer's dates have passed.")
    if offer.product.is_regulated and not offer.organization.is_verified:
        raise HttpError(
            403, f"Only verified organizations can sell {offer.product.name.lower()}."
        )


def _check_dates(offer: Offer) -> None:
    if offer.available_until < offer.available_from:
        raise HttpError(400, "The offer cannot end before it starts.")
    if not (offer.offers_pickup or offer.offers_delivery):
        raise HttpError(400, "Offer pickup, delivery or both.")


def _own_crop_zone(organization, crop_zone_id: UUID) -> CropZone:
    """Scoped to the caller, so another organization's crop is simply not found."""
    return get_object_or_404(
        CropZone.objects.filter(organization=organization), public_id=crop_zone_id
    )


def _apply_create_offer(request, organization, data: OfferCreateIn) -> Offer:
    if data.status not in (Offer.Status.DRAFT, Offer.Status.ACTIVE):
        raise HttpError(400, "A new offer can only be a draft or active")

    product = active_product_or_400(data.product_id)
    region = region_or_400(data.region_id) if data.region_id else organization.region
    crop_zone = (
        _own_crop_zone(organization, data.crop_zone_id) if data.crop_zone_id else None
    )
    values = data.dict(exclude={"product_id", "region_id", "crop_zone_id", "unit", "status"})
    offer = Offer(
        organization=organization,
        created_by=request.auth,
        product=product,
        region=region,
        crop_zone=crop_zone,
        unit=data.unit or product.default_unit,
        status=data.status,
        **values,
    )
    _check_dates(offer)
    if offer.status == Offer.Status.ACTIVE:
        _check_publishable(offer)
        offer.published_at = timezone.now()

    with transaction.atomic():
        offer.save()
        log_activity(
            request,
            organization,
            ActivityLog.Action.CREATED,
            offer,
            changes=diff_snapshots({}, offer_snapshot(offer)),
        )
    return org_offers(organization).get(pk=offer.pk)


@offers_router.post("", response={201: OfferOut})
async def create_offer(request, data: OfferCreateIn):
    organization = writable_organization(request)
    return 201, await sync_to_async(_apply_create_offer)(request, organization, data)


def _apply_update_offer(request, organization, offer_id, data: OfferUpdateIn) -> Offer:
    fields = data.dict(exclude_unset=True)
    nulled = sorted(
        name
        for name, value in fields.items()
        if value is None and name not in NULLABLE_OFFER_FIELDS
    )
    if nulled:
        raise HttpError(400, f"These fields cannot be null: {', '.join(nulled)}")

    with transaction.atomic():
        # Locked, so setting the stock cannot overwrite an order landing now.
        offer = get_object_or_404(
            Offer.objects.select_for_update(of=("self",))
            .select_related("product", "organization")
            .filter(organization=organization),
            public_id=offer_id,
        )
        before = offer_snapshot(offer)
        if "region_id" in fields:
            region_id = fields.pop("region_id")
            offer.region = region_or_400(region_id) if region_id else None
        if "crop_zone_id" in fields:
            crop_zone_id = fields.pop("crop_zone_id")
            offer.crop_zone = (
                _own_crop_zone(organization, crop_zone_id) if crop_zone_id else None
            )
        for name, value in fields.items():
            setattr(offer, name, value)
        _check_dates(offer)

        going_live = (
            offer.status == Offer.Status.ACTIVE and before["status"] != Offer.Status.ACTIVE
        )
        if going_live:
            _check_publishable(offer)
            offer.published_at = offer.published_at or timezone.now()

        changes = diff_snapshots(before, offer_snapshot(offer))
        offer.save()
        if changes:
            action = (
                ActivityLog.Action.STATUS_CHANGED
                if set(changes) == {"status"}
                else ActivityLog.Action.UPDATED
            )
            log_activity(request, organization, action, offer, changes=changes)
    return org_offers(organization).get(pk=offer.pk)


@offers_router.patch("/{offer_id}", response=OfferOut)
async def update_offer(request, offer_id: UUID, data: OfferUpdateIn):
    """Only the keys sent are applied. Also the pause and publish route."""
    organization = writable_organization(request)
    return await sync_to_async(_apply_update_offer)(request, organization, offer_id, data)


def _apply_archive_offer(request, organization, offer: Offer) -> None:
    before = offer.status
    with transaction.atomic():
        offer.status = Offer.Status.ARCHIVED
        offer.save(update_fields=["status", "updated_at"])
        log_activity(
            request,
            organization,
            ActivityLog.Action.DELETED,
            offer,
            changes={"status": {"from": before, "to": offer.status}},
        )


@offers_router.delete("/{offer_id}", response={204: None})
async def delete_offer(request, offer_id: UUID):
    """A soft delete. Orders point at offers, and open ones are not affected."""
    organization = writable_organization(request)
    offer = await aget_object_or_404(org_offers(organization), public_id=offer_id)
    await sync_to_async(_apply_archive_offer)(request, organization, offer)
    return 204, None


# ── images ───────────────────────────────────────────────────────────────────

def _apply_add_image(request, organization, offer: Offer, upload: UploadedFile):
    _validate_image(upload)
    with transaction.atomic():
        # Counting, capping and inserting are one critical section.
        Offer.objects.select_for_update().get(pk=offer.pk)
        existing = OfferImage.objects.filter(offer=offer).count()
        if existing >= settings.LISTING_IMAGE_MAX_COUNT:
            raise HttpError(
                400, f"An offer can have at most {settings.LISTING_IMAGE_MAX_COUNT} images"
            )
        try:
            image = OfferImage.objects.create(
                offer=offer,
                file=upload,
                sort_order=existing,
                is_primary=existing == 0,
            )
        except IntegrityError as exc:
            if "offer_one_primary_image" in str(exc):
                raise HttpError(409, "Another image was made the card image just now.")
            raise
        log_activity(
            request,
            organization,
            ActivityLog.Action.UPDATED,
            offer,
            changes={"images": {"from": existing, "to": existing + 1}},
        )
    return image


@offers_router.post("/{offer_id}/images", response={201: OfferImageOut})
async def add_offer_image(request, offer_id: UUID, file: UploadedFile = File(...)):
    organization = writable_organization(request)
    offer = await aget_object_or_404(org_offers(organization), public_id=offer_id)
    return 201, await sync_to_async(_apply_add_image)(request, organization, offer, file)


def _apply_delete_image(request, organization, offer: Offer, image: OfferImage) -> None:
    was_primary = image.is_primary
    stored_file = image.file
    name = os.path.basename(image.file.name)
    with transaction.atomic():
        # Only once the row is certainly gone: a file delete cannot roll back.
        transaction.on_commit(lambda: stored_file.delete(save=False))
        image.delete()
        if was_primary:
            successor = offer.images.order_by("sort_order", "pk").first()
            if successor is not None:
                successor.is_primary = True
                successor.save(update_fields=["is_primary"])
        log_activity(
            request,
            organization,
            ActivityLog.Action.UPDATED,
            offer,
            changes={"images": {"from": name, "to": "removed"}},
        )


@offers_router.delete("/{offer_id}/images/{image_id}", response={204: None})
async def delete_offer_image(request, offer_id: UUID, image_id: UUID):
    organization = writable_organization(request)
    offer = await aget_object_or_404(org_offers(organization), public_id=offer_id)
    image = await aget_object_or_404(offer.images.all(), public_id=image_id)
    await sync_to_async(_apply_delete_image)(request, organization, offer, image)
    return 204, None
