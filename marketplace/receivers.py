"""Connects billing's signals to the marketplace, so billing need not import it."""

from django.dispatch import receiver

from billing.models import Payment
from billing.signals import payment_succeeded

from .order_services import order_payment_received


@receiver(payment_succeeded, sender=Payment)
def on_payment_succeeded(sender, payment: Payment, **kwargs) -> None:
    order_payment_received(payment)
