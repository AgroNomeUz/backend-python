"""
billing/signals.py
How billing tells the rest of the system that money arrived, without importing
it. Sent inside the transaction that credited the wallet, so a receiver that
raises rolls the credit back and the provider's retry tries again. A receiver
must therefore raise only for real faults, never for "this order moved on":
the money stays in the wallet in that case, which is the safe place for it.
"""

from django.dispatch import Signal

# kwargs: payment
payment_succeeded = Signal()
