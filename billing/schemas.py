"""
billing/schemas.py
Request and response contracts for /billing.

Money travels as a decimal string. `max_digits` and `decimal_places` reject a
value the column cannot hold exactly, rather than quietly rounding someone's
money. `provider_token` has no field anywhere here: it never leaves the backend.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from ninja import Field, Schema
from pydantic import StringConstraints

from .models import MONEY_DECIMAL_PLACES, MONEY_MAX_DIGITS, PayoutDestination

MoneyIn = Annotated[
    Decimal,
    Field(gt=0, max_digits=MONEY_MAX_DIGITS, decimal_places=MONEY_DECIMAL_PLACES),
]
IdempotencyKey = Annotated[str, StringConstraints(max_length=64)]


class WalletOut(Schema):
    balance: Decimal
    held: Decimal
    currency: str


class StatementEntryOut(Schema):
    id: UUID = Field(alias="public_id")
    kind: str
    amount: Decimal
    created_at: datetime

    @staticmethod
    def resolve_kind(obj) -> str:
        return obj.transaction.kind


class TopUpIn(Schema):
    amount: MoneyIn
    idempotency_key: IdempotencyKey = ""


class PaymentOut(Schema):
    id: UUID = Field(alias="public_id")
    status: str
    purpose: str
    amount: Decimal
    currency: str
    provider: str
    checkout_url: str
    failure_reason: str
    reference_id: UUID | None = None
    created_at: datetime
    completed_at: datetime | None = None


class MockCompleteIn(Schema):
    outcome: Literal["succeeded", "failed"] = "succeeded"


class DestinationCreateIn(Schema):
    kind: PayoutDestination.Kind
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    holder_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    account_number: Annotated[str, StringConstraints(pattern=r"^\d{12,24}$")]
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12)]


class DestinationOut(Schema):
    id: UUID = Field(alias="public_id")
    kind: str
    label: str
    holder_name: str
    last4: str
    is_active: bool
    usable_from: datetime
    created_at: datetime


class CodeSentOut(Schema):
    retry_after: int
    debug_code: str | None = None


class PayoutCreateIn(Schema):
    destination_id: UUID
    amount: MoneyIn
    idempotency_key: IdempotencyKey = ""


class PayoutOut(Schema):
    id: UUID = Field(alias="public_id")
    status: str
    amount: Decimal
    currency: str
    destination: DestinationOut
    failure_reason: str
    created_at: datetime
    completed_at: datetime | None = None
