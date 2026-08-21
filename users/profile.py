"""
users/profile.py
The caller's own profile, and their organization's.

Everything under `/members` (see `users/views.py`) is one person administering
another and is gated on `users.manage`. This module is the other half: what a
member may do to *themselves*, plus the organization profile the whole roster
reads.

Three things here are worth knowing before changing anything:

  * `PATCH /users/me` carries **no permission check at all**, deliberately.
    `PATCH /members/{id}` needs `users.manage` and always will — but a member
    editing their own name is not administration, and routing it through the
    admin endpoint 403s every non-admin employee in the org.

  * A **phone change is a credential change**, so it costs a code sent to the
    new number. Phone is the OTP login identifier: an unverified swap on a
    typo locks the account out, and an unverified swap on purpose is an
    account takeover. `PATCH /users/me` therefore has no `phone` field.

  * `is_verified` and `entity_type` are missing from `OrganizationUpdateIn`
    rather than filtered out of it. The first drives the verified badge on
    public listings; the second is what ONEID verification promotes (§2b).
    A field that isn't in the schema can't be forgotten in a guard.

The async/sync split is the one documented at the top of `users/views.py`:
Django has no async `transaction.atomic()`, and a write plus its audit row must
commit or roll back together, so every write is a sync `_apply_*` under
`atomic()` that the view awaits through `sync_to_async`.
"""

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Count
from ninja import Router
from ninja.errors import HttpError

from api.otp import OtpInvalid, OtpThrottled, issue_otp, verify_otp
from api.schemas import OtpThrottledOut, UserOut
from core.audit import client_ip, diff, request_context, snapshot
from core.models import ActivityLog

from .models import OrgPermission, Organization, Region, User
from .permissions import caller_organization, require_perm
from .schemas import (
    OrganizationDetailOut,
    OrganizationUpdateIn,
    PhoneChangeIn,
    PhoneChangeOut,
    PhoneChangeVerifyIn,
    SelfUpdateIn,
)
from .services import normalize_phone
from .views import check_phone_available, log_member_activity, member_snapshot

me_router = Router(tags=["Profile"])
org_router = Router(tags=["Organization"])

# The organization's audit-visible surface. `region` is a FK — `to_jsonable`
# renders it through `str()`, which is `"Name (CODE)"` and is what a human
# reading the history wants to see.
ORG_AUDIT_FIELDS = [
    "name",
    "address",
    "region",
    "phone",
    "email",
    "tax_number",
    "entity_type",
]


# ── the user payload every profile-shaped response shares ─────────────────────

async def user_payload(user: User) -> dict:
    """
    The caller's profile with their organization nested — `api.schemas.UserOut`.

    One builder, two callers: the auth responses (`_issue_tokens`) and
    `GET /users/me`. They have to agree — the frontend caches what login
    returned and re-reads this endpoint to refresh it, so any drift between
    them shows up as a field that silently reverts.

    The organization is re-fetched with its region joined rather than followed
    off `user`: an async response cannot lazily load a relation, and `user` may
    have arrived from `aauthenticate`, which joins nothing.
    """
    org = None
    is_owner = False

    if user.organization_id:
        org_obj = await (
            Organization.objects
            .select_related("region")
            .aget(pk=user.organization_id)
        )
        # Derived from the org we already hold: `user.is_organization_owner`
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
    }


# ── /users/me ─────────────────────────────────────────────────────────────────

@me_router.get("/me", response=UserOut)
async def get_current_user(request):
    """
    The caller's profile, with the nested organization and their effective
    `is_owner` + `permissions`.

    Identical in shape to the `user` object every auth response carries, so the
    frontend can refresh its cached session from one call. Unlike
    `GET /members/me` this does not require an organization — a user without
    one reads back `organization: null` rather than a 403, matching what login
    already does for the same account.
    """
    return await user_payload(request.auth)


def _apply_update_self(request, organization, user: User, data: SelfUpdateIn) -> User:
    """Sync transactional core of `update_current_user`."""
    fields = data.dict(exclude_unset=True)
    # An explicit null would `setattr(None)` onto a non-null CharField; the
    # fields here are all blank-able, so "clear it" means the empty string.
    fields = {name: ("" if value is None else value) for name, value in fields.items()}
    if not fields:
        return user

    if "email" in fields:
        email = fields["email"].strip().lower()
        # Mirrors the `user_email_ci_unique` constraint so a clash is a 400
        # rather than an IntegrityError 500 — same check as member creation.
        clash = User.objects.filter(email__iexact=email).exclude(pk=user.pk)
        if email and clash.exists():
            raise HttpError(400, "Email already registered")
        fields["email"] = email

    before = member_snapshot(user)
    for name, value in fields.items():
        setattr(user, name, value)

    changes = diff(before, member_snapshot(user))
    if not changes:
        return user

    with transaction.atomic():
        user.save(update_fields=list(fields))
        log_member_activity(
            request, organization, ActivityLog.Action.UPDATED, user, changes=changes
        )

    return user


@me_router.patch("/me", response=UserOut)
async def update_current_user(request, data: SelfUpdateIn):
    """
    Edit your own `full_name`, `telegram` and `email`.

    No permission code is required — this is the self-edit path, and gating it
    on `users.manage` (as `PATCH /members/{id}` does, correctly, for editing
    *other* people) would lock out every ordinary member.

    Not editable here: `permissions` and `is_active`, which are an admin's to
    set (§2b), and `phone`, which is a credential — see `POST /users/me/phone`.

    Unlike `GET /users/me`, this requires the caller to belong to an
    organization — `caller_organization` 403s a no-org account rather than
    letting the edit through. Every write is audited (§0.3) and
    `ActivityLog.organization` is non-nullable, so there is no organization to
    hang the audit row on for such an account; the 403 here is a deliberate
    floor, not an oversight that GET simply doesn't share.
    """
    organization = caller_organization(request)
    user = await sync_to_async(_apply_update_self)(
        request, organization, request.auth, data
    )
    return await user_payload(user)


def _check_new_phone(user: User, raw: str | None) -> str:
    """
    Validate a proposed new number before a code is spent on it.

    Also the reason no `purpose` column is needed on `PhoneOtp`: this
    establishes that the number belongs to no account, so a code issued for it
    cannot authenticate anybody. The most it can be exchanged for is the
    signup token that `/auth/otp/verify` hands anyone who asks about an
    unclaimed number.
    """
    phone = normalize_phone(raw)
    if not phone:
        raise HttpError(400, "A phone number is required")
    if phone == user.phone:
        raise HttpError(400, "This is already your phone number")
    check_phone_available(phone, exclude=user)
    return phone


@me_router.post(
    "/me/phone",
    response={200: PhoneChangeOut, 429: OtpThrottledOut},
)
async def start_phone_change(request, data: PhoneChangeIn):
    """
    Begin a phone change: send a code to the **new** number.

    To the new one, not the current one — what has to be proved is that the
    caller can receive SMS at the number they are moving to. Nothing is
    written until `POST /users/me/phone/verify` succeeds.

    Shares the OTP limits with login (`api/otp.py`): the resend cooldown, the
    hourly cap per number and the hourly cap per address all apply, and a
    refusal is a `429` carrying the `retry_after` the UI counts down from.
    """
    phone = await sync_to_async(_check_new_phone)(request.auth, data.phone)

    try:
        issued = await sync_to_async(issue_otp)(phone, client_ip(request))
    except OtpThrottled as exc:
        return 429, {"detail": exc.detail, "retry_after": exc.retry_after}

    return 200, {"retry_after": issued.retry_after}


def _apply_change_phone(request, organization, user: User, phone: str) -> User:
    """Sync transactional core of `confirm_phone_change`."""
    before = member_snapshot(user)
    user.phone = phone

    with transaction.atomic():
        # Re-checked inside the transaction: the code's round trip through a
        # handset is long enough for someone else to have claimed the number.
        check_phone_available(phone, exclude=user)
        user.save(update_fields=["phone"])
        log_member_activity(
            request,
            organization,
            ActivityLog.Action.UPDATED,
            user,
            changes=diff(before, member_snapshot(user)),
        )

    return user


@me_router.post("/me/phone/verify", response=UserOut)
async def confirm_phone_change(request, data: PhoneChangeVerifyIn):
    """
    Finish a phone change by presenting the code sent to the new number.

    The swap happens inside an authenticated session with a known
    organization, so unlike `/auth/otp/*` it is audited (§0.3) — a login
    identifier moving is exactly the kind of change an org needs to be able to
    look up later.
    """
    organization = caller_organization(request)
    phone = await sync_to_async(_check_new_phone)(request.auth, data.phone)

    try:
        await sync_to_async(verify_otp)(phone, data.code)
    except OtpInvalid as exc:
        raise HttpError(400, str(exc))

    user = await sync_to_async(_apply_change_phone)(
        request, organization, request.auth, phone
    )
    return await user_payload(user)


# ── /org ──────────────────────────────────────────────────────────────────────

@org_router.get("", response=OrganizationDetailOut)
async def get_organization(request):
    """
    The caller's organization profile. Readable by any member — reads are
    never permission-gated, only writes are.
    """
    organization = caller_organization(request)
    return await (
        Organization.objects
        .select_related("region")
        .annotate(member_count=Count("members"))
        .aget(pk=organization.pk)
    )


def _apply_update_org(request, organization: Organization, data: OrganizationUpdateIn):
    """Sync transactional core of `update_organization`."""
    fields = data.dict(exclude_unset=True)
    fields = {name: ("" if value is None else value) for name, value in fields.items()}

    if "region_id" in fields:
        region_id = fields.pop("region_id")
        region = None
        if region_id != "":
            region = Region.objects.filter(public_id=region_id).first()
            if region is None:
                raise HttpError(400, "Unknown region")
        fields["region"] = region

    # Matches `_create_org_account` (api/views.py) refusing an empty name at
    # signup — the name drives public listings, so both paths that can set it
    # enforce the same floor.
    if "name" in fields and not fields["name"].strip():
        raise HttpError(400, "An organization name is required")

    # Signup strips name/address/tax_number before saving; this path didn't,
    # so a pasted value could carry leading/trailing whitespace forever.
    for stripped in ("name", "address", "tax_number"):
        if stripped in fields:
            fields[stripped] = fields[stripped].strip()

    # The only phone write in the codebase that skipped this. Organization.phone
    # is seeded from the admin's already-normalized number at signup, so leaving
    # this one unnormalized would fork the column's formatting on first edit. No
    # uniqueness check: unlike User.phone (a login identifier) an org's contact
    # number isn't one, and two orgs may legitimately share a switchboard.
    if "phone" in fields and fields["phone"] != "":
        fields["phone"] = normalize_phone(fields["phone"])

    if not fields:
        return organization

    before = snapshot(organization, ORG_AUDIT_FIELDS)
    for name, value in fields.items():
        setattr(organization, name, value)

    changes = diff(before, snapshot(organization, ORG_AUDIT_FIELDS))
    if not changes:
        return organization

    with transaction.atomic():
        organization.save(update_fields=list(fields) + ["updated_at"])
        ActivityLog.record(
            organization=organization,
            actor=request.auth,
            action=ActivityLog.Action.UPDATED,
            target=organization,
            changes=changes,
            context=request_context(request),
        )

    return organization


@org_router.patch("", response=OrganizationDetailOut)
async def update_organization(request, data: OrganizationUpdateIn):
    """
    Edit the organization profile. Requires `users.manage`.

    `is_verified` and `entity_type` cannot be reached from here at any value —
    they are not fields of `OrganizationUpdateIn`. Verification is a statement
    the platform makes about an organization, not one the organization makes
    about itself; `/org/verify-legal` (mocked ONEID, §0b) is where it moves.
    """
    organization = caller_organization(request)
    require_perm(request, OrgPermission.MANAGE_USERS)

    await sync_to_async(_apply_update_org)(request, organization, data)

    return await (
        Organization.objects
        .select_related("region")
        .annotate(member_count=Count("members"))
        .aget(pk=organization.pk)
    )
