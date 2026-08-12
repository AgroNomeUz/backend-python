import jwt
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.exceptions import ValidationError
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
    """
    Bearer-token authentication for the whole API.

    `is_async` is not decoration: `HttpBearer.__call__` is sync and simply
    returns whatever `authenticate` gives it, so an async `authenticate`
    hands back a coroutine. Ninja only awaits that if the auth object
    advertises itself as async — without this flag it stores the un-awaited
    coroutine as `request.auth`.

    The query eagerly joins everything an authenticated request reads off the
    user, because an async view cannot lazily load a relation: `.organization`
    backs `caller_organization()` on every org-scoped endpoint, and
    `.owned_organization` backs `is_organization_owner`, which drives the
    permission checks and is serialised into member responses. Fetching them
    lazily inside async code raises SynchronousOnlyOperation; fetching them
    here also collapses three queries per request into one.
    """

    is_async = True

    async def authenticate(self, request, token: str):
        try:
            payload = decode_access_token(token)
        except jwt.InvalidTokenError:
            return None

        from users.models import User

        try:
            return await (
                User.objects
                .select_related(
                    "organization",
                    "organization__region",
                    "owned_organization",
                )
                .aget(public_id=payload["user_id"], is_active=True)
            )
        except (User.DoesNotExist, ValidationError, ValueError):
            # Unknown or deactivated user, or a token carrying something that
            # isn't a UUID — all indistinguishable to a caller: no auth.
            return None
