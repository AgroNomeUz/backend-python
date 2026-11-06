"""
marketplace/jobs.py
The clock-driven half of the market. `run_market_jobs` calls `run_all`.

Every step re-checks the row under a lock, so this is safe to run as often as
you like and alongside live traffic. One order failing is logged and skipped:
it must not stop the sellers behind it from being paid. The count of those comes
back as `failed`, and `run_market_jobs` exits non-zero once everything has run.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .contract_services import (
    close_ended_contracts,
    generate_all_orders,
    sync_all_contracts,
)
from .models import Order, market_today
from .order_services import (
    SYSTEM,
    accept_order,
    expire_unconfirmed,
    expire_unpaid,
    refund_undispatched,
    retry_contract_funding,
)

logger = logging.getLogger(__name__)
Status = Order.Status


def _each(queryset, step, failures: list) -> int:
    done = 0
    for order in queryset:
        try:
            step(order)
        except Exception:
            logger.exception("Market job failed on order %s", order.pk)
            failures.append(order.pk)
        else:
            done += 1
    return done


def run_all() -> dict[str, int]:
    now = timezone.now()
    failures = []
    counts = {
        "expired_unconfirmed": _each(
            Order.objects.filter(
                status=Status.PENDING_CONFIRMATION, confirm_by__lt=now
            ),
            expire_unconfirmed,
            failures,
        ),
        # Before expiring anything unpaid, so a buyer who topped up in time keeps
        # their delivery.
        "contract_retries": _each(
            Order.objects.filter(
                status=Status.AWAITING_PAYMENT, contract__isnull=False
            ),
            retry_contract_funding,
            failures,
        ),
        "expired_unpaid": _each(
            Order.objects.filter(status=Status.AWAITING_PAYMENT, pay_by__lt=now),
            expire_unpaid,
            failures,
        ),
        "refunded_undispatched": _each(
            Order.objects.filter(
                status=Status.PAID,
                delivery_date__lt=market_today()
                - timedelta(days=settings.ORDER_DISPATCH_GRACE_DAYS),
            ),
            refund_undispatched,
            failures,
        ),
        "auto_accepted": _each(
            Order.objects.filter(
                status__in=[Status.DISPATCHED, Status.DELIVERED],
                acceptance_deadline__lt=now,
            ),
            lambda order: accept_order(SYSTEM, order),
            failures,
        ),
        "contract_orders_synced": sync_all_contracts(failures),
        "contract_orders": generate_all_orders(failures),
        "contracts_closed": close_ended_contracts(),
    }
    counts["failed"] = len(failures)
    return counts
