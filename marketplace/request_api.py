"""
marketplace/request_api.py
Purchase requests and the quotes sellers send back.

The feed needs a token, unlike the offer feed: who is buying what, and at what
target price, is a list of leads, and it should not be a public scrape.
"""

from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import IntegrityError, transaction
from django.db.models import DecimalField, Exists, OuterRef, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import aget_object_or_404, get_object_or_404
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from core.audit import log_activity
from core.models import ActivityLog
from users.models import OrgPermission, Organization
from users.permissions import readable_organization, writable_organization

from .models import PurchaseRequest, Quote, Side, market_today
from .offer_api import (
    active_product_or_400,
    region_or_400,
    require_may_sell,
)
from .order_api import order_detail
from .order_services import Actor, close_request, order_from_quote
from .schemas import (
    OrderDetailOut,
    PurchaseRequestCreateIn,
    PurchaseRequestOut,
    QuoteAcceptIn,
    QuoteCreateIn,
    QuoteOut,
)

requests_router = Router(tags=["Market requests"])
quotes_router = Router(tags=["Market requests"])


def request_relations(queryset):
    """Everything `PurchaseRequestOut` serialises, joined up front."""
    return queryset.select_related(
        "product",
        "product__category",
        "customer_organization",
        "customer_organization__region",
        "delivery_region",
    ).annotate(
        quantity_covered=Coalesce(
            Sum("quotes__quantity", filter=Q(quotes__status=Quote.Status.ACCEPTED)),
            Value(0),
            output_field=DecimalField(),
        )
    )


def quote_relations(queryset):
    return queryset.select_related(
        "request", "provider_organization", "provider_organization__region"
    )


def open_requests():
    return PurchaseRequest.objects.filter(
        status=PurchaseRequest.Status.OPEN, needed_until__gte=market_today()
    )


def _visible_requests(organization: Organization):
    """
    The open feed, plus what this organization posted or has quoted on.

    `Exists` rather than a join on `quotes`: `request_relations` sums that same
    relation, and a filter on it would narrow the sum to this seller's quotes.
    """
    has_quoted = Exists(
        Quote.objects.filter(request=OuterRef("pk"), provider_organization=organization)
    )
    return PurchaseRequest.objects.filter(
        Q(status=PurchaseRequest.Status.OPEN, needed_until__gte=market_today())
        | Q(customer_organization=organization)
        | Q(has_quoted)
    )


# ── requests ─────────────────────────────────────────────────────────────────

@requests_router.get("", response=list[PurchaseRequestOut])
@paginate(LimitOffsetPagination)
async def list_requests(
    request,
    product: str | None = None,
    category: str | None = None,
    region: str | None = None,
    is_recurring: bool | None = None,
):
    """What buyers are looking for right now, excluding the caller's own."""
    organization = readable_organization(request, OrgPermission.VIEW_MARKET)
    qs = request_relations(open_requests().exclude(customer_organization=organization))
    if product:
        qs = qs.filter(product__slug=product)
    if category:
        qs = qs.filter(
            Q(product__category__slug=category)
            | Q(product__category__parent__slug=category)
        )
    if region:
        qs = qs.filter(Q(delivery_region__slug=region) | Q(delivery_region__code=region))
    if is_recurring is not None:
        qs = qs.filter(is_recurring=is_recurring)
    return qs


@requests_router.get("/my", response=list[PurchaseRequestOut])
@paginate(LimitOffsetPagination)
async def list_my_requests(request, status: str | None = None):
    organization = readable_organization(request, OrgPermission.VIEW_MARKET)
    qs = request_relations(PurchaseRequest.objects.filter(customer_organization=organization))
    if status:
        qs = qs.filter(status=status)
    return qs


def _apply_create_request(request, organization, data: PurchaseRequestCreateIn):
    if data.needed_until < data.needed_from:
        raise HttpError(400, "needed_until cannot be before needed_from")
    if data.needed_until < market_today():
        raise HttpError(400, "Those dates have passed")
    product = active_product_or_400(data.product_id)
    region = (
        region_or_400(data.delivery_region_id)
        if data.delivery_region_id
        else organization.region
    )
    values = data.dict(exclude={"product_id", "delivery_region_id", "unit"})
    with transaction.atomic():
        purchase_request = PurchaseRequest.objects.create(
            customer_organization=organization,
            created_by=request.auth,
            product=product,
            delivery_region=region,
            unit=data.unit or product.default_unit,
            **values,
        )
        log_activity(
            request,
            organization,
            ActivityLog.Action.CREATED,
            purchase_request,
            changes={"quantity": {"from": None, "to": str(purchase_request.quantity)}},
        )
    return request_relations(PurchaseRequest.objects.all()).get(pk=purchase_request.pk)


@requests_router.post("", response={201: PurchaseRequestOut})
async def create_request(request, data: PurchaseRequestCreateIn):
    """Say what you need. Set `is_recurring` for regular supply."""
    organization = writable_organization(request, OrgPermission.MANAGE_MARKET)
    return 201, await sync_to_async(_apply_create_request)(request, organization, data)


@requests_router.get("/{request_id}", response=PurchaseRequestOut)
async def get_request(request, request_id: UUID):
    organization = readable_organization(request, OrgPermission.VIEW_MARKET)
    return await aget_object_or_404(
        request_relations(_visible_requests(organization)), public_id=request_id
    )


def _apply_close_request(request, organization, request_id: UUID):
    with transaction.atomic():
        purchase_request = get_object_or_404(
            PurchaseRequest.objects.select_for_update().filter(
                customer_organization=organization
            ),
            public_id=request_id,
        )
        if purchase_request.status != PurchaseRequest.Status.OPEN:
            raise HttpError(409, "This request is already closed.")
        close_request(purchase_request)
        log_activity(
            request,
            organization,
            ActivityLog.Action.STATUS_CHANGED,
            purchase_request,
            changes={"status": {"from": "open", "to": purchase_request.status}},
        )
    return request_relations(PurchaseRequest.objects.all()).get(pk=purchase_request.pk)


@requests_router.post("/{request_id}/close", response=PurchaseRequestOut)
async def close_purchase_request(request, request_id: UUID):
    """Stop taking quotes. Any still waiting are rejected."""
    organization = writable_organization(request, OrgPermission.MANAGE_MARKET)
    return await sync_to_async(_apply_close_request)(request, organization, request_id)


# ── quotes ───────────────────────────────────────────────────────────────────

def _apply_create_quote(request, organization, purchase_request, data: QuoteCreateIn):
    if purchase_request.customer_organization_id == organization.pk:
        raise HttpError(400, "You cannot quote on your own request.")
    require_may_sell(purchase_request.product, organization)
    if data.valid_until < market_today():
        raise HttpError(400, "valid_until has passed")
    try:
        with transaction.atomic():
            quote = Quote.objects.create(
                request=purchase_request,
                provider_organization=organization,
                created_by=request.auth,
                **data.dict(),
            )
            log_activity(
                request,
                organization,
                ActivityLog.Action.CREATED,
                quote,
                changes={"unit_price": {"from": None, "to": str(quote.unit_price)}},
            )
    except IntegrityError as exc:
        if "quote_one_live_per_provider" in str(exc):
            raise HttpError(409, "You already have a live quote on this request.")
        raise
    return quote_relations(Quote.objects.all()).get(pk=quote.pk)


@requests_router.post("/{request_id}/quotes", response={201: QuoteOut})
async def create_quote(request, request_id: UUID, data: QuoteCreateIn):
    organization = writable_organization(request, OrgPermission.MANAGE_MARKET)
    purchase_request = await aget_object_or_404(open_requests(), public_id=request_id)
    return 201, await sync_to_async(_apply_create_quote)(
        request, organization, purchase_request, data
    )


@requests_router.get("/{request_id}/quotes", response=list[QuoteOut])
@paginate(LimitOffsetPagination)
async def list_request_quotes(request, request_id: UUID):
    """The buyer sees every quote, cheapest first. A seller sees only their own."""
    organization = readable_organization(request, OrgPermission.VIEW_MARKET)
    purchase_request = await aget_object_or_404(
        _visible_requests(organization), public_id=request_id
    )
    quotes = quote_relations(purchase_request.quotes.all())
    if purchase_request.customer_organization_id != organization.pk:
        quotes = quotes.filter(provider_organization=organization)
    return quotes


@quotes_router.get("/my", response=list[QuoteOut])
@paginate(LimitOffsetPagination)
async def list_my_quotes(request, status: str | None = None):
    organization = readable_organization(request, OrgPermission.VIEW_MARKET)
    qs = quote_relations(Quote.objects.filter(provider_organization=organization))
    if status:
        qs = qs.filter(status=status)
    return qs


def _received_quotes(organization):
    return Quote.objects.filter(request__customer_organization=organization)


def _apply_accept_quote(request, organization, quote: Quote, data: QuoteAcceptIn):
    actor = Actor.from_request(request, organization, Side.CUSTOMER)
    order = order_from_quote(
        actor,
        quote,
        delivery_date=data.delivery_date,
        delivery_method=data.delivery_method,
        delivery_address=data.delivery_address,
    )
    return order_detail(order.pk)


@quotes_router.post("/{quote_id}/accept", response={201: OrderDetailOut})
async def accept_quote(request, quote_id: UUID, data: QuoteAcceptIn):
    """Take a one-time quote: this creates an order waiting for payment."""
    organization = writable_organization(request, OrgPermission.MANAGE_MARKET)
    quote = await aget_object_or_404(_received_quotes(organization), public_id=quote_id)
    return 201, await sync_to_async(_apply_accept_quote)(request, organization, quote, data)


def _apply_close_quote(request, organization, queryset, quote_id, to_status) -> Quote:
    with transaction.atomic():
        quote = get_object_or_404(queryset.select_for_update(), public_id=quote_id)
        if quote.status != Quote.Status.SUBMITTED:
            raise HttpError(409, "This quote is no longer open.")
        quote.status = to_status
        quote.save(update_fields=["status", "updated_at"])
        log_activity(
            request,
            organization,
            ActivityLog.Action.STATUS_CHANGED,
            quote,
            changes={"status": {"from": Quote.Status.SUBMITTED, "to": to_status}},
        )
    return quote_relations(Quote.objects.all()).get(pk=quote.pk)


@quotes_router.post("/{quote_id}/reject", response=QuoteOut)
async def reject_quote(request, quote_id: UUID):
    organization = writable_organization(request, OrgPermission.MANAGE_MARKET)
    return await sync_to_async(_apply_close_quote)(
        request,
        organization,
        _received_quotes(organization),
        quote_id,
        Quote.Status.REJECTED,
    )


@quotes_router.post("/{quote_id}/withdraw", response=QuoteOut)
async def withdraw_quote(request, quote_id: UUID):
    organization = writable_organization(request, OrgPermission.MANAGE_MARKET)
    return await sync_to_async(_apply_close_quote)(
        request,
        organization,
        Quote.objects.filter(provider_organization=organization),
        quote_id,
        Quote.Status.WITHDRAWN,
    )
