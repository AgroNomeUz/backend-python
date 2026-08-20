"""
api/sms.py
The single place the application talks to an SMS gateway.

We are not signed up with Eskiz or Play Mobile yet, so `send_sms` writes the
message to the log and returns. Everything above it — the OTP lifecycle, the
rate limits, the attempt caps — is real, because that is the part that has to
be right before a gateway is plugged in. Swapping in a provider means
replacing the body of one function.

Development without a gateway needs a way to actually log in, so under
`DEBUG` a whitelist of test numbers (`OTP_TEST_PHONES`) always receives the
fixed `OTP_DEV_CODE` and no message is sent. The whitelist is empty by
default and the branch is `DEBUG`-gated, so production cannot inherit it.
"""

import logging

from django.conf import settings

logger = logging.getLogger("api.sms")


def send_sms(phone: str, message: str) -> None:
    """Deliver `message` to `phone`. Mocked: logged, never sent (§0b)."""
    logger.info("[sms] to=%s body=%s", phone, message)


def is_test_phone(phone: str) -> bool:
    """
    True for numbers that skip the gateway and accept the fixed dev code.

    Only ever true in `DEBUG` — a misconfigured production environment can't
    hand out a known passcode.
    """
    return bool(settings.DEBUG) and phone in set(settings.OTP_TEST_PHONES)
