"""
api/models.py
Credentials the auth endpoints hand out and take back: refresh tokens, and
the one-time passcodes that back phone-first login.
"""

from django.db import models
from django.conf import settings
from django.utils import timezone

from core.models import PublicIdModel


class RefreshToken(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="refresh_tokens",
    )
    token = models.TextField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_revoked = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["token"])]

    def __str__(self):
        return f"RefreshToken(user={self.user_id}, revoked={self.is_revoked})"


class PhoneOtp(PublicIdModel):
    """
    One issued passcode, and the proof of phone ownership it becomes.

    A row is created by `POST /auth/otp/request` and lives out one of three
    endings: it expires, it is verified and logs someone in, or — when the
    number belongs to nobody yet — it is verified and then spent exactly once
    by `POST /auth/org/signup`. `signup_claimed_at` is what makes the signup
    token single-use, in the database rather than in a cache that a second
    process wouldn't see.

    The code is stored hashed. A six-digit secret is trivially brute-forced
    from a database dump, and these rows are also the rate-limiting substrate
    (see `api/otp.py`), so they outlive the login they authorise.

    There is deliberately no FK to `User`: at request time the number may not
    belong to an account at all, and §0.2 says an unknown phone must never
    silently create one.
    """

    phone = models.CharField(max_length=32, db_index=True)
    code_hash = models.CharField(max_length=128)

    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(
        default=0,
        help_text="Verification attempts against this code; capped to stop guessing",
    )
    verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set once the right code was presented; the code is then burnt",
    )
    signup_claimed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when the signup token minted from this row was exchanged",
    )

    # Kept for the per-IP request cap, which is counted off this table.
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Phone OTP"
        verbose_name_plural = "Phone OTPs"
        indexes = [
            models.Index(fields=["phone", "-created_at"]),
            models.Index(fields=["ip", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"PhoneOtp({self.phone}, expires={self.expires_at:%Y-%m-%d %H:%M})"

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()
