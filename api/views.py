import jwt
from asgiref.sync import sync_to_async
from django.contrib.auth import aauthenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from ninja import NinjaAPI
from ninja.errors import HttpError

from equipment.views import activity_router, assets_router, catalog_router
from users.models import OrgPermission, Organization, User
from users.views import members_router
from .auth import (
    ACCESS_TOKEN_EXPIRE_SECONDS,
    REFRESH_TOKEN_LIFETIME,
    JWTBearer,
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
)
from .models import RefreshToken
from .public import public_router
from .schemas import (
    AuthOut,
    LoginIn,
    PasswordChangeIn,
    RefreshIn,
    SignUpIn,
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


def _create_user_and_org(data: SignUpIn) -> User:
    """
    Atomically creates a User and a blank default Organization they own.
    Organization fields (name, region, address, …) are filled in later.
    Runs inside sync_to_async so it can use Django ORM transactions.
    """
    with transaction.atomic():
        user = User.objects.create_user(
            username=data.username,
            # Emails are stored lowercase: they identify an account at login
            # and are unique case-insensitively (users.User Meta constraint).
            email=data.email.strip().lower(),
            password=data.password,
            first_name=data.first_name,
            last_name=data.last_name,
        )
        org = Organization.objects.create(owner=user)
        user.organization = org
        user.save(update_fields=["organization"])
    return user


_create_user_and_org_async = sync_to_async(_create_user_and_org)



# ── auth endpoints ────────────────────────────────────────────────────────────

@api.post("/auth/signup", response=AuthOut, auth=None, tags=["Auth"])
async def signup(request, data: SignUpIn):
    if await User.objects.filter(username=data.username).aexists():
        raise HttpError(400, "Username already taken")
    if await User.objects.filter(email__iexact=data.email.strip()).aexists():
        raise HttpError(400, "Email already registered")

    user = await _create_user_and_org_async(data)
    return await _issue_tokens(user)


@api.post("/auth/login", response=AuthOut, auth=None, tags=["Auth"])
async def login(request, data: LoginIn):
    # Support login with email or username
    identifier = data.username
    if "@" in identifier:
        try:
            found = await User.objects.aget(email__iexact=identifier.strip())
            identifier = found.username
        except User.DoesNotExist:
            raise HttpError(401, "Invalid credentials")

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
