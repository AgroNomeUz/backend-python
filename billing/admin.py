"""
Read-only by design: money moves through billing/services.py and nowhere else.
Editing a row here would bypass the ledger, so nothing can be added, changed or
deleted from the admin site.
"""

from django.contrib import admin

from core.admin import ReadOnlyAdmin

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


@admin.register(LedgerAccount)
class LedgerAccountAdmin(ReadOnlyAdmin):
    list_display = ["id", "kind", "organization", "currency", "created_at"]
    list_filter = ["kind"]
    search_fields = ["organization__name"]


@admin.register(LedgerTransaction)
class LedgerTransactionAdmin(ReadOnlyAdmin):
    list_display = ["id", "kind", "idempotency_key", "hold", "payment", "payout", "created_at"]
    list_filter = ["kind"]


@admin.register(LedgerEntry)
class LedgerEntryAdmin(ReadOnlyAdmin):
    list_display = ["id", "transaction", "account", "amount", "created_at"]


@admin.register(EscrowHold)
class EscrowHoldAdmin(ReadOnlyAdmin):
    list_display = [
        "id",
        "status",
        "payer_organization",
        "payee_organization",
        "amount",
        "released_amount",
        "refunded_amount",
        "commission_amount",
        "created_at",
    ]
    list_filter = ["status"]


@admin.register(Payment)
class PaymentAdmin(ReadOnlyAdmin):
    list_display = ["id", "status", "purpose", "organization", "amount", "provider", "created_at"]
    list_filter = ["status", "purpose", "provider"]


@admin.register(PayoutDestination)
class PayoutDestinationAdmin(ReadOnlyAdmin):
    list_display = ["id", "organization", "kind", "label", "last4", "is_active", "usable_from"]
    exclude = ["provider_token"]


@admin.register(Payout)
class PayoutAdmin(ReadOnlyAdmin):
    list_display = ["id", "status", "organization", "amount", "destination", "created_at"]
    list_filter = ["status"]


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyAdmin):
    list_display = ["provider", "event_id", "event_type", "outcome", "received_at"]
    list_filter = ["provider", "event_type"]
