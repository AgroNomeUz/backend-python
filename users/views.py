"""
users/views.py
Organization member (staff) management.

An organization starts as a single owner account created at signup. Everyone
else is added here: an admin submits an email, the server creates the account
with a one-time password, and the employee signs in with that email and
changes the password (`POST /auth/password/change`).

Authorization, in one place:

  * reading the roster — any member of the organization
  * every write        — `OrgPermission.MANAGE_USERS`
  * the owner          — implicitly holds every permission, and is protected
    from being demoted, deactivated or password-reset by their own staff

Members are addressed by `public_id` (UUID) and always scoped to the caller's
organization, so an id from another org returns 404 rather than 403 — ids
can't be probed. Every write is mirrored into `core.ActivityLog`.
"""

from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from api.models import RefreshToken
from core.audit import diff, request_context, snapshot
from core.models import ActivityLog

from .models import OrgPermission, Organization, User
from .permissions import caller_organization, normalize_permissions, require_perm
from .schemas import (
    MemberCreateIn,
    MemberCreateOut,
    MemberOut,
    MemberUpdateIn,
    PasswordResetOut,
)
from .services import generate_temporary_password, username_for_email

members_router = Router(tags=["Members"])


# ── helpers ───────────────────────────────────────────────────────────────────

def org_members(organization: Organization):
    """
    The organization's roster.

    `owned_organization` is selected because `MemberOut` reads
    `is_organization_owner` and `org_permissions` off every row — without it
    the list view fires one extra query per member.
    """
    return (
        User.objects
        .filter(organization=organization)
        .select_related("owned_organization")
        .order_by("date_joined")
    )


def member_or_404(organization: Organization, member_id: UUID) -> User:
    return get_object_or_404(org_members(organization), public_id=member_id)


MEMBER_AUDIT_FIELDS = ["username", "email", "first_name", "last_name", "is_active"]


def member_snapshot(member: User) -> dict:
    """Audit-friendly view of a member. Permissions are the stored set."""
    values = snapshot(member, MEMBER_AUDIT_FIELDS)
    values["permissions"] = sorted(member.permissions or [])
    return values


def log_member_activity(request, organization, action, member, changes=None) -> None:
    ActivityLog.record(
        organization=organization,
        actor=request.auth,
        action=action,
        target=member,
        changes=changes,
        context=request_context(request),
    )


def revoke_sessions(member: User) -> None:
    """
    Kill every outstanding refresh token for a member.

    Access tokens are stateless and live for 30 minutes, so this bounds — but
    does not instantly end — a session. Called whenever the account's standing
    changes underneath it: deactivation, or an admin password reset.
    """
    RefreshToken.objects.filter(user=member, is_revoked=False).update(is_revoked=True)


def protect_owner(member: User, reason: str) -> None:
    """
    The owner is not administrable by their own staff.

    Without this, anyone holding `users.manage` could lock the owner out of
    their own organization or take it over via a password reset.
    """
    if member.is_organization_owner:
        raise HttpError(403, reason)


def protect_self(request, member: User, action: str) -> None:
    """Guard against an admin editing their own standing."""
    if member.pk == request.auth.pk:
        raise HttpError(400, f"You cannot {action} your own account")


# ── roster ────────────────────────────────────────────────────────────────────

@members_router.get("/me", response=MemberOut)
def get_current_member(request):
    """
    The caller's own record.

    Permissions can be changed mid-session by an admin, so the frontend
    re-reads this instead of trusting what the login response said.
    """
    return member_or_404(caller_organization(request), request.auth.public_id)


@members_router.get("", response=list[MemberOut])
@paginate(LimitOffsetPagination)
def list_members(
    request,
    search: str | None = None,
    is_active: bool | None = None,
    permission: str | None = None,
):
    """Everyone in the caller's organization. Readable by any member."""
    qs = org_members(caller_organization(request))

    if search:
        qs = qs.filter(
            Q(email__icontains=search)
            | Q(username__icontains=search)
            | Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
        )
    if is_active is not None:
        qs = qs.filter(is_active=is_active)
    if permission:
        # Matches the stored set only — the owner holds every permission
        # implicitly and is deliberately not surfaced by this filter.
        qs = qs.filter(permissions__contains=[permission])

    return qs


@members_router.post("", response={201: MemberCreateOut})
def create_member(request, data: MemberCreateIn):
    """
    Add an employee to the caller's organization.

    The employee signs in with this email and the returned one-time password,
    which is shown **once** — reset it if it is lost. They are flagged
    `must_change_password` until they choose their own.
    """
    organization = caller_organization(request)
    require_perm(request, OrgPermission.MANAGE_USERS)

    email = data.email.strip().lower()
    if User.objects.filter(email__iexact=email).exists():
        raise HttpError(400, "Email already registered")

    password = generate_temporary_password()

    with transaction.atomic():
        member = User.objects.create_user(
            username=username_for_email(email),
            email=email,
            password=password,
            first_name=data.first_name,
            last_name=data.last_name,
            organization=organization,
            permissions=normalize_permissions(data.permissions),
            must_change_password=True,
        )
        log_member_activity(
            request,
            organization,
            ActivityLog.Action.CREATED,
            member,
            changes=diff({}, member_snapshot(member)),
        )

    return 201, {"member": member, "temporary_password": password}


@members_router.get("/{member_id}", response=MemberOut)
def get_member(request, member_id: UUID):
    return member_or_404(caller_organization(request), member_id)


@members_router.patch("/{member_id}", response=MemberOut)
def update_member(request, member_id: UUID, data: MemberUpdateIn):
    """
    Partial update — only the keys present in the request body are applied.

    `permissions` replaces the member's whole set. Changes take effect on the
    member's next request: permissions are read from the database on every
    call, never from the token.
    """
    organization = caller_organization(request)
    require_perm(request, OrgPermission.MANAGE_USERS)
    member = member_or_404(organization, member_id)

    fields = data.dict(exclude_unset=True)
    if not fields:
        return member

    if "permissions" in fields:
        protect_owner(member, "The organization owner's permissions cannot be changed")
        # Self-editing here is how an admin accidentally revokes their own
        # access; make them do it from another admin account.
        protect_self(request, member, "change the permissions of")
        fields["permissions"] = normalize_permissions(fields["permissions"])
    if "is_active" in fields:
        protect_owner(member, "The organization owner cannot be deactivated")
        protect_self(request, member, "deactivate")

    before = member_snapshot(member)
    for name, value in fields.items():
        setattr(member, name, value)

    changes = diff(before, member_snapshot(member))
    if not changes:
        return member

    action = (
        ActivityLog.Action.STATUS_CHANGED
        if set(changes) == {"is_active"}
        else ActivityLog.Action.UPDATED
    )

    with transaction.atomic():
        member.save(update_fields=list(fields))
        if changes.get("is_active", {}).get("to") is False:
            revoke_sessions(member)
        log_member_activity(request, organization, action, member, changes=changes)

    return member


@members_router.delete("/{member_id}", response={204: None})
def deactivate_member(request, member_id: UUID):
    """
    Remove an employee's access.

    Deactivates rather than deletes: the account is referenced by the
    organization's activity log, and history that loses its actor stops being
    an audit trail. Outstanding refresh tokens are revoked. Reversible with
    `PATCH {"is_active": true}`.
    """
    organization = caller_organization(request)
    require_perm(request, OrgPermission.MANAGE_USERS)
    member = member_or_404(organization, member_id)

    protect_owner(member, "The organization owner cannot be deactivated")
    protect_self(request, member, "deactivate")

    if not member.is_active:
        return 204, None

    before = member_snapshot(member)
    member.is_active = False

    with transaction.atomic():
        member.save(update_fields=["is_active"])
        revoke_sessions(member)
        log_member_activity(
            request,
            organization,
            ActivityLog.Action.STATUS_CHANGED,
            member,
            changes=diff(before, member_snapshot(member)),
        )

    return 204, None


@members_router.post("/{member_id}/reset-password", response=PasswordResetOut)
def reset_member_password(request, member_id: UUID):
    """
    Issue a fresh one-time password — for the "they lost it" case.

    Revokes the member's existing sessions and re-flags them
    `must_change_password`. The new password is shown once.
    """
    organization = caller_organization(request)
    require_perm(request, OrgPermission.MANAGE_USERS)
    member = member_or_404(organization, member_id)

    protect_owner(
        member, "The organization owner's password cannot be reset by staff"
    )
    protect_self(request, member, "reset the password of")

    password = generate_temporary_password()
    member.set_password(password)
    member.must_change_password = True

    with transaction.atomic():
        member.save(update_fields=["password", "must_change_password"])
        revoke_sessions(member)
        log_member_activity(
            request,
            organization,
            ActivityLog.Action.UPDATED,
            member,
            # Never record the password itself, only that it was replaced.
            changes={"password": {"from": None, "to": "reset"}},
        )

    return {"member": member, "temporary_password": password}
