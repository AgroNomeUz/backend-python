"""
users/schemas.py
Request/response contracts for organization member management.

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
