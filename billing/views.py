"""
billing/views.py
The organization's wallet, its payments and payouts, and provider webhooks.

Every write resolves the organization from the token, needs `billing.manage`
and is audited. Another organization's id is a 404. The webhook is the only
unauthenticated endpoint, and it trusts nothing until the signature checks out.

`mock-complete` stands in for the buyer finishing a checkout on the provider's
page. It builds the same signed event a provider would send and feeds it through
the same code path, so the mock exercises the real webhook logic, and it does
not exist in production.
"""

from uuid import UUID

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import Http404, HttpRequest
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from api.otp import OtpInvalid, OtpThrottled, issue_otp, verify_otp
from api.schemas import OtpThrottledOut
from core.audit import client_ip, log_activity
from core.models import ActivityLog
from users.models import OrgPermission
from users.permissions import readable_organization, writable_organization

from . import gateway as gateways
from . import services
from .models import LedgerEntry, Payment, Payout, PayoutDestination
from .schemas import (
    CodeSentOut,
    DestinationCreateIn,
    DestinationOut,
    MockCompleteIn,
    PaymentOut,
    PayoutCreateIn,
    PayoutOut,
    StatementEntryOut,
    TopUpIn,
    WalletOut,
)

billing_router = Router(tags=["Billing"])


def _require_mock() -> None:
    if not settings.BILLING_MOCK_ENABLED:
        raise Http404()


# ── wallet ───────────────────────────────────────────────────────────────────

def _wallet_summary(organization) -> dict:
    return {
        "balance": services.wallet_balance(organization),
        "held": services.held_for(organization),
        "currency": settings.BILLING_CURRENCY,
    }


@billing_router.get("/wallet", response=WalletOut)
async def get_wallet(request):
    """`balance` is spendable; `held` is tied up in open orders on top of that."""
    organization = readable_organization(request, OrgPermission.VIEW_BILLING)
    return await sync_to_async(_wallet_summary)(organization)


def _statement(organization):
    return LedgerEntry.objects.filter(
        account=services.wallet_for(organization)
    ).select_related("transaction")


@billing_router.get("/wallet/entries", response=list[StatementEntryOut])
@paginate(LimitOffsetPagination)
async def list_wallet_entries(request):
    """Every movement on the wallet, newest first."""
    organization = readable_organization(request, OrgPermission.VIEW_BILLING)
    return await sync_to_async(_statement)(organization)


# ── payments in ──────────────────────────────────────────────────────────────

def _apply_top_up(request, organization, data: TopUpIn) -> Payment:
    return services.start_payment(
        organization=organization,
        amount=data.amount,
        purpose=Payment.Purpose.TOP_UP,
        user=request.auth,
        idempotency_key=data.idempotency_key,
        on_created=lambda payment: log_activity(
            request,
            organization,
            ActivityLog.Action.CREATED,
            payment,
            changes={"amount": {"from": None, "to": str(payment.amount)}},
        ),
    )


@billing_router.post("/top-ups", response={201: PaymentOut})
async def create_top_up(request, data: TopUpIn):
    """Start adding money to the wallet. The buyer finishes at `checkout_url`."""
    organization = writable_organization(request, OrgPermission.MANAGE_BILLING)
    return 201, await sync_to_async(_apply_top_up)(request, organization, data)


@billing_router.get("/payments", response=list[PaymentOut])
@paginate(LimitOffsetPagination)
async def list_payments(request):
    organization = readable_organization(request, OrgPermission.VIEW_BILLING)
    return Payment.objects.filter(organization=organization)


@billing_router.get("/payments/{payment_id}", response=PaymentOut)
async def get_payment(request, payment_id: UUID):
    organization = readable_organization(request, OrgPermission.VIEW_BILLING)
    return await aget_object_or_404(
        Payment.objects.filter(organization=organization), public_id=payment_id
    )


def _apply_mock_payment(payment: Payment, outcome: str) -> Payment:
    gateway = gateways.get_gateway(payment.provider)
    event_type = (
        gateways.PAYMENT_SUCCEEDED if outcome == "succeeded" else gateways.PAYMENT_FAILED
    )
    body = gateway.build_event(
        event_type,
        payment.provider_reference,
        payment.amount,
        reason="" if outcome == "succeeded" else "Declined by the mock gateway",
    )
    event = gateway.parse_webhook(body, {gateway.SIGNATURE_HEADER: gateway.sign(body)})
    services.process_event(gateway.name, event)
    payment.refresh_from_db()
    return payment


@billing_router.post("/payments/{payment_id}/mock-complete", response=PaymentOut)
async def mock_complete_payment(request, payment_id: UUID, data: MockCompleteIn):
    """**Development only.** Plays the buyer finishing the provider's checkout."""
    _require_mock()
    organization = writable_organization(request, OrgPermission.MANAGE_BILLING)
    payment = await aget_object_or_404(
        Payment.objects.filter(organization=organization), public_id=payment_id
    )
    return await sync_to_async(_apply_mock_payment)(payment, data.outcome)


# ── payout destinations ──────────────────────────────────────────────────────

@billing_router.post(
    "/payout-destinations/code", response={200: CodeSentOut, 429: OtpThrottledOut}
)
async def send_destination_code(request):
    """Text a code to the caller. Adding a payout account has to present it."""
    writable_organization(request, OrgPermission.MANAGE_BILLING)
    phone = request.auth.phone
    if not phone:
        raise HttpError(400, "Your account has no phone number to confirm this with")
    try:
        issued = await sync_to_async(issue_otp)(phone, client_ip(request))
    except OtpThrottled as exc:
        return 429, {"detail": exc.detail, "retry_after": exc.retry_after}
    return 200, {"retry_after": issued.retry_after, "debug_code": issued.disclosed_code}


def _apply_add_destination(request, organization, data: DestinationCreateIn):
    phone = request.auth.phone
    if not phone:
        raise HttpError(400, "Your account has no phone number to confirm this with")
    try:
        verify_otp(phone, data.code)
    except OtpInvalid as exc:
        raise HttpError(400, str(exc))
    return services.add_destination(
        organization=organization,
        user=request.auth,
        kind=data.kind,
        label=data.label,
        holder_name=data.holder_name,
        account_number=data.account_number,
        on_created=lambda destination: log_activity(
            request,
            organization,
            ActivityLog.Action.CREATED,
            destination,
            changes={"last4": {"from": None, "to": destination.last4}},
        ),
    )


@billing_router.get("/payout-destinations", response=list[DestinationOut])
async def list_destinations(request):
    organization = readable_organization(request, OrgPermission.VIEW_BILLING)
    return [
        destination
        async for destination in PayoutDestination.objects.filter(
            organization=organization, is_active=True
        )
    ]


@billing_router.post("/payout-destinations", response={201: DestinationOut})
async def add_payout_destination(request, data: DestinationCreateIn):
    """Add an account to withdraw to. Usable after the cooling-off period."""
    organization = writable_organization(request, OrgPermission.MANAGE_BILLING)
    return 201, await sync_to_async(_apply_add_destination)(request, organization, data)


def _apply_remove_destination(request, organization, destination) -> None:
    destination.is_active = False
    destination.save(update_fields=["is_active"])
    log_activity(
        request,
        organization,
        ActivityLog.Action.DELETED,
        destination,
        changes={"is_active": {"from": True, "to": False}},
    )


@billing_router.delete("/payout-destinations/{destination_id}", response={204: None})
async def remove_destination(request, destination_id: UUID):
    """A soft delete: payouts already made to it keep their history."""
    organization = writable_organization(request, OrgPermission.MANAGE_BILLING)
    destination = await aget_object_or_404(
        PayoutDestination.objects.filter(organization=organization, is_active=True),
        public_id=destination_id,
    )
    await sync_to_async(_apply_remove_destination)(request, organization, destination)
    return 204, None


# ── payouts ──────────────────────────────────────────────────────────────────

def _apply_payout(request, organization, destination, data: PayoutCreateIn) -> Payout:
    payout = services.request_payout(
        organization=organization,
        destination=destination,
        amount=data.amount,
        user=request.auth,
        idempotency_key=data.idempotency_key,
        on_created=lambda payout: log_activity(
            request,
            organization,
            ActivityLog.Action.CREATED,
            payout,
            changes={"amount": {"from": None, "to": str(payout.amount)}},
        ),
    )
    return Payout.objects.select_related("destination").get(pk=payout.pk)


@billing_router.get("/payouts", response=list[PayoutOut])
@paginate(LimitOffsetPagination)
async def list_payouts(request):
    organization = readable_organization(request, OrgPermission.VIEW_BILLING)
    return Payout.objects.filter(organization=organization).select_related("destination")


@billing_router.post("/payouts", response={201: PayoutOut})
async def create_payout(request, data: PayoutCreateIn):
    """Withdraw money from the wallet to one of the organization's accounts."""
    organization = writable_organization(request, OrgPermission.MANAGE_BILLING)
    destination = await aget_object_or_404(
        PayoutDestination.objects.filter(organization=organization),
        public_id=data.destination_id,
    )
    return 201, await sync_to_async(_apply_payout)(
        request, organization, destination, data
    )


@billing_router.get("/payouts/{payout_id}", response=PayoutOut)
async def get_payout(request, payout_id: UUID):
    organization = readable_organization(request, OrgPermission.VIEW_BILLING)
    return await aget_object_or_404(
        Payout.objects.filter(organization=organization).select_related("destination"),
        public_id=payout_id,
    )


def _apply_mock_payout(payout: Payout, outcome: str) -> Payout:
    gateway = gateways.get_gateway(payout.provider)
    event_type = gateways.PAYOUT_PAID if outcome == "succeeded" else gateways.PAYOUT_FAILED
    body = gateway.build_event(
        event_type,
        payout.provider_reference,
        payout.amount,
        reason="" if outcome == "succeeded" else "Refused by the mock gateway",
    )
    event = gateway.parse_webhook(body, {gateway.SIGNATURE_HEADER: gateway.sign(body)})
    services.process_event(gateway.name, event)
    return Payout.objects.select_related("destination").get(pk=payout.pk)


@billing_router.post("/payouts/{payout_id}/mock-complete", response=PayoutOut)
async def mock_complete_payout(request, payout_id: UUID, data: MockCompleteIn):
    """**Development only.** Plays the provider reporting the transfer's result."""
    _require_mock()
    organization = writable_organization(request, OrgPermission.MANAGE_BILLING)
    payout = await aget_object_or_404(
        Payout.objects.filter(organization=organization), public_id=payout_id
    )
    return await sync_to_async(_apply_mock_payout)(payout, data.outcome)


# ── provider webhooks ────────────────────────────────────────────────────────

def _apply_webhook(provider: str, body: bytes, headers: dict) -> dict:
    gateway = gateways.get_gateway(provider)
    event = gateway.parse_webhook(body, headers)
    outcome, duplicate = services.process_event(gateway.name, event)
    return {"outcome": outcome, "duplicate": duplicate}


@billing_router.post("/webhooks/{provider}", auth=None, response=dict)
async def receive_webhook(request: HttpRequest, provider: str):
    """
    Events from a payment provider.

    An event we can read but refuse to apply, such as a wrong amount, answers 200
    because a retry would not change the answer. Its reason is kept on
    `WebhookEvent` and `reconcile_ledger` puts it in front of a person. One that
    refers to something we cannot find *yet* answers 503, so the provider sends
    it again.
    """
    try:
        return await sync_to_async(_apply_webhook)(provider, request.body, request.META)
    except gateways.GatewayUnavailable:
        raise Http404()
    except gateways.WebhookRejected as exc:
        raise HttpError(400, str(exc))
    except services.EventNotReady:
        raise HttpError(503, "Not ready for this event yet. Send it again.")
