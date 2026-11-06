"""
marketplace/reliability.py
How dependable a seller has been, from what actually happened.

Stars are opinions; a factory that needs tomatoes every morning wants to know
whether they turned up. Everything here is derived from orders, so there is
nothing to fake without moving real money through the platform.

Over the trailing window, among orders a seller was responsible for. Orders the
buyer walked away from are left out, so a seller is not marked down for that.
"""

from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q, Sum
from django.utils import timezone

from users.models import Organization

from .models import Order, Side

WINDOW_DAYS = 90
Status = Order.Status


def _rate(part, whole) -> Decimal | None:
    if not whole:
        return None
    return (Decimal(part) / Decimal(whole)).quantize(Decimal("0.0001"))


def provider_reliability(organization: Organization) -> dict:
    since = timezone.now() - timedelta(days=WINDOW_DAYS)
    orders = Order.objects.filter(
        provider_organization=organization, created_at__gte=since
    )

    settled = orders.filter(status=Status.SETTLED)
    failed = orders.filter(
        Q(status__in=[Status.DECLINED, Status.EXPIRED, Status.CANCELLED]),
        cancelled_by=Side.PROVIDER,
    )
    delivered = settled.filter(delivered_at__isnull=False)

    settled_count = settled.count()
    failed_count = failed.count()
    totals = settled.aggregate(
        accepted=Sum("accepted_quantity"), ordered=Sum("ordered_quantity")
    )
    failed_quantity = failed.aggregate(total=Sum("ordered_quantity"))["total"] or 0

    zone = ZoneInfo(settings.MARKET_TIME_ZONE)
    on_time = sum(
        1
        for delivered_at, delivery_date in delivered.values_list(
            "delivered_at", "delivery_date"
        )
        if delivered_at.astimezone(zone).date() <= delivery_date
    )
    disputes = settled.filter(dispute__isnull=False).count()

    return {
        "window_days": WINDOW_DAYS,
        "orders": settled_count + failed_count,
        # What arrived and was accepted, out of everything the seller took on.
        "fill_rate": _rate(
            totals["accepted"] or 0, (totals["ordered"] or 0) + failed_quantity
        ),
        "on_time_rate": _rate(on_time, delivered.count()),
        "dispute_rate": _rate(disputes, settled_count),
    }

