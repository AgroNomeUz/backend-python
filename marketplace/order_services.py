"""
marketplace/order_services.py
Everything that moves an order, and with it stock and money.

Each operation locks the order row first, then re-reads its status, so two
requests cannot both act on it: a seller cancelling while the buyer pays, or the
job auto-accepting while the buyer rejects. Status changes go through `_move`
alone, which checks `Order.ALLOWED_TRANSITIONS`, records an `OrderEvent` and
writes the audit row, all in the same transaction as the money.

An order from a quote locks its purchase request next, before any money moves:
order → request → quote. `order_from_quote` goes request → quote but only ever
creates orders, never locks one, so the two cannot deadlock.

Services take an `Actor` rather than a request because the job runner performs
the same operations with nobody logged in.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja.errors import HttpError

from billing import services as billing
from billing.models import MONEY_DECIMAL_PLACES, MONEY_MAX_DIGITS, Payment
from core.audit import request_context
from core.models import ActivityLog
from deals.services import record_completed_sale, renew_completed_sale
from users.models import Organization, User

from .models import (
    DeliveryMethod,
    Offer,
    Order,
    OrderDispute,
    OrderEvent,
    Product,
    PurchaseRequest,
    Quote,
    Side,
    SupplyContract,
    active_offer_q,
    market_today,
)

ORDER_REFERENCE = "marketplace.order"
MONEY_LIMIT = Decimal(10) ** (MONEY_MAX_DIGITS - MONEY_DECIMAL_PLACES)
Status = Order.Status


@dataclass(frozen=True)
class Actor:
    """Who is doing this. `SYSTEM` is the job runner; staff have no organization."""

    user: User | None = None
    organization: Organization | None = None
    side: str = Side.PLATFORM
    context: dict = field(default_factory=dict)

    @classmethod
    def from_request(cls, request, organization, side: str) -> "Actor":
        return cls(request.auth, organization, side, request_context(request))


SYSTEM = Actor(context={"source": "system"})


def line_total(quantity: Decimal, unit_price: Decimal) -> Decimal:
    total = billing.money(quantity * unit_price)
    if total <= 0:
        raise HttpError(400, "That quantity is too small to price")
    if total >= MONEY_LIMIT:
        raise HttpError(400, "That order is too large")
    return total


def require_may_sell(product: Product, organization: Organization) -> None:
    """
    Every way into selling comes through here: publishing an offer, sending a
    quote, taking a quote and proposing a contract. Buying from an offer goes
    through `active_offer_q`, which holds the same rule.
    """
    if product.is_regulated and not organization.is_verified:
        raise HttpError(
            403, f"Only verified organizations can sell {product.name.lower()}."
        )


def audit(actor: Actor, target, action: str, changes: dict) -> None:
    """
    Record against the acting organization. With no organization, as for the job
    runner or platform staff, both sides get the row: it happened to them both.
    """
    if actor.organization is not None:
        organizations = [actor.organization]
    else:
        organizations = [target.provider_organization, target.customer_organization]
    for organization in organizations:
        ActivityLog.record(
            organization=organization,
            actor=actor.user,
            action=action,
            target=target,
            changes=changes,
            context=actor.context,
        )


def _move(order: Order, to_status: str, actor: Actor, note: str = "", **fields) -> None:
    from_status = order.status
    if not Order.can_transition(from_status, to_status):
        raise HttpError(
            409, f"This order is {order.get_status_display().lower()}; that is not possible now."
        )
    order.status = to_status
    for name, value in fields.items():
        setattr(order, name, value)
    order.save(update_fields=["status", "updated_at", *fields])
    _record(order, from_status, to_status, actor, note)
    audit(
        actor,
        order,
        ActivityLog.Action.STATUS_CHANGED,
        {"status": {"from": from_status, "to": to_status}},
    )


def _record(order, from_status, to_status, actor: Actor, note: str = "") -> None:
    OrderEvent.objects.create(
        order=order,
        from_status=from_status,
        to_status=to_status,
        side=actor.side,
        actor=actor.user,
        note=note,
    )


def _locked(order: Order) -> Order:
    return (
        Order.objects.select_for_update(of=("self",))
        .select_related("provider_organization", "customer_organization", "contract")
        .get(pk=order.pk)
    )


def after_now(hours: int) -> datetime:
    return timezone.now() + timedelta(hours=hours)


# ── placing ──────────────────────────────────────────────────────────────────

def reserve_stock(offer_id: int, quantity: Decimal) -> bool:
    """
    Take stock atomically. The `WHERE` is what stops overselling: of two buyers
    racing for the last 50 kg, the second matches no row.
    """
    return bool(
        Offer.objects.filter(
            pk=offer_id, status=Offer.Status.ACTIVE, quantity_available__gte=quantity
        ).update(quantity_available=F("quantity_available") - quantity)
    )


def _release_stock(order: Order) -> None:
    if order.offer_id:
        Offer.objects.filter(pk=order.offer_id).update(
            quantity_available=F("quantity_available") + order.quantity
        )


def place_order(
    actor: Actor,
    offer_id: UUID,
    *,
    quantity: Decimal,
    delivery_date,
    delivery_method: str,
    delivery_address: str = "",
    note: str = "",
    expected_unit_price: Decimal | None = None,
    idempotency_key: str = "",
) -> Order:
    customer = actor.organization
    if idempotency_key:
        existing = Order.objects.filter(
            customer_organization=customer, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            if existing.offer.public_id != offer_id:
                raise HttpError(409, "That idempotency key was used for another offer.")
            return existing

    offer = get_object_or_404(
        Offer.objects.filter(active_offer_q()).select_related("organization", "product"),
        public_id=offer_id,
    )
    if offer.organization_id == customer.pk:
        raise HttpError(400, "You cannot order from your own organization.")
    if expected_unit_price is not None and expected_unit_price != offer.price:
        raise HttpError(409, "The price of this offer has changed.")
    if quantity < offer.min_order_quantity:
        raise HttpError(
            400, f"The minimum order is {offer.min_order_quantity} {offer.unit}."
        )
    if not offer.available_from <= delivery_date <= offer.available_until:
        raise HttpError(
            400,
            f"This offer can be delivered from {offer.available_from} "
            f"to {offer.available_until}.",
        )
    if delivery_date < market_today():
        raise HttpError(400, "The delivery date has passed.")
    if delivery_method == DeliveryMethod.PICKUP and not offer.offers_pickup:
        raise HttpError(400, "This seller does not offer pickup.")
    if delivery_method == DeliveryMethod.SELLER_DELIVERY:
        if not offer.offers_delivery:
            raise HttpError(400, "This seller does not deliver.")
        if not delivery_address.strip():
            raise HttpError(400, "A delivery address is required.")

    subtotal = line_total(quantity, offer.price)

    try:
        with transaction.atomic():
            if not reserve_stock(offer.pk, quantity):
                raise HttpError(409, "There is not enough of this offer left.")
            order = Order.objects.create(
                provider_organization=offer.organization,
                customer_organization=customer,
                created_by=actor.user,
                offer=offer,
                product=offer.product,
                variety=offer.variety,
                grade=offer.grade,
                unit=offer.unit,
                unit_price=offer.price,
                currency=offer.currency,
                ordered_quantity=quantity,
                quantity=quantity,
                subtotal=subtotal,
                delivery_method=delivery_method,
                delivery_address=delivery_address,
                delivery_date=delivery_date,
                note=note,
                idempotency_key=idempotency_key,
                confirm_by=after_now(settings.ORDER_CONFIRM_HOURS),
            )
            _record(order, "", Status.PENDING_CONFIRMATION, actor)
            audit(
                actor,
                order,
                ActivityLog.Action.CREATED,
                {"subtotal": {"from": None, "to": str(subtotal)}},
            )
    except IntegrityError as exc:
        # Two taps with one key: the first won, and the rollback returned stock.
        if "order_customer_idempotency_unique" not in str(exc):
            raise
        return Order.objects.get(
            customer_organization=customer, idempotency_key=idempotency_key
        )
    return order


def order_from_quote(
    actor: Actor,
    quote: Quote,
    *,
    delivery_date,
    delivery_method: str,
    delivery_address: str = "",
) -> Order:
    """
    The buyer takes a quote. The seller committed by quoting, so this goes
    straight to payment. Several quotes can cover one request, which stays open
    until paid orders add up to what the buyer asked for: see `_hold_from_wallet`.
    """
    with transaction.atomic():
        # Request first: closing a request locks it and then touches its quotes.
        request = PurchaseRequest.objects.select_for_update().get(pk=quote.request_id)
        quote = (
            Quote.objects.select_for_update(of=("self",))
            .select_related("provider_organization", "request", "request__product")
            .get(pk=quote.pk)
        )
        if quote.status != Quote.Status.SUBMITTED:
            raise HttpError(409, "This quote is no longer open.")
        if quote.valid_until < market_today():
            raise HttpError(409, "This quote has expired.")
        if request.status != PurchaseRequest.Status.OPEN:
            raise HttpError(409, "This request is closed.")
        if request.is_recurring:
            raise HttpError(
                409, "This request is for regular supply: propose a contract instead."
            )
        require_may_sell(request.product, quote.provider_organization)
        if delivery_date < market_today():
            raise HttpError(400, "The delivery date has passed.")
        if not request.needed_from <= delivery_date <= request.needed_until:
            raise HttpError(
                400, f"This request needs delivery from {request.needed_from} "
                f"to {request.needed_until}."
            )
        if delivery_method == DeliveryMethod.SELLER_DELIVERY and not delivery_address.strip():
            raise HttpError(400, "A delivery address is required.")

        now = timezone.now()
        order = Order.objects.create(
            provider_organization=quote.provider_organization,
            customer_organization=actor.organization,
            created_by=actor.user,
            quote=quote,
            product=request.product,
            variety=quote.variety,
            grade=quote.grade,
            unit=request.unit,
            unit_price=quote.unit_price,
            ordered_quantity=quote.quantity,
            quantity=quote.quantity,
            subtotal=line_total(quote.quantity, quote.unit_price),
            delivery_method=delivery_method,
            delivery_address=delivery_address,
            delivery_date=delivery_date,
            status=Status.AWAITING_PAYMENT,
            confirmed_at=now,
            pay_by=after_now(settings.ORDER_PAYMENT_HOURS),
        )
        _record(order, "", Status.AWAITING_PAYMENT, actor)
        audit(
            actor,
            order,
            ActivityLog.Action.CREATED,
            {"subtotal": {"from": None, "to": str(order.subtotal)}},
        )

        quote.status = Quote.Status.ACCEPTED
        quote.save(update_fields=["status", "updated_at"])
    return order


def close_request(request: PurchaseRequest) -> None:
    """No more quotes are wanted, so anyone still waiting gets an answer."""
    request.status = PurchaseRequest.Status.CLOSED
    request.save(update_fields=["status", "updated_at"])
    request.quotes.filter(status=Quote.Status.SUBMITTED).update(
        status=Quote.Status.REJECTED, updated_at=timezone.now()
    )


def _locked_request(order: Order) -> PurchaseRequest:
    return PurchaseRequest.objects.select_for_update(of=("self",)).get(quotes=order.quote_id)


def _reopen_quote(order: Order, actor: Actor) -> None:
    """
    An order that ended unpaid used up none of the request, so its quote can be
    taken again, unless the request has closed, the quote has lapsed or the seller
    has sent a newer one.
    """
    request = _locked_request(order)
    newer = request.quotes.filter(
        provider_organization=order.provider_organization, status=Quote.Status.SUBMITTED
    )
    if request.status != PurchaseRequest.Status.OPEN or newer.exists():
        return
    reopened = Quote.objects.filter(
        pk=order.quote_id, valid_until__gte=market_today()
    ).update(status=Quote.Status.SUBMITTED, updated_at=timezone.now())
    if reopened:
        audit(
            actor,
            order,
            ActivityLog.Action.UPDATED,
            {"quote": {"from": Quote.Status.ACCEPTED, "to": Quote.Status.SUBMITTED}},
        )


# ── the seller's answer ──────────────────────────────────────────────────────

def _commission_rate(order: Order) -> Decimal:
    if order.contract_id:
        return settings.BILLING_CONTRACT_COMMISSION_RATE
    return settings.BILLING_COMMISSION_RATE


def _hold_from_wallet(order: Order, actor: Actor) -> None:
    """
    Move the order to `paid`. Raises `InsufficientFunds` if the wallet is short.
    Closes the order's purchase request if paid orders now cover it.
    """
    request = _locked_request(order) if order.quote_id else None
    hold = billing.create_hold(
        payer=order.customer_organization,
        payee=order.provider_organization,
        amount=order.subtotal,
        commission_rate=_commission_rate(order),
        reference_type=ORDER_REFERENCE,
        reference_id=order.public_id,
    )
    _move(order, Status.PAID, actor, hold=hold, paid_at=timezone.now())
    if request is not None and request.status == PurchaseRequest.Status.OPEN:
        covered = (
            Order.objects.filter(quote__request=request, paid_at__isnull=False)
            .exclude(status=Status.CANCELLED)
            .aggregate(total=Sum("quantity"))["total"]
        )
        if covered >= request.quantity:
            close_request(request)


@transaction.atomic
def confirm_order(actor: Actor, order: Order, quantity: Decimal | None = None) -> Order:
    """
    The seller commits. For a contract delivery this also charges the buyer's
    wallet, because signing the contract was the instruction to do so, and it is
    the only place the quantity can move, within the agreed tolerance.
    """
    order = _locked(order)
    if order.status != Status.PENDING_CONFIRMATION:
        raise HttpError(409, "This order has already been answered.")

    if quantity is not None and quantity != order.quantity:
        if not order.contract_id:
            raise HttpError(400, "Only a contract delivery can change quantity.")
        tolerance = order.contract_terms.tolerance_percent / Decimal(100)
        low = order.ordered_quantity * (1 - tolerance)
        high = order.ordered_quantity * (1 + tolerance)
        if not low <= quantity <= high:
            raise HttpError(
                400, f"The contract allows between {low.normalize():f} and {high.normalize():f}."
            )
        order.quantity = quantity
        order.subtotal = line_total(quantity, order.unit_price)
        order.save(update_fields=["quantity", "subtotal"])

    _move(
        order,
        Status.AWAITING_PAYMENT,
        actor,
        confirmed_at=timezone.now(),
        pay_by=after_now(settings.ORDER_PAYMENT_HOURS),
    )
    if order.contract_id:
        fund_contract_delivery(order)
    return order


def fund_contract_delivery(order: Order) -> bool:
    """
    Try the wallet. A short wallet is not an error: the order waits in
    `awaiting_payment` like any other, so one low balance does not sink the rest.
    """
    try:
        with transaction.atomic():
            _hold_from_wallet(order, SYSTEM)
    except billing.InsufficientFunds:
        order.refresh_from_db()
        return False
    return True


@transaction.atomic
def retry_contract_funding(order: Order) -> None:
    """For the job runner: the buyer may have topped up since the delivery was confirmed."""
    order = _locked(order)
    if order.status == Status.AWAITING_PAYMENT:
        fund_contract_delivery(order)


def _close_without_delivery(
    order: Order, to_status: str, actor: Actor, *, reason: str, cancelled_by: str
) -> None:
    """The ways an order ends unfulfilled. Stock, money and any unpaid quote go back."""
    if order.hold_id:
        billing.refund_remaining(order.hold)
    _release_stock(order)
    _move(
        order,
        to_status,
        actor,
        note=reason,
        cancelled_by=cancelled_by,
        cancel_reason=reason,
    )
    if order.quote_id and not order.hold_id:
        _reopen_quote(order, actor)


@transaction.atomic
def decline_order(actor: Actor, order: Order, reason: str = "") -> Order:
    order = _locked(order)
    if order.status != Status.PENDING_CONFIRMATION:
        raise HttpError(409, "This order has already been answered.")
    _close_without_delivery(
        order, Status.DECLINED, actor, reason=reason, cancelled_by=Side.PROVIDER
    )
    return order


@transaction.atomic
def cancel_order(actor: Actor, order: Order, reason: str = "") -> Order:
    """Either side, any time before dispatch. A paid order is refunded in full."""
    order = _locked(order)
    if order.status in (Status.DISPATCHED, Status.DELIVERED):
        raise HttpError(409, "This order has left the seller. Reject it instead.")
    _close_without_delivery(
        order, Status.CANCELLED, actor, reason=reason, cancelled_by=actor.side
    )
    return order


# ── paying ───────────────────────────────────────────────────────────────────

def _require_payable(order: Order) -> None:
    if order.status != Status.AWAITING_PAYMENT:
        raise HttpError(409, "This order is not waiting for payment.")


def pay_from_wallet(actor: Actor, order: Order) -> Order:
    with transaction.atomic():
        order = _locked(order)
        _require_payable(order)
        _hold_from_wallet(order, actor)
    return order


def pay_through_gateway(actor: Actor, order: Order) -> Payment:
    """
    Open a checkout. Outside any transaction, as `start_payment` requires. The
    order itself moves when the provider's event arrives: see
    `order_payment_received`.
    """
    with transaction.atomic():
        _require_payable(_locked(order))
    return billing.start_payment(
        organization=order.customer_organization,
        amount=order.subtotal,
        purpose=Payment.Purpose.ORDER,
        user=actor.user,
        reference_type=ORDER_REFERENCE,
        reference_id=order.public_id,
    )


def order_payment_received(payment: Payment) -> None:
    """
    Called inside the transaction that credited the wallet.

    If the order has moved on, for instance it expired while the buyer was at the
    checkout, the money simply stays in their wallet. That is why every hold is
    funded from the wallet: a late payment can never be lost or strand an order.
    """
    if payment.reference_type != ORDER_REFERENCE or payment.reference_id is None:
        return
    order = Order.objects.filter(public_id=payment.reference_id).first()
    if order is None:
        return
    order = _locked(order)
    if order.status != Status.AWAITING_PAYMENT:
        return
    try:
        _hold_from_wallet(order, Actor(side=Side.CUSTOMER, context={"source": "payment"}))
    except billing.InsufficientFunds:
        # Only reachable if the amount no longer covers the order. Leave it.
        pass


# ── delivery ─────────────────────────────────────────────────────────────────

def _deadline_after_dispatch(delivery_date) -> datetime:
    """
    The end of the delivery day, plus the usual window.

    Dispatched goods need a clock too. A seller who forgets to press "delivered"
    and a buyer who says nothing would otherwise leave the money in escrow for
    good, with neither side able to cancel.
    """
    zone = ZoneInfo(settings.MARKET_TIME_ZONE)
    end_of_day = datetime.combine(delivery_date + timedelta(days=1), time.min, tzinfo=zone)
    return max(end_of_day, timezone.now()) + timedelta(
        hours=settings.ORDER_ACCEPTANCE_HOURS
    )


@transaction.atomic
def dispatch_order(actor: Actor, order: Order) -> Order:
    order = _locked(order)
    _move(
        order,
        Status.DISPATCHED,
        actor,
        dispatched_at=timezone.now(),
        acceptance_deadline=_deadline_after_dispatch(order.delivery_date),
    )
    return order


@transaction.atomic
def deliver_order(actor: Actor, order: Order) -> Order:
    order = _locked(order)
    if order.status != Status.DISPATCHED:
        raise HttpError(409, "This order has not been dispatched.")
    now = timezone.now()
    _move(
        order,
        Status.DELIVERED,
        actor,
        delivered_at=now,
        # Never earlier than dispatch set it. Otherwise a seller could press
        # this days before the delivery date and be paid before anything
        # was due, while the buyer has no reason to look.
        acceptance_deadline=max(
            order.acceptance_deadline,
            now + timedelta(hours=settings.ORDER_ACCEPTANCE_HOURS),
        ),
    )
    return order


def _record_deal(order: Order) -> None:
    """
    A sale that actually paid out appears on the seller's profile, as one deal
    per one-time order or one deal for a whole contract.
    """
    if order.hold.released_amount <= 0:
        return
    if order.contract_id:
        # Locked and read fresh: two deliveries settling at once must make one
        # deal. Always order first, then contract, so this cannot deadlock.
        contract = SupplyContract.objects.select_for_update().get(pk=order.contract_id)
        if contract.deal_id is None:
            contract.deal = record_completed_sale(
                provider=order.provider_organization,
                customer=order.customer_organization,
            )
            contract.save(update_fields=["deal", "updated_at"])
        else:
            renew_completed_sale(contract.deal_id)
    else:
        order.deal = record_completed_sale(
            provider=order.provider_organization,
            customer=order.customer_organization,
        )
        order.save(update_fields=["deal"])


def _settle(order: Order, actor: Actor, note: str = "", **fields) -> None:
    order.hold.refresh_from_db()
    _record_deal(order)
    _move(order, Status.SETTLED, actor, note=note, settled_at=timezone.now(), **fields)


@transaction.atomic
def accept_order(actor: Actor, order: Order) -> Order:
    """
    The buyer takes the whole delivery, and the seller is paid.

    Also what the job runner calls when the window lapses. The status is read
    again under the lock, because the buyer may have rejected a moment before.
    """
    order = _locked(order)
    if order.status not in (Status.DISPATCHED, Status.DELIVERED):
        raise HttpError(409, "This order is not waiting for acceptance.")
    billing.release_hold(order.hold, order.subtotal)
    _settle(order, actor, accepted_quantity=order.quantity)
    return order


@transaction.atomic
def reject_order(
    actor: Actor,
    order: Order,
    *,
    accepted_quantity: Decimal,
    reason: str,
    description: str = "",
) -> Order:
    """
    The buyer refuses all or part of a delivery.

    The accepted share is paid out now, since nothing is in dispute about it. The
    rest stays held until the seller concedes or staff decide.
    """
    order = _locked(order)
    if order.status not in (Status.DISPATCHED, Status.DELIVERED):
        raise HttpError(409, "This order is not waiting for acceptance.")
    if order.acceptance_deadline and timezone.now() > order.acceptance_deadline:
        raise HttpError(409, "The time to reject this delivery has passed.")
    if accepted_quantity >= order.quantity:
        raise HttpError(400, "That is the whole delivery: accept it instead.")

    accepted_value = billing.money(accepted_quantity * order.unit_price)
    disputed_amount = order.subtotal - accepted_value
    if disputed_amount <= 0:
        raise HttpError(400, "That leaves nothing in dispute: accept it instead.")

    if accepted_value > 0:
        billing.release_hold(order.hold, accepted_value)
    OrderDispute.objects.create(
        order=order,
        reason=reason,
        description=description,
        disputed_amount=disputed_amount,
        opened_by=actor.user,
    )
    _move(
        order,
        Status.DISPUTED,
        actor,
        note=description,
        accepted_quantity=accepted_quantity,
    )
    return order


def _open_dispute(order: Order) -> OrderDispute:
    dispute = (
        OrderDispute.objects.select_for_update()
        .filter(order=order, status=OrderDispute.Status.OPEN)
        .first()
    )
    if dispute is None or order.status != Status.DISPUTED:
        raise HttpError(409, "This order has no open dispute.")
    return dispute


@transaction.atomic
def concede_dispute(actor: Actor, order: Order) -> Order:
    """The seller agrees with the buyer: everything still held goes back."""
    order = _locked(order)
    dispute = _open_dispute(order)
    billing.refund_hold(order.hold, dispute.disputed_amount)
    dispute.status = OrderDispute.Status.CONCEDED
    dispute.refund_amount = dispute.disputed_amount
    dispute.resolved_by = actor.user
    dispute.resolved_at = timezone.now()
    dispute.save(update_fields=["status", "refund_amount", "resolved_by", "resolved_at"])
    _settle(order, actor, note="The seller conceded the dispute.")
    return order


@transaction.atomic
def resolve_dispute(
    actor: Actor, order: Order, *, refund_amount: Decimal, note: str
) -> Order:
    """Platform staff split what is held. Any share not refunded goes to the seller."""
    order = _locked(order)
    dispute = _open_dispute(order)
    if refund_amount > dispute.disputed_amount:
        raise HttpError(400, f"Only {dispute.disputed_amount} is in dispute.")

    to_seller = dispute.disputed_amount - refund_amount
    if refund_amount > 0:
        billing.refund_hold(order.hold, refund_amount)
    if to_seller > 0:
        billing.release_hold(order.hold, to_seller)

    dispute.status = OrderDispute.Status.RESOLVED
    dispute.refund_amount = refund_amount
    dispute.resolved_by = actor.user
    dispute.resolution_note = note
    dispute.resolved_at = timezone.now()
    dispute.save(
        update_fields=[
            "status",
            "refund_amount",
            "resolved_by",
            "resolution_note",
            "resolved_at",
        ]
    )
    # Whatever the seller was paid for counts as accepted, for the fill rate.
    awarded = (to_seller / order.unit_price).quantize(Decimal("0.001"))
    accepted = min(order.quantity, (order.accepted_quantity or 0) + awarded)
    _settle(order, actor, note=note, accepted_quantity=accepted)
    return order


# ── deadlines ────────────────────────────────────────────────────────────────

@transaction.atomic
def expire_unconfirmed(order: Order) -> None:
    order = _locked(order)
    if order.status == Status.PENDING_CONFIRMATION:
        _close_without_delivery(
            order,
            Status.EXPIRED,
            SYSTEM,
            reason="The seller did not confirm in time.",
            cancelled_by=Side.PROVIDER,
        )


@transaction.atomic
def expire_unpaid(order: Order) -> None:
    order = _locked(order)
    if order.status == Status.AWAITING_PAYMENT:
        _close_without_delivery(
            order,
            Status.EXPIRED,
            SYSTEM,
            reason="The buyer did not pay in time.",
            cancelled_by=Side.CUSTOMER,
        )


@transaction.atomic
def refund_undispatched(order: Order) -> None:
    """Paid for and never sent: the buyer's money must not wait on them noticing."""
    order = _locked(order)
    if order.status == Status.PAID:
        _close_without_delivery(
            order,
            Status.CANCELLED,
            SYSTEM,
            reason="The seller did not dispatch in time.",
            cancelled_by=Side.PROVIDER,
        )


@transaction.atomic
def apply_contract_terms(order: Order, terms) -> bool:
    """Move an unconfirmed delivery onto newer terms. Returns whether it changed."""
    order = _locked(order)
    if order.status != Status.PENDING_CONFIRMATION or order.contract_terms_id == terms.pk:
        return False
    old_price = order.unit_price
    order.contract_terms = terms
    order.unit_price = terms.unit_price
    order.ordered_quantity = terms.quantity_per_delivery
    order.quantity = terms.quantity_per_delivery
    order.subtotal = line_total(terms.quantity_per_delivery, terms.unit_price)
    order.delivery_method = terms.delivery_method
    order.delivery_address = terms.delivery_address
    order.save(
        update_fields=[
            "contract_terms",
            "unit_price",
            "ordered_quantity",
            "quantity",
            "subtotal",
            "delivery_method",
            "delivery_address",
            "updated_at",
        ]
    )
    _record(order, order.status, order.status, SYSTEM, f"Updated to version {terms.version}.")
    audit(
        SYSTEM,
        order,
        ActivityLog.Action.UPDATED,
        {"unit_price": {"from": str(old_price), "to": str(terms.unit_price)}},
    )
    return True


def confirm_deadline(delivery_date) -> datetime:
    """A contract delivery has to be confirmed before its day starts."""
    zone = ZoneInfo(settings.MARKET_TIME_ZONE)
    deadline = datetime.combine(delivery_date, time.min, tzinfo=zone)
    if deadline <= timezone.now():
        return after_now(settings.ORDER_CONFIRM_HOURS)
    return deadline


def contract_order_values(contract, terms, delivery_date) -> dict:
    """The fields of one delivery, copied from the terms that govern its date."""
    return {
        "provider_organization": contract.provider_organization,
        "customer_organization": contract.customer_organization,
        "contract": contract,
        "contract_terms": terms,
        "product": contract.product,
        "variety": contract.variety,
        "grade": contract.grade,
        "unit": contract.unit,
        "unit_price": terms.unit_price,
        "currency": contract.currency,
        "ordered_quantity": terms.quantity_per_delivery,
        "quantity": terms.quantity_per_delivery,
        "subtotal": line_total(terms.quantity_per_delivery, terms.unit_price),
        "delivery_method": terms.delivery_method,
        "delivery_address": terms.delivery_address,
        "delivery_date": delivery_date,
        "confirm_by": confirm_deadline(delivery_date),
    }


def record_generated_order(order: Order) -> None:
    _record(order, "", Status.PENDING_CONFIRMATION, SYSTEM)

