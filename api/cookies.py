"""
api/cookies.py
The refresh token as an HttpOnly cookie, for browser clients that opt in.

Why: agronome.uz and console.agronome.uz are two origins. A refresh token in
`localStorage` on one is invisible to the other, so a user would log in twice
and be signed out of one while active in the other. A cookie scoped to the
parent domain (`AUTH_REFRESH_COOKIE_DOMAIN=.agronome.uz`) is shared by both,
and because it is HttpOnly no script on either site can read it.

How a client opts in: it sends `X-Auth-Transport: cookie` on the request.
The server then sets the cookie and answers with `refresh_token: null`, so the
token never sits in JavaScript memory at all. Without the header nothing
changes — the mobile app and any existing client keep receiving the token in
the body and sending it back in the body.

CSRF: the cookie is only ever *read* by `/auth/token/refresh` and
`/auth/logout`, and only when the request carries that custom header. A
cross-origin page cannot add a custom header without a CORS preflight, and the
preflight only succeeds for `CORS_ALLOWED_ORIGINS`. On top of that, a request
carrying an `Origin` the server doesn't trust is refused outright. A forged
form post can trigger neither condition, so it cannot spend the cookie.

The cookie's path is the auth prefix, so it never rides along on ordinary API
calls — those authenticate with the short-lived bearer access token, kept in
memory by the client.
"""

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from ninja.errors import HttpError

from .auth import REFRESH_TOKEN_LIFETIME

TRANSPORT_HEADER = "X-Auth-Transport"
COOKIE_TRANSPORT = "cookie"


def wants_cookie(request: HttpRequest) -> bool:
    """True when the client asked for the refresh token in a cookie."""
    return request.headers.get(TRANSPORT_HEADER, "").strip().lower() == COOKIE_TRANSPORT


def _trusted_origins() -> set[str]:
    return {o.rstrip("/") for o in settings.CORS_ALLOWED_ORIGINS}


def check_cookie_request(request: HttpRequest) -> None:
    """
    Refuse a cookie-authenticated refresh/logout from an untrusted origin.

    Browsers always send `Origin` on a cross-origin fetch, so its absence
    means a same-origin request or a non-browser client — neither can be a
    CSRF vehicle. A present one must be on the allow-list (or be the API's
    own host).
    """
    origin = request.headers.get("Origin")
    if not origin:
        return
    origin = origin.rstrip("/")
    own = f"{request.scheme}://{request.get_host()}"
    if origin != own and origin not in _trusted_origins():
        raise HttpError(403, "Origin not allowed")


def refresh_cookie_value(request: HttpRequest) -> str | None:
    return request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME) or None


def set_refresh_cookie(response: HttpResponse, token: str) -> None:
    response.set_cookie(
        settings.AUTH_REFRESH_COOKIE_NAME,
        token,
        max_age=int(REFRESH_TOKEN_LIFETIME.total_seconds()),
        domain=settings.AUTH_REFRESH_COOKIE_DOMAIN,
        path=settings.AUTH_REFRESH_COOKIE_PATH,
        secure=settings.AUTH_REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
    )


def clear_refresh_cookie(response: HttpResponse) -> None:
    response.delete_cookie(
        settings.AUTH_REFRESH_COOKIE_NAME,
        domain=settings.AUTH_REFRESH_COOKIE_DOMAIN,
        path=settings.AUTH_REFRESH_COOKIE_PATH,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
    )
