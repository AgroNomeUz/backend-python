import jwt
from datetime import datetime, timedelta, timezone

from django.conf import settings
from ninja.security import HttpBearer

ACCESS_TOKEN_LIFETIME = timedelta(minutes=30)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)
ACCESS_TOKEN_EXPIRE_SECONDS = int(ACCESS_TOKEN_LIFETIME.total_seconds())


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
