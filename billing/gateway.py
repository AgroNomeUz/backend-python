"""
billing/gateway.py
The seam between us and a payment provider — the same idea as `api/sms.py`.

No contract with Payme, Click or a bank is signed yet, so `MockGateway` stands
in. Supporting a real provider means writing one class with these four methods
and adding it to `_GATEWAYS`; nothing else in the codebase should change.

**The mock is refused in production.** Its job is to let a caller declare that
money arrived, which on a live system is minting money. `get_gateway` raises
instead, so a misconfigured deployment fails loudly at the first payment.
"""

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils.crypto import salted_hmac

PAYMENT_SUCCEEDED = "payment.succeeded"
PAYMENT_FAILED = "payment.failed"
PAYOUT_PAID = "payout.paid"
PAYOUT_FAILED = "payout.failed"
EVENT_TYPES = {PAYMENT_SUCCEEDED, PAYMENT_FAILED, PAYOUT_PAID, PAYOUT_FAILED}


class GatewayUnavailable(Exception):
    """No usable payment provider is configured."""


class GatewayError(Exception):
    """
    The provider definitely refused the request, so nothing happened.

    A real gateway must raise this only when it is sure. A timeout is not a
    refusal: the money may have moved, and treating it as one would return a
    payout to the wallet and pay it twice. Let those propagate instead.
    """


class WebhookRejected(Exception):
    """A webhook failed verification. Nothing in it can be trusted."""


@dataclass(frozen=True)
class Checkout:
    provider_reference: str
    checkout_url: str


@dataclass(frozen=True)
class GatewayEvent:
    event_id: str
    event_type: str
    provider_reference: str
    amount: Decimal
    reason: str = ""


class MockGateway:
    """Accepts everything. Outcomes are decided by the mock-complete endpoints."""

    name = "mock"
    SIGNATURE_HEADER = "HTTP_X_MOCK_SIGNATURE"

    def create_checkout(self, payment) -> Checkout:
        return Checkout(
            provider_reference=f"mock_pay_{uuid.uuid4().hex}",
            checkout_url=f"/api/v1/billing/payments/{payment.public_id}/mock-complete",
        )

    def tokenize_destination(self, kind: str, account_number: str) -> str:
        return f"mock_dest_{uuid.uuid4().hex}"

    def send_payout(self, payout) -> str:
        return f"mock_out_{uuid.uuid4().hex}"

    def _secret(self) -> bytes:
        return salted_hmac("billing.gateway.mock", "webhook", algorithm="sha256").digest()

    def sign(self, body: bytes) -> str:
        return hmac.new(self._secret(), body, hashlib.sha256).hexdigest()

    def build_event(
        self, event_type: str, provider_reference: str, amount, reason: str = ""
    ) -> bytes:
        """The webhook body a real provider would POST, for the mock-complete endpoints."""
        return json.dumps(
            {
                "event_id": f"mock_evt_{uuid.uuid4().hex}",
                "type": event_type,
                "reference": provider_reference,
                "amount": str(amount),
                "reason": reason,
            }
        ).encode()

    def parse_webhook(self, body: bytes, headers: dict) -> GatewayEvent:
        """Verify the signature before reading a single field of the body."""
        signature = headers.get(self.SIGNATURE_HEADER, "")
        if not hmac.compare_digest(signature, self.sign(body)):
            raise WebhookRejected("Bad signature")
        try:
            data = json.loads(body)
            amount = Decimal(str(data["amount"]))
            event = GatewayEvent(
                event_id=str(data["event_id"]),
                event_type=str(data["type"]),
                provider_reference=str(data["reference"]),
                amount=amount,
                reason=str(data.get("reason", "")),
            )
        except (ValueError, KeyError, TypeError, InvalidOperation):
            raise WebhookRejected("Malformed event")
        if event.event_type not in EVENT_TYPES:
            raise WebhookRejected("Unknown event type")
        return event


_GATEWAYS = {MockGateway.name: MockGateway}


def get_gateway(name: str | None = None):
    """The configured provider, or `name` when finishing work it started."""
    name = name or settings.BILLING_GATEWAY
    gateway = _GATEWAYS.get(name)
    if gateway is None:
        raise GatewayUnavailable(f"Unknown payment gateway '{name}'")
    if gateway is MockGateway and not settings.BILLING_MOCK_ENABLED:
        raise GatewayUnavailable("The mock payment gateway is disabled in production")
    return gateway()
