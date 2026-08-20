"""
api/sms.py
The single place the application talks to an SMS gateway.

We are not signed up with Eskiz or Play Mobile yet, so `send_sms` has nothing
to deliver to. Everything above it — the OTP lifecycle, the rate limits, the
attempt caps — is real, because that is the part that has to be right before
a gateway is plugged in. Swapping in a provider means replacing the body of
one function.

**The message body is never logged.** The only messages this application
sends are passcodes, and a passcode in a log file is a live credential: read
it, post it to the public `/auth/otp/verify`, and you hold that person's
tokens. So the mock records only that a message went undelivered, to a masked
number.

Developing without a gateway still needs a way in, and there are exactly two,
both refused when `ENVIRONMENT == "production"` regardless of what the
environment asks for:

  * `OTP_TEST_PHONES` — numbers the operator lists explicitly. They receive
    the fixed `OTP_DEV_CODE`, which is a constant whoever configured it
    already knows, so returning it in the API response discloses nothing new.
  * `OTP_ECHO_CODES` — off by default. Writes real passcodes to the log for
    any number. This is the dangerous one; it exists so a developer can log
    in as an arbitrary number, and it is never sent over the API.
"""

import logging

from django.conf import settings

logger = logging.getLogger("api.sms")


def development_escape_hatches_allowed() -> bool:
    """
    Whether the dev-only disclosure paths may run at all.

    Checked here rather than only where the settings are read, so that a
    settings file (or a test) setting the flag directly still can't turn a
    production deployment into one that hands out other people's passcodes.
    """
    return settings.ENVIRONMENT != "production"


def mask_phone(phone: str) -> str:
    """
    A number recognisable to whoever owns it, useless to everyone else.

    Logs need to identify which delivery failed; they do not need the
    subscriber number.
    """
    if len(phone) < 8:
        return "*" * len(phone)
    return f"{phone[:4]}{'*' * (len(phone) - 6)}{phone[-2:]}"


def send_sms(phone: str, message: str) -> None:
    """
    Deliver `message` to `phone`. Mocked: nothing is sent (§0b).

    Deliberately ignores `message` — see the module docstring. When a real
    provider lands, this body becomes the provider call and still must not
    log the body.
    """
    logger.warning(
        "[sms] no gateway configured — message to %s was NOT delivered",
        mask_phone(phone),
    )


def echo_code_for_development(phone: str, code: str) -> None:
    """
    Write a live passcode to the log, if someone deliberately asked for that.

    The whole point of `OTP_ECHO_CODES` is to disclose a credential, so it is
    off by default, refused in production, and logged at WARNING with the
    reason attached — a log full of these should look wrong at a glance.
    """
    if not settings.OTP_ECHO_CODES or not development_escape_hatches_allowed():
        return
    logger.warning(
        "[sms] OTP_ECHO_CODES is enabled: the code for %s is %s. "
        "This discloses a live credential and must never be set in production.",
        phone,
        code,
    )


def is_test_phone(phone: str) -> bool:
    """
    True for numbers that skip the gateway and accept the fixed dev code.

    The whitelist is empty by default and ignored in production, so the fixed
    code can only ever reach a number the operator named on purpose.
    """
    if not development_escape_hatches_allowed():
        return False
    return phone in set(settings.OTP_TEST_PHONES)
