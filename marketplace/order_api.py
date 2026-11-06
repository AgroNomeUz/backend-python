"""
marketplace/order_api.py
Orders, from either side. Money and stock rules live in `order_services`.

Both organizations can read an order, but each action belongs to one side: the
other side's id is a 404 on that action (§0.1), the same way `deals/views.py`
scopes confirm and decline. `market.manage` gates handling an order; paying
moves money, so it needs `billing.manage` instead.

Disputes are decided by platform staff, not by either organization. Those two
endpoints check `is_staff`, and staff may have no organization at all.
"""

from datetime import date
from uuid import UUID

from asgiref.sync import sync_to_async
from django.db.models import Q
from django.http import Http404
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from users.models import OrgPermission, Organization
from users.permissions import caller_organization, readable_organization, require_perm

from . import order_services as services
from .models import Offer, Order, OrderDispute, Side, active_offer_q
from .offer_api import writable_organization
from .schemas import (
    DisputeResolveIn,
    OrderConfirmIn,
    OrderCreateIn,
    OrderDetailOut,
    OrderOut,
    OrderPayIn,
    OrderPayOut,
    OrderRejectIn,
    ReasonIn,
)

orders_router = Router(tags=["Market orders"])
disputes_router = Router(tags=["Market orders"])


def order_relations(queryset):
    """Everything `OrderOut` serialises, joined up front."""
    return queryset.select_related(
        "product",
        "product__category",
        "provider_organization",
        "provider_organization__region",
        "customer_organization",
        "customer_organization__region",
        "offer",
        "quote",
        "contract",
        "deal",
        "hold",
        "dispute",
    )


def order_detail(pk: int) -> Order:
    return order_relations(Order.objects.all()).prefetch_related("events").get(pk=pk)


def org_orders(organization: Organization):
    return Order.objects.filter(
        Q(provider_organization=organization) | Q(customer_organization=organization)
    )


async def _order_as(side: str, organization, order_id: UUID) -> Order:
    field = {
        Side.PROVIDER: "provider_organization",
        Side.CUSTOMER: "customer_organization",
    }[side]
    return await aget_object_or_404(
        Order.objects.filter(**{field: organization}), public_id=order_id
    )


def _run(request, organization, side, operation, order, *args, **kwargs) -> Order:
    actor = services.Actor.from_request(request, organization, side)
    result = operation(actor, order, *args, **kwargs)
    return order_detail(result.pk)


async def _as_provider(request, order_id, operation, *args, **kwargs) -> Order:
    organization = writable_organization(request)
    order = await _order_as(Side.PROVIDER, organization, order_id)
    return await sync_to_async(_run)(
        request, organization, Side.PROVIDER, operation, order, *args, **kwargs
    )


async def _as_customer(request, order_id, operation, *args, **kwargs) -> Order:
    organization = writable_organization(request)
    order = await _order_as(Side.CUSTOMER, organization, order_id)
    return await sync_to_async(_run)(
        request, organization, Side.CUSTOMER, operation, order, *args, **kwargs
    )


# ── reading and placing ──────────────────────────────────────────────────────

@orders_router.get("", response=list[OrderOut])
@paginate(LimitOffsetPagination)
async def list_orders(
    request,
    role: str | None = None,
    status: str | None = None,
    contract_id: UUID | None = None,
    delivery_date: date | None = None,
):
    """The organization's orders. `role` is `provider` or `customer`."""
    organization = readable_organization(request, OrgPermission.VIEW_MARKET)
    if role == Side.PROVIDER:
        qs = Order.objects.filter(provider_organization=organization)
    elif role == Side.CUSTOMER:
        qs = Order.objects.filter(customer_organization=organization)
    elif role is None:
        qs = org_orders(organization)
    else:
        raise HttpError(400, "role must be 'provider' or 'customer'")
    if status:
        qs = qs.filter(status=status)
    if contract_id:
        qs = qs.filter(contract__public_id=contract_id)
    if delivery_date:
        qs = qs.filter(delivery_date=delivery_date)
    return order_relations(qs)


def _apply_place_order(request, organization, offer: Offer, data: OrderCreateIn) -> Order:
    actor = services.Actor.from_request(request, organization, Side.CUSTOMER)
    order = services.place_order(
        actor,
        offer,
        quantity=data.quantity,
        delivery_date=data.delivery_date,
        delivery_method=data.delivery_method,
        delivery_address=data.delivery_address,
        note=data.note,
        expected_unit_price=data.expected_unit_price,
        idempotency_key=data.idempotency_key,
    )
    return order_detail(order.pk)


@orders_router.post("", response={201: OrderDetailOut})
async def place_order(request, data: OrderCreateIn):
    """
    Buy from an offer. Reserves the stock and asks the seller to confirm; the
    buyer pays once they have.
    """
    organization = writable_organization(request)
    offer = await aget_object_or_404(
        Offer.objects.filter(active_offer_q()).select_related(
            "organization", "product"
        ),
        public_id=data.offer_id,
    )
    return 201, await sync_to_async(_apply_place_order)(
        request, organization, offer, data
    )


@orders_router.get("/{order_id}", response=OrderDetailOut)
async def get_order(request, order_id: UUID):
    organization = readable_organization(request, OrgPermission.VIEW_MARKET)
    return await aget_object_or_404(
        order_relations(org_orders(organization)).prefetch_related("events"),
        public_id=order_id,
    )


# ── the seller ───────────────────────────────────────────────────────────────

@orders_router.post("/{order_id}/confirm", response=OrderDetailOut)
async def confirm_order(request, order_id: UUID, data: OrderConfirmIn):
    """
    Commit to the order. A contract delivery is charged to the buyer's wallet
    here, and `quantity` may differ within the contract's tolerance.
    """
    return await _as_provider(request, order_id, services.confirm_order, data.quantity)


@orders_router.post("/{order_id}/decline", response=OrderDetailOut)
async def decline_order(request, order_id: UUID, data: ReasonIn):
    return await _as_provider(request, order_id, services.decline_order, data.reason)


@orders_router.post("/{order_id}/dispatch", response=OrderDetailOut)
async def dispatch_order(request, order_id: UUID):
    return await _as_provider(request, order_id, services.dispatch_order)


@orders_router.post("/{order_id}/deliver", response=OrderDetailOut)
async def deliver_order(request, order_id: UUID):
    """Starts the buyer's acceptance window."""
    return await _as_provider(request, order_id, services.deliver_order)


@orders_router.post("/{order_id}/dispute/concede", response=OrderDetailOut)
async def concede_dispute(request, order_id: UUID):
    """Agree with the buyer: everything still held is refunded to them."""
    return await _as_provider(request, order_id, services.concede_dispute)


# ── the buyer ────────────────────────────────────────────────────────────────

def _apply_pay(request, organization, order: Order, method: str) -> dict:
    actor = services.Actor.from_request(request, organization, Side.CUSTOMER)
    payment = None
    if method == "wallet":
        services.pay_from_wallet(actor, order)
    else:
        payment = services.pay_through_gateway(actor, order)
    return {"order": order_detail(order.pk), "payment": payment}


@orders_router.post("/{order_id}/pay", response=OrderPayOut)
async def pay_order(request, order_id: UUID, data: OrderPayIn):
    """
    Pay from the wallet at once, or get a `checkout_url`. Either way the money
    stays in escrow until the buyer accepts the goods.
    """
    organization = caller_organization(request)
    require_perm(request, OrgPermission.MANAGE_BILLING)
    order = await _order_as(Side.CUSTOMER, organization, order_id)
    return await sync_to_async(_apply_pay)(request, organization, order, data.method)


@orders_router.post("/{order_id}/accept", response=OrderDetailOut)
async def accept_order(request, order_id: UUID):
    """Take the whole delivery. This pays the seller."""
    return await _as_customer(request, order_id, services.accept_order)


@orders_router.post("/{order_id}/reject", response=OrderDetailOut)
async def reject_order(request, order_id: UUID, data: OrderRejectIn):
    """
    Refuse all or part of a delivery. `accepted_quantity` is paid out now, and
    the rest stays held until the seller concedes or the platform decides.
    """
    return await _as_customer(
        request,
        order_id,
        services.reject_order,
        accepted_quantity=data.accepted_quantity,
        reason=data.reason,
        description=data.description,
    )


# ── either side ──────────────────────────────────────────────────────────────

@orders_router.post("/{order_id}/cancel", response=OrderDetailOut)
async def cancel_order(request, order_id: UUID, data: ReasonIn):
    """Before dispatch, by either side. A paid order is refunded in full."""
    organization = writable_organization(request)
    order = await aget_object_or_404(org_orders(organization), public_id=order_id)
    side = (
        Side.PROVIDER
        if order.provider_organization_id == organization.pk
        else Side.CUSTOMER
    )
    return await sync_to_async(_run)(
        request, organization, side, services.cancel_order, order, data.reason
    )


# ── platform staff ───────────────────────────────────────────────────────────

def _require_staff(request) -> None:
    if not request.auth.is_staff:
        raise Http404()


@disputes_router.get("", response=list[OrderDetailOut])
@paginate(LimitOffsetPagination)
async def list_open_disputes(request):
    """The queue. A 404 for anyone but platform staff."""
    _require_staff(request)
    return (
        order_relations(Order.objects.filter(dispute__status=OrderDispute.Status.OPEN))
        .prefetch_related("events")
        .order_by("dispute__created_at")
    )


def _apply_resolve(request, order: Order, data: DisputeResolveIn) -> Order:
    actor = services.Actor(
        user=request.auth,
        side=Side.PLATFORM,
        context={"source": "staff"},
    )
    result = services.resolve_dispute(
        actor, order, refund_amount=data.refund_amount, note=data.note
    )
    return order_detail(result.pk)


@disputes_router.post("/{order_id}/resolve", response=OrderDetailOut)
async def resolve_dispute(request, order_id: UUID, data: DisputeResolveIn):
    """Decide an open dispute: `refund_amount` to the buyer, the rest to the seller."""
    _require_staff(request)
    order = await aget_object_or_404(Order, public_id=order_id)
    return await sync_to_async(_apply_resolve)(request, order, data)
