"""
billing/services.py
Every movement of money. Other apps call these and never touch the ledger.

Locking, always in this order, so two operations cannot deadlock:

    the caller's own row (an order) → EscrowHold / Payment / Payout → wallets

A wallet is locked before its balance is read for a debit, so two debits cannot
both pass the check. Credits need no lock: they cannot overdraw anything. The
platform accounts are never locked, because that would put every payment on the
platform in one queue, and each hold's row already serialises its own escrow.

`BillingError` carries an HTTP status so a view can pass it straight on, but
nothing here imports ninja: the job runner calls these too.
"""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from users.models import Organization

from . import gateway as gateways
from .models import (
    EscrowHold,
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
    Payment,
    Payout,
    PayoutDestination,
    WebhookEvent,
)
from .signals import payment_succeeded

CENTS = Decimal("0.01")
# A row with no checkout younger than this is a double tap still being served.
CHECKOUT_ABANDONED_AFTER = timedelta(minutes=1)
Kind = LedgerTransaction.Kind


class BillingError(Exception):
    status = 400

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status


class InsufficientFunds(BillingError):
    status = 409


class EventNotReady(Exception):
    """
    The provider told us about something we cannot find yet.

    Providers can answer faster than we can save their reference. Consuming the
    event would lose the only news we will get, so it is left unprocessed and the
    provider is told to send it again.
    """


def money(value) -> Decimal:
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


# ── accounts and balances ────────────────────────────────────────────────────

def wallet_for(organization: Organization) -> LedgerAccount:
    account, _ = LedgerAccount.objects.get_or_create(
        kind=LedgerAccount.Kind.WALLET,
        organization=organization,
        currency=settings.BILLING_CURRENCY,
    )
    return account


def platform_account(kind: str) -> LedgerAccount:
    account, _ = LedgerAccount.objects.get_or_create(
        kind=kind, organization=None, currency=settings.BILLING_CURRENCY
    )
    return account


def balance_of(account: LedgerAccount) -> Decimal:
    total = LedgerEntry.objects.filter(account=account).aggregate(total=Sum("amount"))
    return total["total"] or Decimal("0.00")


def wallet_balance(organization: Organization) -> Decimal:
    return balance_of(wallet_for(organization))


def held_for(organization: Organization) -> Decimal:
    """What this organization has tied up in open holds."""
    open_holds = EscrowHold.objects.filter(
        payer_organization=organization, status=EscrowHold.Status.HELD
    )
    return sum((hold.remaining for hold in open_holds), Decimal("0.00"))


def _locked_wallet(organization: Organization) -> LedgerAccount:
    account = wallet_for(organization)
    return LedgerAccount.objects.select_for_update().get(pk=account.pk)


def _post(
    kind: str,
    legs: list[tuple[LedgerAccount, Decimal]],
    idempotency_key: str,
    *,
    hold: EscrowHold | None = None,
    payment: Payment | None = None,
    payout: Payout | None = None,
) -> LedgerTransaction:
    """
    Write one balanced transaction, or return the one this key already made.

    The sum is checked here for a readable failure; the deferred trigger is what
    actually guarantees it. Zero legs are dropped, since a commission can round
    to nothing and `ledgerentry_amount_not_zero` refuses them.
    """
    legs = [(account, money(amount)) for account, amount in legs if money(amount) != 0]
    if sum(amount for _, amount in legs) != 0:
        raise ValueError(f"Unbalanced ledger transaction: {legs}")

    existing = LedgerTransaction.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing

    try:
        with transaction.atomic():
            ledger_transaction = LedgerTransaction.objects.create(
                kind=kind,
                idempotency_key=idempotency_key,
                hold=hold,
                payment=payment,
                payout=payout,
            )
            LedgerEntry.objects.bulk_create(
                LedgerEntry(transaction=ledger_transaction, account=account, amount=amount)
                for account, amount in legs
            )
    except IntegrityError:
        # Lost a race on `idempotency_key`; the winner did exactly this.
        return LedgerTransaction.objects.get(idempotency_key=idempotency_key)
    return ledger_transaction


# ── escrow ───────────────────────────────────────────────────────────────────

@transaction.atomic
def create_hold(
    *,
    payer: Organization,
    payee: Organization,
    amount: Decimal,
    commission_rate: Decimal,
    reference_type: str,
    reference_id,
) -> EscrowHold:
    """Move `amount` from the payer's wallet into escrow."""
    amount = money(amount)
    if amount <= 0:
        raise BillingError("A hold must be for a positive amount")

    wallet = _locked_wallet(payer)
    existing = EscrowHold.objects.filter(
        reference_type=reference_type, reference_id=reference_id
    ).first()
    if existing is not None:
        return existing

    if balance_of(wallet) < amount:
        raise InsufficientFunds("Not enough money in the wallet")

    hold = EscrowHold.objects.create(
        payer_organization=payer,
        payee_organization=payee,
        amount=amount,
        commission_rate=commission_rate,
        currency=settings.BILLING_CURRENCY,
        reference_type=reference_type,
        reference_id=reference_id,
    )
    _post(
        Kind.HOLD,
        [(wallet, -amount), (platform_account(LedgerAccount.Kind.ESCROW), amount)],
        f"hold:{hold.pk}",
        hold=hold,
    )
    return hold


def _locked_hold(hold: EscrowHold) -> EscrowHold:
    return EscrowHold.objects.select_for_update().get(pk=hold.pk)


def _check_spend(hold: EscrowHold, amount: Decimal) -> None:
    if amount <= 0:
        raise BillingError("The amount must be positive")
    if amount > hold.remaining:
        raise BillingError(
            f"Only {hold.remaining} remains in this hold", status=409
        )


def _close_if_spent(hold: EscrowHold) -> None:
    if hold.remaining == 0:
        hold.status = EscrowHold.Status.SETTLED
        hold.settled_at = timezone.now()


@transaction.atomic
def release_hold(hold: EscrowHold, amount: Decimal) -> EscrowHold:
    """
    Pay `amount` of the hold to the payee, less commission.

    Commission is worked out per release, on what is released, so goods the
    buyer rejected earn the platform nothing.
    """
    hold = _locked_hold(hold)
    amount = money(amount)
    _check_spend(hold, amount)

    commission = money(amount * hold.commission_rate)
    sequence = hold.transactions.filter(kind=Kind.RELEASE).count()
    _post(
        Kind.RELEASE,
        [
            (platform_account(LedgerAccount.Kind.ESCROW), -amount),
            (wallet_for(hold.payee_organization), amount - commission),
            (platform_account(LedgerAccount.Kind.COMMISSION), commission),
        ],
        f"hold:{hold.pk}:release:{sequence}",
        hold=hold,
    )
    hold.released_amount += amount
    hold.commission_amount += commission
    _close_if_spent(hold)
    hold.save(
        update_fields=["released_amount", "commission_amount", "status", "settled_at"]
    )
    return hold


@transaction.atomic
def refund_hold(hold: EscrowHold, amount: Decimal) -> EscrowHold:
    """Return `amount` of the hold to the payer's wallet."""
    hold = _locked_hold(hold)
    amount = money(amount)
    _check_spend(hold, amount)

    sequence = hold.transactions.filter(kind=Kind.REFUND).count()
    _post(
        Kind.REFUND,
        [
            (platform_account(LedgerAccount.Kind.ESCROW), -amount),
            (wallet_for(hold.payer_organization), amount),
        ],
        f"hold:{hold.pk}:refund:{sequence}",
        hold=hold,
    )
    hold.refunded_amount += amount
    _close_if_spent(hold)
    hold.save(update_fields=["refunded_amount", "status", "settled_at"])
    return hold


def refund_remaining(hold: EscrowHold) -> EscrowHold:
    remaining = EscrowHold.objects.get(pk=hold.pk).remaining
    return refund_hold(hold, remaining) if remaining > 0 else hold


# ── payments in ──────────────────────────────────────────────────────────────

def start_payment(
    *,
    organization: Organization,
    amount: Decimal,
    purpose: str,
    user=None,
    reference_type: str = "",
    reference_id=None,
    idempotency_key: str = "",
    on_created=None,
) -> Payment:
    """
    Open a checkout with the provider.

    Called outside any transaction on purpose: a network call must not hold row
    locks. If the provider call fails the row is marked failed, so nothing is
    left pending and the buyer can simply try again. A process that died before
    the provider call leaves a pending row with no checkout, and the next attempt
    finishes it. `on_created` runs in the transaction that creates the row, and
    not at all on a replay: it is where a caller writes its audit row.
    """
    amount = money(amount)
    if amount <= 0:
        raise BillingError("The amount must be positive")
    try:
        gateway = gateways.get_gateway()
    except gateways.GatewayUnavailable as exc:
        raise BillingError(str(exc), status=503)

    if idempotency_key:
        existing = Payment.objects.filter(
            organization=organization, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            _check_same_request(existing.amount, amount)
            return _finish_if_abandoned(existing, gateway)

    try:
        with transaction.atomic():
            payment = Payment.objects.create(
                organization=organization,
                created_by=user,
                amount=amount,
                purpose=purpose,
                currency=settings.BILLING_CURRENCY,
                provider=gateway.name,
                reference_type=reference_type,
                reference_id=reference_id,
                idempotency_key=idempotency_key,
            )
            if on_created:
                on_created(payment)
    except IntegrityError:
        # A concurrent duplicate won one of the two unique constraints.
        existing = _open_duplicate(organization, reference_type, reference_id, idempotency_key)
        if existing is None:
            raise
        _check_same_request(existing.amount, amount)
        return _finish_if_abandoned(existing, gateway)

    return _open_checkout(payment, gateway)


def _finish_if_abandoned(payment: Payment, gateway) -> Payment:
    if (
        payment.status != Payment.Status.PENDING
        or payment.provider_reference
        or timezone.now() - payment.created_at < CHECKOUT_ABANDONED_AFTER
    ):
        return payment
    return _open_checkout(payment, gateway)


def _open_checkout(payment: Payment, gateway) -> Payment:
    try:
        checkout = gateway.create_checkout(payment)
    except Exception as exc:
        # Any failure, not only a refusal: with no provider reference this row
        # can never complete, and left pending it would block every retry.
        payment.status = Payment.Status.FAILED
        payment.failure_reason = str(exc)[:255]
        payment.completed_at = timezone.now()
        payment.save(update_fields=["status", "failure_reason", "completed_at"])
        if isinstance(exc, gateways.GatewayError):
            raise BillingError("The payment provider refused the request", status=502)
        raise

    # First one wins: two retries can both finish a row, and overwriting a
    # reference would leave a buyer at a checkout whose event matches nothing.
    Payment.objects.filter(pk=payment.pk, provider_reference="").update(
        provider_reference=checkout.provider_reference,
        checkout_url=checkout.checkout_url,
    )
    payment.refresh_from_db()
    return payment


def _check_same_request(existing_amount: Decimal, amount: Decimal) -> None:
    if existing_amount != amount:
        raise BillingError(
            "A payment already exists for this request with a different amount",
            status=409,
        )


def _open_duplicate(organization, reference_type, reference_id, idempotency_key):
    if idempotency_key:
        found = Payment.objects.filter(
            organization=organization, idempotency_key=idempotency_key
        ).first()
        if found is not None:
            return found
    if reference_id is not None:
        return Payment.objects.filter(
            reference_type=reference_type,
            reference_id=reference_id,
            status=Payment.Status.PENDING,
        ).first()
    return None


@transaction.atomic
def _complete_payment(event: gateways.GatewayEvent, provider: str) -> str:
    payment = (
        Payment.objects.select_for_update()
        .filter(provider=provider, provider_reference=event.provider_reference)
        .first()
    )
    if payment is None:
        raise EventNotReady(f"No payment with reference {event.provider_reference}")
    if payment.status == Payment.Status.SUCCEEDED:
        # A success followed by "failed" is a chargeback. Nothing here can undo
        # money that may already be spent or paid out, so it waits for a person.
        return f"ignored: payment already succeeded, event was {event.event_type}"
    if event.amount != payment.amount:
        return f"rejected: amount {event.amount} does not match {payment.amount}"

    if event.event_type == gateways.PAYMENT_FAILED:
        if payment.status == Payment.Status.FAILED:
            return "already failed"
        payment.status = Payment.Status.FAILED
        payment.failure_reason = event.reason[:255]
        payment.completed_at = timezone.now()
        payment.save(update_fields=["status", "failure_reason", "completed_at"])
        return "failed"

    # From `pending`, or from `failed`: a provider that reports a failure and
    # then the success really did take the money, and it has to be credited.
    payment.status = Payment.Status.SUCCEEDED
    payment.completed_at = timezone.now()
    payment.save(update_fields=["status", "completed_at"])
    _post(
        Kind.DEPOSIT,
        [
            (platform_account(LedgerAccount.Kind.GATEWAY), -payment.amount),
            (wallet_for(payment.organization), payment.amount),
        ],
        f"payment:{payment.pk}",
        payment=payment,
    )
    payment_succeeded.send(sender=Payment, payment=payment)
    return "succeeded"


# ── payouts ──────────────────────────────────────────────────────────────────

def add_destination(
    *,
    organization: Organization,
    user,
    kind: str,
    label: str,
    holder_name: str,
    account_number: str,
    on_created=None,
) -> PayoutDestination:
    try:
        token = gateways.get_gateway().tokenize_destination(kind, account_number)
    except gateways.GatewayUnavailable as exc:
        raise BillingError(str(exc), status=503)
    except gateways.GatewayError:
        raise BillingError("The payment provider refused that account", status=502)
    with transaction.atomic():
        destination = PayoutDestination.objects.create(
            organization=organization,
            created_by=user,
            kind=kind,
            label=label,
            holder_name=holder_name,
            last4=account_number[-4:],
            provider_token=token,
            usable_from=timezone.now()
            + timedelta(hours=settings.BILLING_PAYOUT_COOLDOWN_HOURS),
        )
        if on_created:
            on_created(destination)
    return destination


def request_payout(
    *,
    organization: Organization,
    destination: PayoutDestination,
    amount: Decimal,
    user=None,
    idempotency_key: str = "",
    on_created=None,
) -> Payout:
    """
    Take the money out of the wallet, then ask the provider to send it.

    Two steps, and the wallet is debited first, because the opposite order can
    pay twice. A provider failure puts the money straight back.
    """
    amount = money(amount)
    if amount <= 0:
        raise BillingError("The amount must be positive")
    if not destination.is_active:
        raise BillingError("That payout account has been removed")
    if destination.usable_from > timezone.now():
        raise BillingError(
            "A new payout account can receive money from "
            f"{destination.usable_from.isoformat()}",
            status=409,
        )
    try:
        gateway = gateways.get_gateway()
    except gateways.GatewayUnavailable as exc:
        raise BillingError(str(exc), status=503)

    with transaction.atomic():
        wallet = _locked_wallet(organization)
        if idempotency_key:
            existing = Payout.objects.filter(
                organization=organization, idempotency_key=idempotency_key
            ).first()
            if existing is not None:
                _check_same_request(existing.amount, amount)
                return existing
        if balance_of(wallet) < amount:
            raise InsufficientFunds("Not enough money in the wallet")
        payout = Payout.objects.create(
            organization=organization,
            destination=destination,
            requested_by=user,
            amount=amount,
            currency=settings.BILLING_CURRENCY,
            provider=gateway.name,
            idempotency_key=idempotency_key,
        )
        _post(
            Kind.PAYOUT,
            [
                (wallet, -amount),
                (platform_account(LedgerAccount.Kind.PAYOUTS_IN_TRANSIT), amount),
            ],
            f"payout:{payout.pk}",
            payout=payout,
        )
        if on_created:
            on_created(payout)

    try:
        payout.provider_reference = gateway.send_payout(payout)
    except gateways.GatewayError as exc:
        _fail_payout(payout, str(exc))
        raise BillingError("The payment provider refused the payout", status=502)
    payout.status = Payout.Status.PROCESSING
    payout.save(update_fields=["provider_reference", "status"])
    return payout


@transaction.atomic
def _fail_payout(payout: Payout, reason: str) -> None:
    payout = Payout.objects.select_for_update().get(pk=payout.pk)
    _post(
        Kind.PAYOUT_REVERSED,
        [
            (platform_account(LedgerAccount.Kind.PAYOUTS_IN_TRANSIT), -payout.amount),
            (wallet_for(payout.organization), payout.amount),
        ],
        f"payout:{payout.pk}:reversed",
        payout=payout,
    )
    payout.status = Payout.Status.FAILED
    payout.failure_reason = reason[:255]
    payout.completed_at = timezone.now()
    payout.save(update_fields=["status", "failure_reason", "completed_at"])


@transaction.atomic
def _complete_payout(event: gateways.GatewayEvent, provider: str) -> str:
    payout = (
        Payout.objects.select_for_update()
        .filter(provider=provider, provider_reference=event.provider_reference)
        .first()
    )
    if payout is None:
        raise EventNotReady(f"No payout with reference {event.provider_reference}")
    if payout.status != Payout.Status.PROCESSING:
        return f"ignored: payout is {payout.status}"
    if event.amount != payout.amount:
        return f"rejected: amount {event.amount} does not match {payout.amount}"

    if event.event_type == gateways.PAYOUT_FAILED:
        _fail_payout(payout, event.reason)
        return "failed"

    _post(
        Kind.PAYOUT_SETTLED,
        [
            (platform_account(LedgerAccount.Kind.PAYOUTS_IN_TRANSIT), -payout.amount),
            (platform_account(LedgerAccount.Kind.GATEWAY), payout.amount),
        ],
        f"payout:{payout.pk}:settled",
        payout=payout,
    )
    payout.status = Payout.Status.PAID
    payout.completed_at = timezone.now()
    payout.save(update_fields=["status", "completed_at"])
    return "paid"


# ── provider events ──────────────────────────────────────────────────────────

def process_event(provider: str, event: gateways.GatewayEvent) -> tuple[str, bool]:
    """
    Apply one verified event, once. Returns (outcome, was_duplicate).

    The dedupe row and the event's effect share a transaction. If the effect
    raises, the row goes with it and the provider's retry is treated as new.
    """
    try:
        with transaction.atomic():
            record = WebhookEvent.objects.create(
                provider=provider,
                event_id=event.event_id,
                event_type=event.event_type,
                payload={
                    "reference": event.provider_reference,
                    "amount": str(event.amount),
                    "reason": event.reason,
                },
            )
            if event.event_type in (gateways.PAYMENT_SUCCEEDED, gateways.PAYMENT_FAILED):
                outcome = _complete_payment(event, provider)
            else:
                outcome = _complete_payout(event, provider)
            record.outcome = outcome[:255]
            record.save(update_fields=["outcome"])
    except IntegrityError as exc:
        if "webhookevent_once_per_provider" not in str(exc):
            raise
        return "duplicate", True
    return outcome, False
