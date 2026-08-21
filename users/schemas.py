"""
users/schemas.py
Request/response contracts for organization members, the caller's own
profile, and the organization profile.

All `id` fields carry `public_id` (UUID). Integer PKs never leave the backend.
"""

from datetime import datetime
from uuid import UUID

from ninja import Field, Schema
from pydantic import EmailStr

from .models import OrgPermission


class MemberOut(Schema):
    """A member of the caller's organization."""

    id: UUID = Field(alias="public_id")
    username: str
    email: str
    phone: str | None = None
    full_name: str
    telegram: str
    first_name: str
    last_name: str
    is_owner: bool = Field(alias="is_organization_owner")
    # Effective, not stored: an owner reads back the full permission set.
    permissions: list[str] = Field(alias="org_permissions")
    must_change_password: bool
    is_active: bool
    date_joined: datetime


class MemberCreateIn(Schema):
    """
    Add a staff account to the caller's organization.

    Only the email is required — the employee fills in the rest after their
    first login. No password field: the server issues a one-time one.

    `phone` is optional here and becomes the member's login identifier once
    OTP auth ships; until then it is contact detail only.
    """

    email: EmailStr
    phone: str | None = None
    full_name: str = ""
    telegram: str = ""
    first_name: str = ""
    last_name: str = ""
    permissions: list[OrgPermission] = []


class MemberCreateOut(Schema):
    """
    The created member plus their one-time password.

    `temporary_password` is returned exactly once, at creation, and is never
    readable again — only a fresh reset produces a new one.
    """

    member: MemberOut
    temporary_password: str


class MemberUpdateIn(Schema):
    """
    Partial update — only the keys present in the request body are applied.

    `permissions` replaces the whole set rather than merging, so a client can
    revoke by sending the codes that remain.
    """

    phone: str | None = None
    full_name: str | None = None
    telegram: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    permissions: list[OrgPermission] | None = None
    is_active: bool | None = None


class PasswordResetOut(Schema):
    """Result of an admin-issued password reset."""

    member: MemberOut
    temporary_password: str


# ── the caller's own profile ──────────────────────────────────────────────────

class SelfUpdateIn(Schema):
    """
    Partial self-edit — what a member may change about themselves without
    holding any permission code.

    Deliberately three fields. `permissions` is granted by an admin (§2b),
    `is_active` is an admin action, and `phone` is a credential: it is the
    OTP login identifier, so it moves only through `POST /users/me/phone`,
    which proves the caller owns the new number first.

    `email` carries no such proof today — there is no mail gateway in this
    project (§0b) — and is accepted unverified, because an email logs you in
    only *with* a password. That stops being true the moment a password reset
    by email ships; this field needs a verification step before it does.
    """

    full_name: str | None = None
    telegram: str | None = None
    email: EmailStr | None = None


class PhoneChangeIn(Schema):
    """The number the caller wants to move to. A code is sent to it, not to
    the number they hold now — the point is to prove they own the new one."""

    phone: str


class PhoneChangeOut(Schema):
    """Seconds until a resend is allowed, so the UI can run a countdown."""

    retry_after: int


class PhoneChangeVerifyIn(Schema):
    phone: str
    code: str


# ── the caller's organization ─────────────────────────────────────────────────

class OrgRegionOut(Schema):
    """
    The organization's region, read straight off the model.

    Not `api.schemas.RegionOut`: that one is only ever built from the hand-made
    dicts in the auth responses, so its `id` needs no alias. Serialising a
    `Region` instance without one would hand out the integer PK.
    """

    id: UUID = Field(alias="public_id")
    name: str
    code: str


class OrganizationDetailOut(Schema):
    """
    The caller's own organization — `api.schemas.OrganizationOut` plus the two
    fields only a member ever sees: how many people are in it, and when it
    was created.
    """

    id: UUID = Field(alias="public_id")
    name: str
    address: str
    region: OrgRegionOut | None = None
    tax_number: str
    phone: str
    email: str
    entity_type: str
    # Read-only here by construction: it drives the verified badge on public
    # listings and is set by staff, never by an endpoint (§2).
    is_verified: bool
    member_count: int
    created_at: datetime


class OrganizationUpdateIn(Schema):
    """
    Partial update of the organization profile. Requires `users.manage`.

    `is_verified` and `entity_type` are **absent rather than filtered**: the
    first drives the public badge and the second is what ONEID verification
    promotes (§2b, `/org/verify-legal`). A field that isn't in the schema
    can't be forgotten in a guard.
    """

    name: str | None = None
    address: str | None = None
    region_id: UUID | None = None
    tax_number: str | None = None
    phone: str | None = None
    email: str | None = None
