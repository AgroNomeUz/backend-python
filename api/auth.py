import uuid
from datetime import datetime, timedelta, timezone

import jwt

from django.conf import settings
from ninja.security import HttpBearer

ACCESS_TOKEN_LIFETIME = timedelta(minutes=30)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)
ACCESS_TOKEN_EXPIRE_SECONDS = int(ACCESS_TOKEN_LIFETIME.total_seconds())


def _signup_token_lifetime() -> timedelta:
    return timedelta(seconds=settings.SIGNUP_TOKEN_TTL_SECONDS)


def _encode(payload: dict) -> str:
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def _decode(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])


def create_access_token(user_public_id) -> str:
    now = datetime.now(timezone.utc)
    return _encode({
        "user_id": str(user_public_id),
        "type": "access",
        "iat": now,
        "exp": now + ACCESS_TOKEN_LIFETIME,
    })


def create_refresh_token(user_public_id) -> str:
    now = datetime.now(timezone.utc)
    return _encode({
        "user_id": str(user_public_id),
        "type": "refresh",
        # `iat`/`exp` have one-second resolution, so without a nonce two
        # logins in the same second encode to the identical string — and
        # `RefreshToken.token` is unique, which turned that into a 500.
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + REFRESH_TOKEN_LIFETIME,
    })


def decode_access_token(token: str) -> dict:
    payload = _decode(token)
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Not an access token")
    return payload


def decode_refresh_token(token: str) -> dict:
    payload = _decode(token)
    if payload.get("type") != "refresh":
        raise jwt.InvalidTokenError("Not a refresh token")
    return payload


def create_signup_token(phone: str, otp_public_id) -> str:
    """
    Proof that the holder owns `phone`, good for one organization signup.

    Carries the verified `PhoneOtp` row rather than standing alone: expiry is
    in the token, but *single use* is a fact about that row
    (`PhoneOtp.signup_claimed_at`), which is the only place two concurrent
    requests can be told apart.
    """
    now = datetime.now(timezone.utc)
    return _encode({
        "phone": phone,
        "otp_id": str(otp_public_id),
        "type": "signup",
        "iat": now,
        "exp": now + _signup_token_lifetime(),
    })


def decode_signup_token(token: str) -> dict:
    payload = _decode(token)
    if payload.get("type") != "signup":
        raise jwt.InvalidTokenError("Not a signup token")
    return payload


class JWTBearer(HttpBearer):
    async def authenticate(self, request, token: str):
        try:
            payload = decode_access_token(token)
            from users.models import User
            return await User.objects.aget(
                public_id=payload["user_id"], is_active=True
            )
        except Exception:
            return None
