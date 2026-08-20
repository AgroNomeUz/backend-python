"""
api/views.py
The API root and the authentication endpoints.

Phone + OTP is the front door (§0.4): `otp/request` sends a code and says
whether the number is known, `otp/verify` either logs the caller in or hands
back a signup token, and `org/signup` exchanges that token for a new
organization — so a number is proven exactly once. Email + password is kept
for admins and back-office, but public password signup is gone: the only way
an account is created is an org admin signing up, or an admin registering a
member (§0.2).
"""

import jwt
from asgiref.sync import sync_to_async
from django.contrib.auth import aauthenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address
from django.db import transaction
from django.utils import timezone
from ninja import NinjaAPI
from ninja.errors import HttpError

from core.audit import diff, request_context, snapshot
from core.models import ActivityLog
from equipment.views import activity_router, assets_router, catalog_router
from users.models import OrgPermission, Organization, Region, User
from users.services import normalize_phone, username_for_phone
from users.views import members_router
from .auth import (
    ACCESS_TOKEN_EXPIRE_SECONDS,
    REFRESH_TOKEN_LIFETIME,
    JWTBearer,
    create_access_token,
    create_refresh_token,
    create_signup_token,
    decode_refresh_token,
    decode_signup_token,
)
from .models import RefreshToken
from .otp import OtpInvalid, OtpThrottled, claim_signup_otp, issue_otp, verify_otp
from .public import public_router
from .schemas import (
    AuthOut,
    LoginIn,
    LogoutIn,
    OrgSignUpIn,
    OtpRequestIn,
    OtpRequestOut,
    OtpThrottledOut,
    OtpVerifyIn,
    OtpVerifyOut,
    PasswordChangeIn,
    RefreshIn,
    TokenOut,
)

api = NinjaAPI(title="Agro API", version="1.0.0", auth=JWTBearer())

api.add_router("/catalog", catalog_router)
api.add_router("/assets", assets_router)
api.add_router("/activity", activity_router)
api.add_router("/members", members_router)
api.add_router("/public", public_router)


# ── helpers ──────────────────────────────────────────────────────────────────

async def _issue_tokens(user: User) -> dict:
    access = create_access_token(user.public_id)
    refresh = create_refresh_token(user.public_id)
    await RefreshToken.objects.acreate(
        user=user,
        token=refresh,
        expires_at=timezone.now() + REFRESH_TOKEN_LIFETIME,
    )
    org = None
    is_owner = False
    if user.organization_id:
        org_obj = await (
            Organization.objects
            .select_related("region")
            .aget(pk=user.organization_id)
        )
        # Derived from the org we already fetched: `user.is_organization_owner`
        # would hit the database, and this coroutine runs in an async context.
        is_owner = org_obj.owner_id == user.pk
        region = None
        if org_obj.region:
            region = {
                "id": org_obj.region.public_id,
                "name": org_obj.region.name,
                "code": org_obj.region.code,
            }
        org = {
            "id": org_obj.public_id,
            "name": org_obj.name,
            "address": org_obj.address,
            "region": region,
            "tax_number": org_obj.tax_number,
            "phone": org_obj.phone,
            "email": org_obj.email,
            "entity_type": org_obj.entity_type,
            "is_verified": org_obj.is_verified,
        }
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_SECONDS,
        "user": {
            "id": user.public_id,
            "username": user.username,
            "email": user.email,
            "phone": user.phone,
            "full_name": user.get_full_name(),
            "telegram": user.telegram,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "is_owner": is_owner,
            "permissions": (
                list(OrgPermission.values) if is_owner else sorted(user.permissions or [])
            ),
            "must_change_password": user.must_change_password,
            "organization": org,
        },
    }


def _client_ip(request) -> str | None:
    """
    The caller's address, or None if it isn't one.

    `X-Forwarded-For` is client-controlled, and `PhoneOtp.ip` is a Postgres
    `inet` column — an unvalidated header would turn a junk value into a 500
    rather than a rate-limit entry.
    """
    ip = (request_context(request) or {}).get("ip")
    try:
        validate_ipv46_address(ip)
    except (ValidationError, TypeError):
        return None
    return ip


def _looks_like_phone(value: str) -> bool:
    """
    True for something that could only be a phone number.

    Usernames are alphanumeric and phone numbers never contain letters, so
    this cleanly separates the two identifiers `/auth/login` accepts without
    an extra lookup for ordinary username logins.
    """
    return bool(value) and not any(char.isalpha() for char in value)


ORG_AUDIT_FIELDS = ["name", "address", "phone", "email", "tax_number", "entity_type"]


def _create_org_account(request, data: OrgSignUpIn, phone: str, otp_id: str) -> User:
    """
    Spend a verified phone on a new organization and its admin.

    The only public account-creating path (§0.2). Everything is one
    transaction, the signup token included: if the organization can't be
    created the token is un-spent and the client can retry with it.

    The admin gets no password — they log in with OTP. Email and password
    remain available (§0.4) but are set later, from inside the account.
    """
    entity_type = data.entity_type or Organization.EntityType.INDIVIDUAL
    if entity_type not in Organization.EntityType.values:
        raise HttpError(400, "Unknown entity type")

    full_name = data.full_name.strip()
    organization_name = data.organization_name.strip()
    if not full_name:
        raise HttpError(400, "Your name is required")
    if not organization_name:
        raise HttpError(400, "An organization name is required")

    with transaction.atomic():
        region = None
        if data.region_id is not None:
            region = Region.objects.filter(public_id=data.region_id).first()
            if region is None:
                raise HttpError(400, "Unknown region")

        # Burns the token first: two requests racing with the same one must
        # produce one organization, not two.
        try:
            claim_signup_otp(otp_id, phone)
        except OtpInvalid as exc:
            raise HttpError(401, str(exc))

        if User.objects.filter(phone=phone).exists():
            raise HttpError(400, "Phone number already registered")

        user = User.objects.create_user(
            username=username_for_phone(phone),
            phone=phone,
            full_name=full_name,
        )
        org = Organization.objects.create(
            owner=user,
            name=organization_name,
            region=region,
            entity_type=entity_type,
            # Seeded from the admin's number so the org is contactable before
            # anyone edits the profile; `PATCH /org` can change it.
            phone=phone,
        )
        user.organization = org
        user.save(update_fields=["organization"])

        # §0.3 — an organization's history starts with its own creation.
        ActivityLog.record(
            organization=org,
            actor=user,
            action=ActivityLog.Action.CREATED,
            target=org,
            changes=diff({}, snapshot(org, ORG_AUDIT_FIELDS)),
            context=request_context(request),
        )

    return user


_create_org_account_async = sync_to_async(_create_org_account)
_issue_otp_async = sync_to_async(issue_otp)
_verify_otp_async = sync_to_async(verify_otp)


# ── auth endpoints ────────────────────────────────────────────────────────────

@api.post(
    "/auth/otp/request",
    response={200: OtpRequestOut, 429: OtpThrottledOut},
    auth=None,
    tags=["Auth"],
)
async def otp_request(request, data: OtpRequestIn):
    """
    Send a login code to a phone number.

    Never creates an account: `account_exists` only tells the app which
    screen to show next, and an unknown number is routed to org signup
    (§0.2). The SMS gateway is mocked — the code goes to the server log
    (§0b) — but the rate limits are real.
    """
    phone = normalize_phone(data.phone)
    if not phone:
        raise HttpError(400, "A phone number is required")

    try:
        issued = await _issue_otp_async(phone, _client_ip(request))
    except OtpThrottled as exc:
        return 429, {"detail": exc.detail, "retry_after": exc.retry_after}

    return 200, {
        "retry_after": issued.retry_after,
        # Deactivated accounts count as existing: sending their owner to a
        # create-organization screen would only fail later on a taken phone.
        "account_exists": await User.objects.filter(phone=phone).aexists(),
        "debug_code": issued.debug_code,
    }


@api.post("/auth/otp/verify", response=OtpVerifyOut, auth=None, tags=["Auth"])
async def otp_verify(request, data: OtpVerifyIn):
    """
    Exchange a code for either a session or a signup token.

    A correct code proves the caller owns the number; what that is worth
    depends on whether anyone holds it already.
    """
    phone = normalize_phone(data.phone)
    if not phone:
        raise HttpError(400, "A phone number is required")

    try:
        otp = await _verify_otp_async(phone, data.code)
    except OtpInvalid as exc:
        raise HttpError(400, str(exc))

    user = await User.objects.filter(phone=phone).afirst()
    if user is None:
        return {
            "status": "no_account",
            "signup_token": create_signup_token(phone, otp.public_id),
        }
    if not user.is_active:
        raise HttpError(403, "Account is disabled")

    return {"status": "authenticated", **await _issue_tokens(user)}


@api.post("/auth/org/signup", response=AuthOut, auth=None, tags=["Auth"])
async def org_signup(request, data: OrgSignUpIn):
    """
    Create an organization and its admin from a verified phone number.

    The new user owns the organization, and an owner implicitly holds every
    permission code (§0.2) — so there is no role to assign here.
    """
    try:
        payload = decode_signup_token(data.signup_token)
    except jwt.ExpiredSignatureError:
        raise HttpError(401, "Signup token expired — verify your number again")
    except jwt.InvalidTokenError:
        raise HttpError(401, "Invalid signup token")

    user = await _create_org_account_async(
        request, data, payload["phone"], payload["otp_id"]
    )
    return await _issue_tokens(user)


@api.post("/auth/login", response=AuthOut, auth=None, tags=["Auth"])
async def login(request, data: LoginIn):
    """
    The secondary route: phone, email or username, plus a password (§0.4).

    Members registered by an admin start here with their one-time password;
    everyone else uses OTP.
    """
    identifier = data.username.strip()
    if "@" in identifier:
        try:
            found = await User.objects.aget(email__iexact=identifier)
            identifier = found.username
        except User.DoesNotExist:
            raise HttpError(401, "Invalid credentials")
    elif _looks_like_phone(identifier):
        # Falls through to a username attempt when no account holds the
        # number, so a numeric username still works.
        found = await User.objects.filter(phone=normalize_phone(identifier)).afirst()
        if found is not None:
            identifier = found.username

    user = await aauthenticate(request, username=identifier, password=data.password)
    if user is None:
        raise HttpError(401, "Invalid credentials")
    if not user.is_active:
        raise HttpError(403, "Account is disabled")

    return await _issue_tokens(user)


def _change_password(user: User, current_password: str, new_password: str) -> None:
    """
    Replace a user's password and invalidate every session built on the old one.

    Sync because it touches password hashing, the project's password
    validators and a bulk update — all of which want the ORM's sync path.
    """
    if not user.check_password(current_password):
        raise HttpError(400, "Current password is incorrect")
    if current_password == new_password:
        raise HttpError(400, "The new password must differ from the current one")

    try:
        validate_password(new_password, user)
    except ValidationError as exc:
        raise HttpError(400, " ".join(exc.messages))

    with transaction.atomic():
        user.set_password(new_password)
        # Clears the flag set when an admin issued a one-time password.
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        # The caller gets a fresh pair below; everything else is now stale.
        RefreshToken.objects.filter(user=user, is_revoked=False).update(
            is_revoked=True
        )


_change_password_async = sync_to_async(_change_password)


@api.post("/auth/password/change", response=AuthOut, tags=["Auth"])
async def change_password(request, data: PasswordChangeIn):
    """
    Set your own password — the step that clears `must_change_password`.

    Employees added by an org admin sign in with a one-time password and land
    here. All existing refresh tokens are revoked and a fresh pair is issued,
    so other devices have to log in again.
    """
    user = request.auth
    await _change_password_async(user, data.current_password, data.new_password)
    return await _issue_tokens(user)


@api.post("/auth/token/refresh", response=TokenOut, auth=None, tags=["Auth"])
async def token_refresh(request, data: RefreshIn):
    try:
        decode_refresh_token(data.refresh_token)
    except jwt.ExpiredSignatureError:
        raise HttpError(401, "Refresh token expired")
    except jwt.InvalidTokenError:
        raise HttpError(401, "Invalid refresh token")

    try:
        stored = await (
            RefreshToken.objects
            .select_related("user")
            .aget(token=data.refresh_token, is_revoked=False)
        )
    except RefreshToken.DoesNotExist:
        raise HttpError(401, "Refresh token not found or already revoked")

    if stored.expires_at < timezone.now():
        raise HttpError(401, "Refresh token expired")

    # Rotate: revoke old, issue new pair
    stored.is_revoked = True
    await stored.asave(update_fields=["is_revoked"])

    user = stored.user
    access = create_access_token(user.public_id)
    refresh = create_refresh_token(user.public_id)
    await RefreshToken.objects.acreate(
        user=user,
        token=refresh,
        expires_at=timezone.now() + REFRESH_TOKEN_LIFETIME,
    )

    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_SECONDS,
    }


@api.post("/auth/logout", response={204: None}, tags=["Auth"])
async def logout(request, data: LogoutIn):
    """
    End this session by revoking its refresh token.

    Scoped to the caller, so a stolen token can't be revoked by someone else,
    and idempotent — logging out twice, or with a token that has already
    rotated away, is still a 204. The access token stays valid for its
    remaining minutes; it is stateless by design.
    """
    await RefreshToken.objects.filter(
        user=request.auth, token=data.refresh_token, is_revoked=False
    ).aupdate(is_revoked=True)
    return 204, None
