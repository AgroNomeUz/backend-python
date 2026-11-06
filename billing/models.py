"""
billing/models.py
Money: a double-entry ledger, the escrow holds that sit on it, and the payments
and payouts that move money across the platform's edge.

**The ledger is the truth.** Every movement is one `LedgerTransaction` whose
`LedgerEntry` rows sum to zero, and a balance is always `SUM(amount)`. There is
no balance column to drift. Two triggers in the initial migration back this up
where a view cannot forget it: entries must balance at commit, and neither
table accepts an UPDATE or DELETE. That is also why neither has a foreign key
to a user: `SET_NULL` would be an UPDATE. Attribution lives in `ActivityLog`.

The sign convention is plain: positive means money sitting in that account, so
a wallet is positive when its organization has funds, and `GATEWAY` goes
negative as money enters the platform. All accounts together always sum to zero.

This app knows nothing about produce. A hold points at whatever it secures
through `reference_type` + `reference_id`, so rentals can use the same ledger.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from core.models import PublicIdModel
from users.models import Organization

MONEY_MAX_DIGITS = 16
MONEY_DECIMAL_PLACES = 2


def money_field(**kwargs) -> models.DecimalField:
    return models.DecimalField(
        max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES, **kwargs
    )


class LedgerAccount(PublicIdModel):
    """One bucket of money: an organization's wallet, or a platform account."""

    class Kind(models.TextChoices):
        WALLET = "wallet", "Organization wallet"
        ESCROW = "escrow", "Escrow"
        PAYOUTS_IN_TRANSIT = "payouts_in_transit", "Payouts in transit"
        COMMISSION = "commission", "Commission revenue"
        GATEWAY = "gateway", "Payment gateway"

    kind = models.CharField(max_length=24, choices=Kind.choices)
    # PROTECT: an organization with money history cannot be deleted away.
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_accounts",
    )
    currency = models.CharField(max_length=3, default="UZS")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(kind="wallet", organization__isnull=False)
                    | (~Q(kind="wallet") & Q(organization__isnull=True))
                ),
                name="ledgeraccount_wallets_have_an_org",
            ),
            models.UniqueConstraint(
                fields=["organization", "currency"],
                condition=Q(kind="wallet"),
                name="ledgeraccount_one_wallet_per_org",
            ),
            models.UniqueConstraint(
                fields=["kind", "currency"],
                condition=~Q(kind="wallet"),
                name="ledgeraccount_one_platform_account_per_kind",
            ),
        ]

    def __str__(self) -> str:
        owner = self.organization_id or "platform"
        return f"{self.kind} ({owner})"


class EscrowHold(PublicIdModel):
    """
    Money taken from a payer and held until it is released or refunded.

    The three running totals are denormalised for reading and for one guard:
    `escrowhold_not_overspent` makes it impossible in the database to pay out
    more than was held. `reconcile_ledger` checks them against the entries.
    """

    class Status(models.TextChoices):
        HELD = "held", "Held"
        SETTLED = "settled", "Settled"

    payer_organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="holds_as_payer"
    )
    payee_organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="holds_as_payee"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.HELD, db_index=True
    )
    amount = money_field()
    released_amount = money_field(default=0)
    refunded_amount = money_field(default=0)
    commission_amount = money_field(default=0)
    # Snapshotted: changing the platform's rate must not rewrite an open order.
    commission_rate = models.DecimalField(max_digits=6, decimal_places=4)
    currency = models.CharField(max_length=3, default="UZS")

    # What this secures. Unique, so one order can never be held for twice.
    reference_type = models.CharField(max_length=40)
    reference_id = models.UUIDField()

    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="escrowhold_amount_positive"
            ),
            models.CheckConstraint(
                condition=Q(released_amount__gte=0) & Q(refunded_amount__gte=0),
                name="escrowhold_totals_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(
                    amount__gte=F("released_amount") + F("refunded_amount")
                ),
                name="escrowhold_not_overspent",
            ),
            models.CheckConstraint(
                condition=~Q(payer_organization=F("payee_organization")),
                name="escrowhold_not_own_org",
            ),
            models.UniqueConstraint(
                fields=["reference_type", "reference_id"],
                name="escrowhold_one_per_reference",
            ),
        ]

    def __str__(self) -> str:
        return f"Hold {self.amount} {self.currency} for {self.reference_type}"

    @property
    def remaining(self):
        return self.amount - self.released_amount - self.refunded_amount


class Payment(PublicIdModel):
    """Money coming in from outside, through the payment provider."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    class Purpose(models.TextChoices):
        TOP_UP = "top_up", "Wallet top-up"
        ORDER = "order", "Order payment"

    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="payments"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments_created",
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    purpose = models.CharField(max_length=10, choices=Purpose.choices)
    amount = money_field()
    currency = models.CharField(max_length=3, default="UZS")

    provider = models.CharField(max_length=20)
    provider_reference = models.CharField(max_length=100, blank=True)
    checkout_url = models.CharField(max_length=500, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)

    reference_type = models.CharField(max_length=40, blank=True)
    reference_id = models.UUIDField(null=True, blank=True)
    # Set by the client on a top-up, so a double tap is one payment.
    idempotency_key = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "-created_at"], name="payment_org_created_idx"
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="payment_amount_positive"
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_reference"],
                condition=~Q(provider_reference=""),
                name="payment_one_per_provider_reference",
            ),
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="payment_org_idempotency_unique",
            ),
            # One open checkout per order: two tabs must not both charge it.
            models.UniqueConstraint(
                fields=["reference_type", "reference_id"],
                condition=Q(status="pending", reference_id__isnull=False),
                name="payment_one_pending_per_reference",
            ),
        ]

    def __str__(self) -> str:
        return f"Payment {self.amount} {self.currency} ({self.status})"


class PayoutDestination(PublicIdModel):
    """
    A bank account or card an organization can withdraw to.

    Only the provider's token and the last four digits are stored. The account
    number itself goes to the provider and is never written here.
    """

    class Kind(models.TextChoices):
        BANK_ACCOUNT = "bank_account", "Bank account"
        CARD = "card", "Card"

    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="payout_destinations"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payout_destinations_created",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    label = models.CharField(max_length=120)
    holder_name = models.CharField(max_length=255)
    last4 = models.CharField(max_length=4)
    provider_token = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    # Payouts are refused until then, so a stolen session cannot add an account
    # and drain the wallet before anyone notices.
    usable_from = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.label} (…{self.last4})"


class Payout(PublicIdModel):
    """Money leaving the platform to an organization's destination."""

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        PROCESSING = "processing", "Processing"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="payouts"
    )
    destination = models.ForeignKey(
        PayoutDestination, on_delete=models.PROTECT, related_name="payouts"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payouts_requested",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.REQUESTED, db_index=True
    )
    amount = money_field()
    currency = models.CharField(max_length=3, default="UZS")
    provider = models.CharField(max_length=20)
    provider_reference = models.CharField(max_length=100, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="payout_amount_positive"
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_reference"],
                condition=~Q(provider_reference=""),
                name="payout_one_per_provider_reference",
            ),
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="payout_org_idempotency_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"Payout {self.amount} {self.currency} ({self.status})"


class LedgerTransaction(PublicIdModel):
    """One balanced movement of money. Append-only."""

    class Kind(models.TextChoices):
        DEPOSIT = "deposit", "Deposit"
        HOLD = "hold", "Escrow hold"
        RELEASE = "release", "Escrow release"
        REFUND = "refund", "Escrow refund"
        PAYOUT = "payout", "Payout requested"
        PAYOUT_SETTLED = "payout_settled", "Payout confirmed"
        PAYOUT_REVERSED = "payout_reversed", "Payout returned"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    # What makes a retried or replayed movement a no-op rather than a second one.
    idempotency_key = models.CharField(max_length=120, unique=True)
    hold = models.ForeignKey(
        EscrowHold,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transactions",
    )
    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transactions",
    )
    payout = models.ForeignKey(
        Payout,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transactions",
    )
    memo = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.kind} #{self.pk}"


class LedgerEntry(PublicIdModel):
    """One leg of a transaction. Append-only."""

    transaction = models.ForeignKey(
        LedgerTransaction, on_delete=models.PROTECT, related_name="entries"
    )
    account = models.ForeignKey(
        LedgerAccount, on_delete=models.PROTECT, related_name="entries"
    )
    amount = money_field()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        verbose_name_plural = "Ledger entries"
        indexes = [
            models.Index(fields=["account", "-id"], name="ledgerentry_statement_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(amount=0), name="ledgerentry_amount_not_zero"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.amount} on account {self.account_id}"


class WebhookEvent(models.Model):
    """
    One event received from a payment provider.

    Exists to be unique: providers deliver the same event more than once, and
    this row is what turns the second delivery into a no-op. Written in the same
    transaction as the event's effect, so a failed attempt leaves nothing behind
    and the provider's retry is processed for real.
    """

    provider = models.CharField(max_length=20)
    event_id = models.CharField(max_length=100)
    event_type = models.CharField(max_length=40)
    payload = models.JSONField(default=dict)
    outcome = models.CharField(max_length=255, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "event_id"], name="webhookevent_once_per_provider"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.event_id}"
