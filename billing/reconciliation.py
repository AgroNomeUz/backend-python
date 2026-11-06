"""
billing/reconciliation.py
Re-derive what the ledger says and report anywhere it disagrees with the rest
of the system. Read-only: a mismatch means something has gone wrong and needs
a person, so nothing here tries to repair it.

Comparing against the provider's own settlement report is the check that
matters most in production, and it can only be added with a real provider.
"""

from datetime import timedelta
from decimal import Decimal

from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import (
    EscrowHold,
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
    Payment,
    Payout,
    WebhookEvent,
)

REQUESTED_TOO_LONG = timedelta(hours=1)
PROCESSING_TOO_LONG = timedelta(days=2)
# Wide enough that a daily run cannot miss one, narrow enough that an old event
# stops failing the check once someone has dealt with it.
EVENT_LOOKBACK = timedelta(days=2)


def reconcile() -> list[str]:
    """
    Every problem found, as readable lines. An empty list means all is well.

    The checks are separate queries, so outside a single snapshot money moving
    between two of them looks like a mismatch. `reconcile_ledger` takes one.
    """
    return [
        *_ledger_balances(),
        *_wallets_not_overdrawn(),
        *_escrow_matches_holds(),
        *_holds_match_entries(),
        *_payments_match_deposits(),
        *_payouts_in_transit(),
    ]


def needs_a_person(*, now=None) -> list[str]:
    """
    Things the books agree on but somebody has to look at.

    Separate from `reconcile`, which is about the ledger contradicting itself.
    Nothing here can be fixed automatically: a payout stuck in `requested` may
    or may not have been sent, so retrying it could pay twice, and returning it
    could give away money that already left.
    """
    now = now or timezone.now()
    stuck = Payout.objects.filter(
        status=Payout.Status.REQUESTED, created_at__lt=now - REQUESTED_TOO_LONG
    ) | Payout.objects.filter(
        status=Payout.Status.PROCESSING, created_at__lt=now - PROCESSING_TOO_LONG
    )
    events = WebhookEvent.objects.filter(
        Q(outcome__startswith="rejected") | Q(outcome__startswith="ignored:"),
        received_at__gte=now - EVENT_LOOKBACK,
    )
    return [
        *(f"Payout {payout.pk} has been {payout.status} since {payout.created_at}" for payout in stuck),
        *(f"Event {event.provider}:{event.event_id}: {event.outcome}" for event in events),
    ]


def _ledger_balances() -> list[str]:
    total = LedgerEntry.objects.aggregate(total=Sum("amount"))["total"] or Decimal(0)
    problems = [f"The ledger sums to {total}, not zero"] if total != 0 else []
    unbalanced = (
        LedgerEntry.objects.values("transaction_id")
        .annotate(total=Sum("amount"))
        .exclude(total=0)
    )
    problems += [
        f"Transaction {row['transaction_id']} sums to {row['total']}"
        for row in unbalanced
    ]
    return problems


def _wallets_not_overdrawn() -> list[str]:
    overdrawn = (
        LedgerAccount.objects.filter(kind=LedgerAccount.Kind.WALLET)
        .annotate(balance=Sum("entries__amount"))
        .filter(balance__lt=0)
    )
    return [f"Wallet {account.pk} is overdrawn: {account.balance}" for account in overdrawn]


def _account_total(kind: str) -> Decimal:
    return LedgerEntry.objects.filter(account__kind=kind).aggregate(total=Sum("amount"))[
        "total"
    ] or Decimal(0)


def _escrow_matches_holds() -> list[str]:
    escrow = _account_total(LedgerAccount.Kind.ESCROW)
    open_holds = sum(
        (hold.remaining for hold in EscrowHold.objects.filter(status=EscrowHold.Status.HELD)),
        Decimal(0),
    )
    if escrow != open_holds:
        return [f"Escrow holds {escrow} but open holds add up to {open_holds}"]
    return []


def _holds_match_entries() -> list[str]:
    def escrow_total(kind):
        return Coalesce(
            Sum(
                "transactions__entries__amount",
                filter=Q(
                    transactions__kind=kind,
                    transactions__entries__account__kind=LedgerAccount.Kind.ESCROW,
                ),
            ),
            Decimal(0),
        )

    # From the holds, not the entries: a hold with no entries at all is a mismatch.
    mismatched = EscrowHold.objects.annotate(
        held=escrow_total(LedgerTransaction.Kind.HOLD),
        released=-escrow_total(LedgerTransaction.Kind.RELEASE),
        refunded=-escrow_total(LedgerTransaction.Kind.REFUND),
    ).exclude(
        held=F("amount"),
        released=F("released_amount"),
        refunded=F("refunded_amount"),
    )
    problems = []
    for hold in mismatched:
        if hold.held != hold.amount:
            problems.append(f"Hold {hold.pk}: amount disagrees with the ledger")
        if hold.released != hold.released_amount:
            problems.append(f"Hold {hold.pk}: released_amount disagrees with the ledger")
        if hold.refunded != hold.refunded_amount:
            problems.append(f"Hold {hold.pk}: refunded_amount disagrees with the ledger")
    return problems


def _payments_match_deposits() -> list[str]:
    deposited = set(
        LedgerTransaction.objects.filter(kind=LedgerTransaction.Kind.DEPOSIT).values_list(
            "payment_id", flat=True
        )
    )
    succeeded = set(
        Payment.objects.filter(status=Payment.Status.SUCCEEDED).values_list("pk", flat=True)
    )
    return [
        *(f"Payment {pk} succeeded but was never credited" for pk in succeeded - deposited),
        *(f"Payment {pk} was credited but did not succeed" for pk in deposited - succeeded),
    ]


def _payouts_in_transit() -> list[str]:
    in_transit = _account_total(LedgerAccount.Kind.PAYOUTS_IN_TRANSIT)
    open_payouts = Payout.objects.filter(
        status__in=[Payout.Status.REQUESTED, Payout.Status.PROCESSING]
    ).aggregate(total=Sum("amount"))["total"] or Decimal(0)
    if in_transit != open_payouts:
        return [f"{in_transit} is in transit but open payouts add up to {open_payouts}"]
    return []
