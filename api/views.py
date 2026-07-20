import jwt
from asgiref.sync import sync_to_async
from django.contrib.auth import aauthenticate
from django.db import transaction
from django.utils import timezone
from ninja import NinjaAPI
from ninja.errors import HttpError

from equipment.views import activity_router, assets_router, catalog_router
from users.models import Organization, User
from .auth import (
    ACCESS_TOKEN_EXPIRE_SECONDS,
    REFRESH_TOKEN_LIFETIME,
    JWTBearer,
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
)
from .models import RefreshToken
from .schemas import AuthOut, LoginIn, RefreshIn, SignUpIn, TokenOut

api = NinjaAPI(title="Agro API", version="1.0.0", auth=JWTBearer())

api.add_router("/catalog", catalog_router)
api.add_router("/assets", assets_router)
api.add_router("/activity", activity_router)


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
    if user.organization_id:
        org_obj = await (
            Organization.objects
            .select_related("region")
            .aget(pk=user.organization_id)
        )
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
            "first_name": user.first_name,
            "last_name": user.last_name,
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
            email=data.email,
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
    if await User.objects.filter(email=data.email).aexists():
        raise HttpError(400, "Email already registered")

    user = await _create_user_and_org_async(data)
    return await _issue_tokens(user)


@api.post("/auth/login", response=AuthOut, auth=None, tags=["Auth"])
async def login(request, data: LoginIn):
    # Support login with email or username
    identifier = data.username
    if "@" in identifier:
        try:
            found = await User.objects.aget(email=identifier)
            identifier = found.username
        except User.DoesNotExist:
            raise HttpError(401, "Invalid credentials")

    user = await aauthenticate(request, username=identifier, password=data.password)
    if user is None:
        raise HttpError(401, "Invalid credentials")
    if not user.is_active:
        raise HttpError(403, "Account is disabled")

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
