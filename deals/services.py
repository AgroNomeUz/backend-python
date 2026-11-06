"""
deals/services.py
The one way another app records a deal.

A settled produce order, or the first settled delivery of a contract, becomes a
completed `Deal` so the seller's public profile and reviews work without
knowing produce exists. There is no confirm step: money changed hands and the
buyer accepted the goods, which is a stronger signal than the confirmation the
inquiry flow asks for.
"""

from django.utils import timezone

from users.models import Organization

from .models import Deal


def record_completed_sale(*, provider: Organization, customer: Organization) -> Deal:
    return Deal.objects.create(
        provider_organization=provider,
        customer_organization=customer,
        deal_type=Deal.DealType.SALE,
        status=Deal.Status.COMPLETED,
        completed_at=timezone.now(),
    )
