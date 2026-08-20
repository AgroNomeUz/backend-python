"""
api/otp.py
The passcode lifecycle behind phone-first login.

Two operations, both synchronous because they are transactional ORM work:

  * `issue_otp`  — rate-limit, mint a code, hash it, hand it to the SMS seam
  * `verify_otp` — check a presented code against the newest live one

Everything that makes an OTP safe rather than merely present lives here, so
the views stay thin and there is one place to audit:

  * codes are stored hashed, never in plaintext, and burnt on first success
  * a wrong guess is counted; `OTP_MAX_ATTEMPTS` of them retire the code, so
    a six-digit secret can't be walked
  * requesting is limited three ways — a resend cooldown per number, an
    hourly cap per number, an hourly cap per IP — all counted from the
    `PhoneOtp` table (see the settings block for why not the cache)

Callers hand us an already-normalised phone (`users.services.normalize_phone`);
`+998 90 123 45 67` and `+998901234567` must not be two different buckets.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import get_random_string

from .models import PhoneOtp
from .sms import is_test_phone, send_sms


class OtpThrottled(Exception):
    """Too many requests for this number or from this address."""

    def __init__(self, retry_after: int, detail: str):
        super().__init__(detail)
        self.retry_after = retry_after
        self.detail = detail


class OtpInvalid(Exception):
    """The presented code is wrong, expired, spent, or was never issued."""


@dataclass
class IssuedOtp:
    """What the view needs back: when they may resend, and (DEBUG) the code."""

    otp: PhoneOtp
    retry_after: int
    debug_code: str | None


def _window_start() -> datetime:
    return timezone.now() - timedelta(hours=1)


def _generate_code(phone: str) -> tuple[str, bool]:
    """
    The code to send, and whether it is the DEBUG-only fixed dev code.

    Test numbers get a predictable code so the app is usable without an SMS
    gateway; everyone else gets `get_random_string`, which is CSPRNG-backed —
    `random` is not, and this is a credential.
    """
    if is_test_phone(phone):
        return settings.OTP_DEV_CODE, True
    return get_random_string(settings.OTP_CODE_LENGTH, "0123456789"), False


def issue_otp(phone: str, ip: str | None = None) -> IssuedOtp:
    """
    Send a fresh passcode to `phone`, or raise `OtpThrottled`.

    The three limits are checked cheapest-first and the cooldown speaks last
    in seconds the client can count down from.
    """
    now = timezone.now()
    window = _window_start()

    latest = PhoneOtp.objects.filter(phone=phone).order_by("-created_at").first()
    if latest is not None:
        elapsed = (now - latest.created_at).total_seconds()
        remaining = int(settings.OTP_RESEND_COOLDOWN_SECONDS - elapsed)
        if remaining > 0:
            raise OtpThrottled(
                remaining,
                f"A code was already sent. Try again in {remaining} seconds.",
            )

    per_phone = PhoneOtp.objects.filter(phone=phone, created_at__gte=window).count()
    if per_phone >= settings.OTP_MAX_REQUESTS_PER_PHONE_PER_HOUR:
        raise OtpThrottled(
            3600,
            "Too many codes requested for this number. Try again later.",
        )

    if ip:
        per_ip = PhoneOtp.objects.filter(ip=ip, created_at__gte=window).count()
        if per_ip >= settings.OTP_MAX_REQUESTS_PER_IP_PER_HOUR:
            raise OtpThrottled(
                3600,
                "Too many codes requested from this address. Try again later.",
            )

    code, is_dev_code = _generate_code(phone)
    otp = PhoneOtp.objects.create(
        phone=phone,
        code_hash=make_password(code),
        expires_at=now + timedelta(seconds=settings.OTP_TTL_SECONDS),
        ip=ip,
    )

    if not is_dev_code:
        minutes = settings.OTP_TTL_SECONDS // 60
        send_sms(phone, f"Agronome code: {code}. Valid for {minutes} minutes.")

    return IssuedOtp(
        otp=otp,
        retry_after=settings.OTP_RESEND_COOLDOWN_SECONDS,
        # Handed back only in DEBUG, so a developer without a gateway can log
        # in with a number that isn't on the test whitelist.
        debug_code=code if settings.DEBUG else None,
    )


def verify_otp(phone: str, code: str) -> PhoneOtp:
    """
    Consume the newest live code for `phone`, or raise `OtpInvalid`.

    Only the newest code is checkable: requesting a new one silently retires
    the old, which is what a user pressing "resend" expects. The row is
    locked for the read-modify-write so two racing attempts can't each see
    the same attempt count.

    The failure is raised *after* the transaction commits, deliberately — an
    exception inside `atomic()` would roll back the very attempt counter that
    makes guessing expensive.
    """
    with transaction.atomic():
        otp = (
            PhoneOtp.objects
            .select_for_update()
            .filter(phone=phone, verified_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if otp is None or otp.is_expired:
            failure = "The code has expired. Request a new one."
        elif otp.attempts >= settings.OTP_MAX_ATTEMPTS:
            failure = "Too many incorrect attempts. Request a new code."
        else:
            otp.attempts += 1
            if check_password(code, otp.code_hash):
                # Burnt: a code proves ownership of a number exactly once.
                otp.verified_at = timezone.now()
                otp.save(update_fields=["attempts", "verified_at"])
                failure = None
            else:
                otp.save(update_fields=["attempts"])
                failure = "Invalid code"

    if failure is not None:
        raise OtpInvalid(failure)
    return otp


def claim_signup_otp(otp_public_id, phone: str) -> PhoneOtp:
    """
    Spend the verification that a signup token refers to.

    This is what makes the token single-use, and it is a conditional UPDATE
    rather than a read-then-write: two requests arriving with the same token
    must not both create an organization.
    """
    with transaction.atomic():
        claimed = (
            PhoneOtp.objects
            .filter(
                public_id=otp_public_id,
                phone=phone,
                verified_at__isnull=False,
                signup_claimed_at__isnull=True,
            )
            .update(signup_claimed_at=timezone.now())
        )
        if not claimed:
            raise OtpInvalid("This signup token has already been used.")
        return PhoneOtp.objects.get(public_id=otp_public_id)
