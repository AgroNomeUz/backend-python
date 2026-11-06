"""
marketplace/schemas.py
Request and response contracts for /market.

Quantities and money reject more precision than their columns hold, rather than
rounding someone's order. Currency, both organizations and every price on an
order come from the server, so none of them has an input field. Everything an
`*Out` schema resolves has to be joined by the queryset behind it: an async view
cannot lazily load a relation.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from ninja import Field, Schema
from pydantic import StringConstraints, field_validator

from api.public_schemas import PublicProviderOut
from billing.schemas import IdempotencyKey, MoneyIn, PaymentOut
from listings.schemas import ListingRegionOut, absolute_file_url

from .models import (
    QUANTITY_DECIMAL_PLACES,
    QUANTITY_MAX_DIGITS,
    DeliveryMethod,
    Grade,
    Offer,
    OrderDispute,
    Unit,
)

QuantityIn = Annotated[
    Decimal,
    Field(gt=0, max_digits=QUANTITY_MAX_DIGITS, decimal_places=QUANTITY_DECIMAL_PLACES),
]
Variety = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
Address = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
District = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]


def org_brief(organization) -> dict:
    """Keyed by `public_id`, the alias a hand-built dict validates against."""
    return {
        "public_id": organization.public_id,
        "name": organization.name,
        "region": organization.region,
        "is_verified": organization.is_verified,
    }


# ── catalogue ────────────────────────────────────────────────────────────────

class ProductSubcategoryOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    slug: str


class ProductCategoryOut(ProductSubcategoryOut):
    children: list[ProductSubcategoryOut] = []

    @staticmethod
    def resolve_children(obj) -> list:
        return list(obj.children.all())


class ProductOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    slug: str
    category: str
    default_unit: str
    eppo_code: str
    is_regulated: bool

    @staticmethod
    def resolve_category(obj) -> str:
        return obj.category.slug


class ReliabilityOut(Schema):
    window_days: int
    orders: int
    fill_rate: Decimal | None = None
    on_time_rate: Decimal | None = None
    dispute_rate: Decimal | None = None


# ── offers ───────────────────────────────────────────────────────────────────

class OfferImageOut(Schema):
    id: UUID = Field(alias="public_id")
    url: str
    sort_order: int
    is_primary: bool

    @staticmethod
    def resolve_url(obj, context) -> str:
        return absolute_file_url(obj, context)


class OfferOut(Schema):
    id: UUID = Field(alias="public_id")
    title: str
    description: str
    offer_type: str
    status: str
    product: ProductOut
    variety: str
    grade: str
    unit: str
    price: Decimal
    currency: str
    quantity_available: Decimal
    min_order_quantity: Decimal
    available_from: date
    available_until: date
    offers_pickup: bool
    offers_delivery: bool
    region: ListingRegionOut | None = None
    district: str
    crop_zone_id: UUID | None = None
    images: list[OfferImageOut] = []
    owner: PublicProviderOut
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def resolve_owner(obj) -> dict:
        return org_brief(obj.organization)

    @staticmethod
    def resolve_crop_zone_id(obj) -> UUID | None:
        return obj.crop_zone.public_id if obj.crop_zone_id else None


class OfferCreateIn(Schema):
    product_id: UUID
    title: Title
    price: MoneyIn
    quantity_available: QuantityIn
    available_from: date
    available_until: date
    offer_type: Offer.OfferType = Offer.OfferType.SPOT
    description: str = ""
    variety: Variety = ""
    grade: Grade = Grade.UNGRADED
    # Defaults to the product's usual unit.
    unit: Unit | None = None
    min_order_quantity: QuantityIn = Decimal("1")
    offers_pickup: bool = True
    offers_delivery: bool = False
    # Defaults to the organization's own region.
    region_id: UUID | None = None
    district: District = ""
    crop_zone_id: UUID | None = None
    status: Offer.Status = Offer.Status.DRAFT


class OfferUpdateIn(Schema):
    """Only the keys present are applied. `product` and `unit` are the offer."""

    title: Title | None = None
    description: str | None = None
    variety: Variety | None = None
    grade: Grade | None = None
    price: MoneyIn | None = None
    quantity_available: Annotated[
        Decimal,
        Field(
            ge=0,
            max_digits=QUANTITY_MAX_DIGITS,
            decimal_places=QUANTITY_DECIMAL_PLACES,
        ),
    ] | None = None
    min_order_quantity: QuantityIn | None = None
    available_from: date | None = None
    available_until: date | None = None
    offers_pickup: bool | None = None
    offers_delivery: bool | None = None
    region_id: UUID | None = None
    district: District | None = None
    crop_zone_id: UUID | None = None
    status: Offer.Status | None = None


# Keys whose `null` is a value rather than an omission.
NULLABLE_OFFER_FIELDS = frozenset({"region_id", "crop_zone_id"})


# ── purchase requests and quotes ─────────────────────────────────────────────

class PurchaseRequestOut(Schema):
    id: UUID = Field(alias="public_id")
    status: str
    product: ProductOut
    variety: str
    grade: str
    quantity: Decimal
    quantity_covered: Decimal = Decimal("0")
    unit: str
    target_price: Decimal | None = None
    is_recurring: bool
    needed_from: date
    needed_until: date
    delivery_region: ListingRegionOut | None = None
    delivery_address: str
    note: str
    customer: PublicProviderOut
    created_at: datetime

    @staticmethod
    def resolve_customer(obj) -> dict:
        return org_brief(obj.customer_organization)


class PurchaseRequestCreateIn(Schema):
    product_id: UUID
    quantity: QuantityIn
    needed_from: date
    needed_until: date
    unit: Unit | None = None
    variety: Variety = ""
    grade: Grade = Grade.UNGRADED
    target_price: MoneyIn | None = None
    is_recurring: bool = False
    delivery_region_id: UUID | None = None
    delivery_address: Address = ""
    note: str = ""


class QuoteOut(Schema):
    id: UUID = Field(alias="public_id")
    request_id: UUID
    status: str
    unit_price: Decimal
    quantity: Decimal
    variety: str
    grade: str
    message: str
    valid_until: date
    provider: PublicProviderOut
    created_at: datetime

    @staticmethod
    def resolve_request_id(obj) -> UUID:
        return obj.request.public_id

    @staticmethod
    def resolve_provider(obj) -> dict:
        return org_brief(obj.provider_organization)


class QuoteCreateIn(Schema):
    unit_price: MoneyIn
    quantity: QuantityIn
    valid_until: date
    variety: Variety = ""
    grade: Grade = Grade.UNGRADED
    message: str = ""


class QuoteAcceptIn(Schema):
    delivery_date: date
    delivery_method: DeliveryMethod
    delivery_address: Address = ""


# ── orders ───────────────────────────────────────────────────────────────────

class OrderEventOut(Schema):
    from_status: str
    to_status: str
    side: str
    note: str
    created_at: datetime


class DisputeOut(Schema):
    id: UUID = Field(alias="public_id")
    status: str
    reason: str
    description: str
    disputed_amount: Decimal
    refund_amount: Decimal | None = None
    resolution_note: str
    resolved_at: datetime | None = None
    created_at: datetime


class SettlementOut(Schema):
    held: Decimal
    released: Decimal
    refunded: Decimal
    commission: Decimal


class OrderOut(Schema):
    id: UUID = Field(alias="public_id")
    status: str
    product: ProductOut
    variety: str
    grade: str
    unit: str
    unit_price: Decimal
    currency: str
    ordered_quantity: Decimal
    quantity: Decimal
    accepted_quantity: Decimal | None = None
    subtotal: Decimal
    delivery_method: str
    delivery_address: str
    delivery_date: date
    note: str
    provider: PublicProviderOut
    customer: PublicProviderOut
    offer_id: UUID | None = None
    quote_id: UUID | None = None
    contract_id: UUID | None = None
    deal_id: UUID | None = None
    settlement: SettlementOut | None = None
    dispute: DisputeOut | None = None
    cancelled_by: str
    cancel_reason: str
    confirm_by: datetime | None = None
    pay_by: datetime | None = None
    acceptance_deadline: datetime | None = None
    confirmed_at: datetime | None = None
    paid_at: datetime | None = None
    dispatched_at: datetime | None = None
    delivered_at: datetime | None = None
    settled_at: datetime | None = None
    created_at: datetime

    @staticmethod
    def resolve_provider(obj) -> dict:
        return org_brief(obj.provider_organization)

    @staticmethod
    def resolve_customer(obj) -> dict:
        return org_brief(obj.customer_organization)

    @staticmethod
    def resolve_offer_id(obj) -> UUID | None:
        return obj.offer.public_id if obj.offer_id else None

    @staticmethod
    def resolve_quote_id(obj) -> UUID | None:
        return obj.quote.public_id if obj.quote_id else None

    @staticmethod
    def resolve_contract_id(obj) -> UUID | None:
        return obj.contract.public_id if obj.contract_id else None

    @staticmethod
    def resolve_deal_id(obj) -> UUID | None:
        return obj.deal.public_id if obj.deal_id else None

    @staticmethod
    def resolve_settlement(obj) -> dict | None:
        if not obj.hold_id:
            return None
        hold = obj.hold
        return {
            "held": hold.remaining,
            "released": hold.released_amount,
            "refunded": hold.refunded_amount,
            "commission": hold.commission_amount,
        }

    @staticmethod
    def resolve_dispute(obj):
        return getattr(obj, "dispute", None)


class OrderDetailOut(OrderOut):
    events: list[OrderEventOut] = []

    @staticmethod
    def resolve_events(obj) -> list:
        return list(obj.events.all())


class OrderCreateIn(Schema):
    offer_id: UUID
    quantity: QuantityIn
    delivery_date: date
    delivery_method: DeliveryMethod
    delivery_address: Address = ""
    note: str = ""
    # The price the buyer was shown. If the seller has changed it since, the
    # order is refused rather than quietly charged at the new price.
    expected_unit_price: MoneyIn | None = None
    idempotency_key: IdempotencyKey = ""


class OrderConfirmIn(Schema):
    # Contract deliveries only, within the agreed tolerance.
    quantity: QuantityIn | None = None


class ReasonIn(Schema):
    reason: str = ""


class OrderPayIn(Schema):
    method: Literal["wallet", "gateway"]


class OrderPayOut(Schema):
    order: OrderOut
    # Present for `gateway`: send the buyer to `payment.checkout_url`.
    payment: PaymentOut | None = None


class OrderRejectIn(Schema):
    accepted_quantity: Annotated[
        Decimal,
        Field(
            ge=0,
            max_digits=QUANTITY_MAX_DIGITS,
            decimal_places=QUANTITY_DECIMAL_PLACES,
        ),
    ] = Decimal("0")
    reason: OrderDispute.Reason
    description: str = ""


class DisputeResolveIn(Schema):
    refund_amount: Annotated[
        Decimal, Field(ge=0, max_digits=16, decimal_places=2)
    ]
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# ── contracts ────────────────────────────────────────────────────────────────

class ContractTermsOut(Schema):
    id: UUID = Field(alias="public_id")
    version: int
    status: str
    proposed_by_side: str
    decided_at: datetime | None = None
    unit_price: Decimal
    quantity_per_delivery: Decimal
    delivery_weekdays: list[int]
    every_n_weeks: int
    start_date: date
    end_date: date
    tolerance_percent: Decimal
    delivery_method: str
    delivery_address: str
    notice_days: int
    created_at: datetime


class ContractOut(Schema):
    id: UUID = Field(alias="public_id")
    status: str
    product: ProductOut
    variety: str
    grade: str
    unit: str
    currency: str
    provider: PublicProviderOut
    customer: PublicProviderOut
    offer_id: UUID | None = None
    quote_id: UUID | None = None
    # The version in force, and the one waiting for an answer.
    current_terms: ContractTermsOut | None = None
    pending_terms: ContractTermsOut | None = None
    activated_at: datetime | None = None
    termination_date: date | None = None
    terminated_by: str
    termination_reason: str
    deal_id: UUID | None = None
    created_at: datetime

    @staticmethod
    def resolve_provider(obj) -> dict:
        return org_brief(obj.provider_organization)

    @staticmethod
    def resolve_customer(obj) -> dict:
        return org_brief(obj.customer_organization)

    @staticmethod
    def resolve_offer_id(obj) -> UUID | None:
        return obj.offer.public_id if obj.offer_id else None

    @staticmethod
    def resolve_quote_id(obj) -> UUID | None:
        return obj.quote.public_id if obj.quote_id else None

    @staticmethod
    def resolve_deal_id(obj) -> UUID | None:
        return obj.deal.public_id if obj.deal_id else None

    @staticmethod
    def resolve_current_terms(obj):
        return next((v for v in obj.versions.all() if v.status == "accepted"), None)

    @staticmethod
    def resolve_pending_terms(obj):
        return next((v for v in obj.versions.all() if v.status == "proposed"), None)


class ContractDetailOut(ContractOut):
    versions: list[ContractTermsOut] = []

    @staticmethod
    def resolve_versions(obj) -> list:
        return list(obj.versions.all())


class ContractTermsIn(Schema):
    unit_price: MoneyIn
    quantity_per_delivery: QuantityIn
    # Monday is 0. All seven means daily.
    delivery_weekdays: list[int] = Field(min_length=1, max_length=7)
    every_n_weeks: int = Field(default=1, ge=1, le=8)
    start_date: date
    end_date: date
    tolerance_percent: Decimal = Field(
        default=Decimal("0"), ge=0, le=50, max_digits=5, decimal_places=2
    )
    delivery_method: DeliveryMethod = DeliveryMethod.SELLER_DELIVERY
    delivery_address: Address = ""
    notice_days: int = Field(default=7, ge=0, le=90)

    @field_validator("delivery_weekdays")
    @classmethod
    def _real_weekdays(cls, days: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in days):
            raise ValueError("Weekdays run from 0 (Monday) to 6 (Sunday).")
        return sorted(set(days))


class ContractCreateIn(ContractTermsIn):
    # Exactly one of these two says who the contract is with and for what.
    offer_id: UUID | None = None
    quote_id: UUID | None = None
    # Defaults to the price on the offer or quote.
    unit_price: MoneyIn | None = None


class ContractAnswerIn(Schema):
    # The `pending_terms.version` the caller read. Anything else is a 409.
    version: int


class ContractTerminateIn(Schema):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

