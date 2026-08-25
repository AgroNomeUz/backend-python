import uuid
from datetime import datetime, timedelta, timezone

import jwt

from django.conf import settings
from django.core.exceptions import ValidationError
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


class OptionalJWTBearer(JWTBearer):
    """
    Bearer authentication that never refuses the request.

    For endpoints a stranger may read but a member reads *more* of: a single
    listing is public while it is active and available, and any status at all
    to the organization that owns it. Ninja turns a `None` from `authenticate`
    into a 401, so a missing or invalid token has to come back as something
    truthy instead — `ANONYMOUS`, which views compare against.

    Deliberately silent about a bad token: this is not a place to tell a
    caller their session expired, and the endpoint behaves identically for a
    stranger and for a stale token. Endpoints that require a session keep
    using `JWTBearer`.
    """

    ANONYMOUS = "anonymous"

    async def __call__(self, request):
        """
        Override the whole call, not just `authenticate`.

        `HttpBearer.__call__` never reaches `authenticate` when the header is
        missing or isn't a bearer scheme — it returns None straight away,
        which ninja turns into a 401. That is precisely the case this class
        exists to allow, so the substitution has to happen one level up.
        """
        result = super().__call__(request)
        user = await result if result is not None else None
        return user or self.ANONYMOUS


def authenticated_user(request):
    """
    The user behind an `OptionalJWTBearer` request, or None if there isn't one.

    `request.auth` under that scheme is either a User or the ANONYMOUS
    sentinel; nothing outside this module should have to know which.
    """
    auth = getattr(request, "auth", None)
    return None if auth in (None, OptionalJWTBearer.ANONYMOUS) else auth
