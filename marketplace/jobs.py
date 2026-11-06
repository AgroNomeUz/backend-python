"""
marketplace/jobs.py
The clock-driven half of the market. `run_market_jobs` calls `run_all`.

Every step re-checks the row under a lock, so this is safe to run as often as
you like and alongside live traffic. One order failing is logged and skipped:
it must not stop the sellers behind it from being paid.
"""

import logging

from django.utils import timezone

from .contract_services import close_ended_contracts, generate_all_orders
from .models import Order
from .order_services import (
    SYSTEM,
    accept_order,
    expire_unconfirmed,
    expire_unpaid,
    retry_contract_funding,
)

logger = logging.getLogger(__name__)
Status = Order.Status


def _each(queryset, step) -> int:
    done = 0
    for order in queryset:
        try:
            step(order)
        except Exception:
            logger.exception("Market job failed on order %s", order.pk)
        else:
            done += 1
    return done


def run_all() -> dict[str, int]:
    now = timezone.now()
    return {
        "expired_unconfirmed": _each(
            Order.objects.filter(
                status=Status.PENDING_CONFIRMATION, confirm_by__lt=now
            ),
            expire_unconfirmed,
        ),
        # Before expiring anything unpaid, so a buyer who topped up in time keeps
        # their delivery.
        "contract_retries": _each(
            Order.objects.filter(
                status=Status.AWAITING_PAYMENT, contract__isnull=False
            ),
            retry_contract_funding,
        ),
        "expired_unpaid": _each(
            Order.objects.filter(status=Status.AWAITING_PAYMENT, pay_by__lt=now),
            expire_unpaid,
        ),
        "auto_accepted": _each(
            Order.objects.filter(status=Status.DELIVERED, acceptance_deadline__lt=now),
            lambda order: accept_order(SYSTEM, order),
        ),
        "contract_orders": generate_all_orders(),
        "contracts_closed": close_ended_contracts(),
    }
