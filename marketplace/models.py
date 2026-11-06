"""
marketplace/models.py
The produce market: a shared catalogue, offers and purchase requests, the
orders that come out of them, and the long-term contracts that generate orders.

**One `Order` for everything.** A delivery under a contract is an order with
`contract` set. It runs the same state machine, holds money the same way and
disputes the same way, so there is no second system for recurring supply.

**An order copies its terms.** Price, unit, variety and grade are snapshotted at
creation. Offers change daily and must never rewrite what was already agreed.

The two organizations on an order are named as on `Deal` and `Inquiry`:
`provider_organization` sells, `customer_organization` buys. Both are copied
from the offer, quote or contract, never from a request body (§0.1). The
catalogue is the one thing here an organization cannot write, which is the same
exception `equipment`'s catalogue has.
"""

from datetime import date
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.validators import FileExtensionValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from billing.models import EscrowHold, money_field
from core.models import PublicIdModel
from deals.models import Deal
from farms.models import CropZone
from listings.models import IMAGE_EXTENSIONS
from users.models import Organization, Region

QUANTITY_MAX_DIGITS = 12
QUANTITY_DECIMAL_PLACES = 3


def quantity_field(**kwargs) -> models.DecimalField:
    return models.DecimalField(
        max_digits=QUANTITY_MAX_DIGITS,
        decimal_places=QUANTITY_DECIMAL_PLACES,
        **kwargs,
    )


def market_today() -> date:
    """Today in Tashkent. The server's UTC date is yesterday until 05:00."""
    return timezone.now().astimezone(ZoneInfo(settings.MARKET_TIME_ZONE)).date()


class Unit(models.TextChoices):
    KILOGRAM = "kg", "Kilogram"
    TONNE = "t", "Tonne"
    LITRE = "l", "Litre"
    PIECE = "piece", "Piece"
    BUNCH = "bunch", "Bunch"
    CRATE = "crate", "Crate"


class Grade(models.TextChoices):
    A = "a", "Grade A"
    B = "b", "Grade B"
    C = "c", "Grade C"
    UNGRADED = "ungraded", "Ungraded"


class DeliveryMethod(models.TextChoices):
    PICKUP = "pickup", "Buyer collects"
    SELLER_DELIVERY = "seller_delivery", "Seller delivers"


class Side(models.TextChoices):
    PROVIDER = "provider", "Provider"
    CUSTOMER = "customer", "Customer"
    PLATFORM = "platform", "Platform"


# ── catalogue ────────────────────────────────────────────────────────────────

class ProductCategory(PublicIdModel):
    """Global reference data: vegetables, fruit, meat and so on."""

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=64, unique=True)
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "Product categories"

    def __str__(self) -> str:
        return self.name


class Product(PublicIdModel):
    """
    What is being sold, in shared words. Free-text titles cannot be searched,
    compared or pooled, and "tomato" is a commodity rather than a unique item.
    """

    category = models.ForeignKey(
        ProductCategory, on_delete=models.PROTECT, related_name="products"
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=64, unique=True)
    default_unit = models.CharField(
        max_length=8, choices=Unit.choices, default=Unit.KILOGRAM
    )
    eppo_code = models.CharField(
        max_length=10,
        blank=True,
        db_index=True,
        help_text="EPPO code, matching farms.CropZone.crop_code",
    )
    is_regulated = models.BooleanField(
        default=False,
        help_text="Needs sanitary paperwork. Only verified organizations can sell it.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


# ── offers ───────────────────────────────────────────────────────────────────

class Offer(PublicIdModel):
    """A quantity of a product an organization is selling now or at harvest."""

    class OfferType(models.TextChoices):
        SPOT = "spot", "In stock"
        FORWARD = "forward", "Future harvest"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        ARCHIVED = "archived", "Archived"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="offers"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="offers_created",
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="offers")
    # Composite FK onto (crop_zone, organization) in the migration: an offer can
    # only point at the seller's own crop.
    crop_zone = models.ForeignKey(
        CropZone,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="offers",
    )

    offer_type = models.CharField(
        max_length=10, choices=OfferType.choices, default=OfferType.SPOT, db_index=True
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    variety = models.CharField(max_length=120, blank=True)
    grade = models.CharField(max_length=10, choices=Grade.choices, default=Grade.UNGRADED)

    unit = models.CharField(max_length=8, choices=Unit.choices, default=Unit.KILOGRAM)
    price = money_field()
    currency = models.CharField(max_length=3, default="UZS")
    # Goes down as orders are placed and back up if they fall through. The check
    # constraint is what actually stops overselling; see `reserve_stock`.
    quantity_available = quantity_field()
    min_order_quantity = quantity_field(default=1)

    # Produce spoils, so every offer ends. The public feed filters on this date
    # rather than a job flipping a status, so an offer cannot linger past it.
    available_from = models.DateField()
    available_until = models.DateField()

    offers_pickup = models.BooleanField(default=True)
    offers_delivery = models.BooleanField(default=False)
    region = models.ForeignKey(
        Region, on_delete=models.SET_NULL, null=True, blank=True, related_name="offers"
    )
    district = models.CharField(max_length=120, blank=True)

    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["status", "available_until"], name="offer_feed_idx"
            ),
            models.Index(fields=["status", "price"], name="offer_status_price_idx"),
            models.Index(fields=["organization", "status"], name="offer_org_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(price__gt=0), name="offer_price_positive"),
            models.CheckConstraint(
                condition=Q(quantity_available__gte=0), name="offer_stock_not_negative"
            ),
            models.CheckConstraint(
                condition=Q(min_order_quantity__gt=0), name="offer_min_order_positive"
            ),
            models.CheckConstraint(
                condition=Q(available_until__gte=F("available_from")),
                name="offer_dates_ordered",
            ),
            models.CheckConstraint(
                condition=Q(offers_pickup=True) | Q(offers_delivery=True),
                name="offer_has_a_delivery_method",
            ),
            models.UniqueConstraint(
                fields=["id", "organization"], name="offer_id_org_unique"
            ),
        ]

    def __str__(self) -> str:
        return self.title


def active_offer_q(prefix: str = "") -> Q:
    """What the public can see and buy, reachable from anywhere."""
    return Q(
        **{
            f"{prefix}status": Offer.Status.ACTIVE,
            f"{prefix}available_until__gte": market_today(),
            f"{prefix}quantity_available__gt": 0,
            f"{prefix}product__is_active": True,
        }
    )


def active_offers():
    return Offer.objects.filter(active_offer_q())


def offer_image_path(instance, filename: str) -> str:
    # Served publicly: `offers` is in PUBLIC_MEDIA_DIRS. Dispute evidence must
    # never land under this directory.
    return f"offers/{instance.offer.public_id}/{filename}"


class OfferImage(PublicIdModel):
    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="images")
    file = models.FileField(
        upload_to=offer_image_path,
        validators=[FileExtensionValidator(IMAGE_EXTENSIONS)],
    )
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["offer"],
                condition=Q(is_primary=True),
                name="offer_one_primary_image",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.offer_id} / {self.file.name}"


# ── purchase requests ────────────────────────────────────────────────────────

class PurchaseRequest(PublicIdModel):
    """
    A buyer saying what they need, for sellers to quote on.

    A salad factory does not browse; it posts "300 kg of tomatoes daily". Several
    farms can each cover part of one request, which is how small supply meets
    large demand.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    customer_organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="purchase_requests"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchase_requests_created",
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="purchase_requests"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    variety = models.CharField(max_length=120, blank=True)
    grade = models.CharField(max_length=10, choices=Grade.choices, default=Grade.UNGRADED)
    quantity = quantity_field()
    unit = models.CharField(max_length=8, choices=Unit.choices, default=Unit.KILOGRAM)
    target_price = money_field(null=True, blank=True)
    # True when the buyer wants regular supply: quotes then lead to a contract.
    is_recurring = models.BooleanField(default=False)
    needed_from = models.DateField()
    needed_until = models.DateField()
    delivery_region = models.ForeignKey(
        Region,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchase_requests",
    )
    delivery_address = models.CharField(max_length=255, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "needed_until"], name="request_feed_idx"),
            models.Index(
                fields=["customer_organization", "-created_at"], name="request_org_idx"
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="request_quantity_positive"
            ),
            models.CheckConstraint(
                condition=Q(needed_until__gte=F("needed_from")),
                name="request_dates_ordered",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.quantity} {self.unit} of {self.product}"


class Quote(PublicIdModel):
    """A seller's answer to a purchase request."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"

    request = models.ForeignKey(
        PurchaseRequest, on_delete=models.PROTECT, related_name="quotes"
    )
    provider_organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="quotes_sent"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quotes_created",
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.SUBMITTED, db_index=True
    )
    unit_price = money_field()
    quantity = quantity_field()
    variety = models.CharField(max_length=120, blank=True)
    grade = models.CharField(max_length=10, choices=Grade.choices, default=Grade.UNGRADED)
    message = models.TextField(blank=True)
    valid_until = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["unit_price", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(unit_price__gt=0), name="quote_price_positive"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="quote_quantity_positive"
            ),
            models.UniqueConstraint(
                fields=["request", "provider_organization"],
                condition=Q(status="submitted"),
                name="quote_one_live_per_provider",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider_organization} quotes {self.unit_price} on {self.request_id}"


# ── contracts ────────────────────────────────────────────────────────────────

class SupplyContract(PublicIdModel):
    """
    A long-term agreement between two organizations. What was agreed lives in
    `ContractTerms`, one immutable row per version; this row is who and what.
    """

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        ACTIVE = "active", "Active"
        REJECTED = "rejected", "Rejected"
        TERMINATED = "terminated", "Terminated"
        COMPLETED = "completed", "Completed"

    provider_organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="contracts_as_provider"
    )
    customer_organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="contracts_as_customer"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contracts_created",
    )
    offer = models.ForeignKey(
        Offer, on_delete=models.PROTECT, null=True, blank=True, related_name="contracts"
    )
    quote = models.ForeignKey(
        Quote, on_delete=models.PROTECT, null=True, blank=True, related_name="contracts"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="contracts"
    )
    variety = models.CharField(max_length=120, blank=True)
    grade = models.CharField(max_length=10, choices=Grade.choices, default=Grade.UNGRADED)
    unit = models.CharField(max_length=8, choices=Unit.choices, default=Unit.KILOGRAM)
    currency = models.CharField(max_length=3, default="UZS")
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PROPOSED, db_index=True
    )
    activated_at = models.DateTimeField(null=True, blank=True)

    # Set when either side gives notice. The contract stays active, delivering,
    # until `termination_date`.
    termination_date = models.DateField(null=True, blank=True)
    terminated_by = models.CharField(max_length=10, choices=Side.choices, blank=True)
    termination_reason = models.TextField(blank=True)

    # One completed deal per contract, however many deliveries it has: a buyer
    # reviews the relationship once, and daily rows would flood the profile.
    deal = models.OneToOneField(
        Deal, on_delete=models.SET_NULL, null=True, blank=True, related_name="contract"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["provider_organization", "status"], name="contract_provider_idx"
            ),
            models.Index(
                fields=["customer_organization", "status"], name="contract_customer_idx"
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(customer_organization=F("provider_organization")),
                name="contract_not_own_org",
            ),
            # Let `Order` reference the pairs with composite foreign keys.
            models.UniqueConstraint(
                fields=["id", "provider_organization"],
                name="contract_id_provider_unique",
            ),
            models.UniqueConstraint(
                fields=["id", "customer_organization"],
                name="contract_id_customer_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.product}: {self.customer_organization} ← {self.provider_organization}"


class ContractTerms(PublicIdModel):
    """
    One version of what was agreed. Never edited: a change is a new version, and
    only the side that did not propose it can accept it.

    Each accepted version governs deliveries from its `start_date` until a later
    accepted version takes over.
    """

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        SUPERSEDED = "superseded", "Superseded"

    contract = models.ForeignKey(
        SupplyContract, on_delete=models.PROTECT, related_name="versions"
    )
    version = models.PositiveIntegerField()
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PROPOSED
    )
    proposed_by_side = models.CharField(max_length=10, choices=Side.choices)
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contract_terms_proposed",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contract_terms_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    unit_price = money_field()
    quantity_per_delivery = quantity_field()
    # Monday is 0, as in `date.weekday()`. All seven is daily.
    delivery_weekdays = ArrayField(models.PositiveSmallIntegerField())
    every_n_weeks = models.PositiveSmallIntegerField(default=1)
    start_date = models.DateField()
    end_date = models.DateField()
    tolerance_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    delivery_method = models.CharField(
        max_length=20,
        choices=DeliveryMethod.choices,
        default=DeliveryMethod.SELLER_DELIVERY,
    )
    delivery_address = models.CharField(max_length=255, blank=True)
    notice_days = models.PositiveSmallIntegerField(default=7)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        verbose_name_plural = "Contract terms"
        constraints = [
            models.UniqueConstraint(
                fields=["contract", "version"], name="contractterms_version_unique"
            ),
            models.UniqueConstraint(
                fields=["contract"],
                condition=Q(status="proposed"),
                name="contractterms_one_pending_per_contract",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gt=0), name="contractterms_price_positive"
            ),
            models.CheckConstraint(
                condition=Q(quantity_per_delivery__gt=0),
                name="contractterms_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(end_date__gte=F("start_date")),
                name="contractterms_dates_ordered",
            ),
            models.CheckConstraint(
                condition=Q(every_n_weeks__gte=1), name="contractterms_interval_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"Contract {self.contract_id} v{self.version} ({self.status})"


# ── orders ───────────────────────────────────────────────────────────────────

class Order(PublicIdModel):
    """One sale and delivery: a one-time purchase, or one delivery of a contract."""

    class Status(models.TextChoices):
        PENDING_CONFIRMATION = "pending_confirmation", "Waiting for the seller"
        AWAITING_PAYMENT = "awaiting_payment", "Waiting for payment"
        PAID = "paid", "Paid"
        DISPATCHED = "dispatched", "Dispatched"
        DELIVERED = "delivered", "Delivered"
        DISPUTED = "disputed", "Disputed"
        SETTLED = "settled", "Settled"
        DECLINED = "declined", "Declined"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.PENDING_CONFIRMATION: {
            Status.AWAITING_PAYMENT,
            Status.DECLINED,
            Status.CANCELLED,
            Status.EXPIRED,
        },
        Status.AWAITING_PAYMENT: {Status.PAID, Status.CANCELLED, Status.EXPIRED},
        Status.PAID: {Status.DISPATCHED, Status.CANCELLED},
        # A buyer can accept or reject while it is still `dispatched`: a seller
        # who never marks the order delivered must not be able to block either.
        Status.DISPATCHED: {Status.DELIVERED, Status.SETTLED, Status.DISPUTED},
        Status.DELIVERED: {Status.SETTLED, Status.DISPUTED},
        Status.DISPUTED: {Status.SETTLED},
        Status.SETTLED: set(),
        Status.DECLINED: set(),
        Status.CANCELLED: set(),
        Status.EXPIRED: set(),
    }

    provider_organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="orders_as_provider"
    )
    customer_organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="orders_as_customer"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders_created",
    )

    # Where it came from: at most one of these three.
    offer = models.ForeignKey(
        Offer, on_delete=models.PROTECT, null=True, blank=True, related_name="orders"
    )
    quote = models.ForeignKey(
        Quote, on_delete=models.PROTECT, null=True, blank=True, related_name="orders"
    )
    contract = models.ForeignKey(
        SupplyContract,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="orders",
    )
    contract_terms = models.ForeignKey(
        ContractTerms,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="orders",
    )

    # The agreement, copied so later edits to its source cannot change it.
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="orders")
    variety = models.CharField(max_length=120, blank=True)
    grade = models.CharField(max_length=10, choices=Grade.choices, default=Grade.UNGRADED)
    unit = models.CharField(max_length=8, choices=Unit.choices)
    unit_price = money_field()
    currency = models.CharField(max_length=3, default="UZS")
    # What was asked for, kept for the fill rate. `quantity` is what the seller
    # committed to, which a contract's tolerance can make different.
    ordered_quantity = quantity_field()
    quantity = quantity_field()
    accepted_quantity = quantity_field(null=True, blank=True)
    subtotal = money_field()

    delivery_method = models.CharField(max_length=20, choices=DeliveryMethod.choices)
    delivery_address = models.CharField(max_length=255, blank=True)
    delivery_date = models.DateField(db_index=True)
    note = models.TextField(blank=True)

    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.PENDING_CONFIRMATION,
        db_index=True,
    )
    # Whose doing it was that this ended early. Feeds the reliability score: a
    # buyer cancelling is not the seller's failure, and neither is an order the
    # seller never confirmed.
    cancelled_by = models.CharField(max_length=10, choices=Side.choices, blank=True)
    cancel_reason = models.TextField(blank=True)

    hold = models.OneToOneField(
        EscrowHold,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="order",
    )
    deal = models.OneToOneField(
        Deal, on_delete=models.SET_NULL, null=True, blank=True, related_name="order"
    )
    # Set by the client, so a double tap places one order.
    idempotency_key = models.CharField(max_length=64, blank=True)

    confirm_by = models.DateTimeField(null=True, blank=True)
    pay_by = models.DateTimeField(null=True, blank=True)
    acceptance_deadline = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["provider_organization", "-created_at"],
                name="order_provider_idx",
            ),
            models.Index(
                fields=["customer_organization", "-created_at"],
                name="order_customer_idx",
            ),
            models.Index(fields=["status", "confirm_by"], name="order_confirm_due_idx"),
            models.Index(fields=["status", "pay_by"], name="order_pay_due_idx"),
            models.Index(
                fields=["status", "acceptance_deadline"], name="order_accept_due_idx"
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(customer_organization=F("provider_organization")),
                name="order_not_own_org",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0) & Q(ordered_quantity__gt=0),
                name="order_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gt=0) & Q(subtotal__gt=0),
                name="order_price_positive",
            ),
            models.CheckConstraint(
                condition=Q(accepted_quantity__isnull=True)
                | (
                    Q(accepted_quantity__gte=0)
                    & Q(accepted_quantity__lte=F("quantity"))
                ),
                name="order_accepted_within_quantity",
            ),
            models.UniqueConstraint(
                fields=["contract", "delivery_date"],
                condition=Q(contract__isnull=False),
                name="order_one_per_contract_date",
            ),
            models.UniqueConstraint(
                fields=["customer_organization", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="order_customer_idempotency_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.quantity} {self.unit} of {self.product} ({self.status})"

    @classmethod
    def can_transition(cls, from_status: str, to_status: str) -> bool:
        return to_status in cls.ALLOWED_TRANSITIONS.get(from_status, set())


class OrderEvent(PublicIdModel):
    """One status change. Append-only, and shown to both sides."""

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="events")
    from_status = models.CharField(max_length=24, blank=True)
    to_status = models.CharField(max_length=24)
    side = models.CharField(max_length=10, choices=Side.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_events",
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"{self.from_status} → {self.to_status}"


class OrderDispute(PublicIdModel):
    """
    A buyer refusing all or part of a delivery.

    The accepted share has already been paid out by the time this exists. What
    is left in the hold stays there until the seller concedes or staff decide.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CONCEDED = "conceded", "Conceded by the seller"
        RESOLVED = "resolved", "Resolved by the platform"

    class Reason(models.TextChoices):
        QUALITY = "quality", "Quality"
        SHORT_DELIVERY = "short_delivery", "Short delivery"
        NOT_DELIVERED = "not_delivered", "Not delivered"
        SPOILED = "spoiled", "Spoiled in transit"
        OTHER = "other", "Other"

    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="dispute")
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    reason = models.CharField(max_length=16, choices=Reason.choices)
    description = models.TextField(blank=True)
    disputed_amount = money_field()
    refund_amount = money_field(null=True, blank=True)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="disputes_opened",
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="disputes_resolved",
    )
    resolution_note = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(disputed_amount__gt=0), name="dispute_amount_positive"
            ),
            models.CheckConstraint(
                condition=Q(refund_amount__isnull=True)
                | (
                    Q(refund_amount__gte=0)
                    & Q(refund_amount__lte=F("disputed_amount"))
                ),
                name="dispute_refund_within_amount",
            ),
        ]

    def __str__(self) -> str:
        return f"Dispute on order {self.order_id} ({self.status})"
