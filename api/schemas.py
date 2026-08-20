from uuid import UUID

from ninja import Schema

# All `id` fields carry the model's `public_id` UUID — integer PKs never
# leave the backend.


# ── Region ────────────────────────────────────────────────────────────────────

class RegionOut(Schema):
    id: UUID
    name: str
    code: str | None


# ── Organization ──────────────────────────────────────────────────────────────

class OrganizationOut(Schema):
    id: UUID
    name: str
    address: str
    region: RegionOut | None = None
    tax_number: str
    phone: str
    email: str
    # "individual" until ONEID verification promotes the org to a legal
    # entity. Sole traders never leave the default.
    entity_type: str = "individual"
    is_verified: bool = False


# ── User ──────────────────────────────────────────────────────────────────────

class UserOut(Schema):
    id: UUID
    username: str
    email: str
    phone: str | None = None
    full_name: str = ""
    telegram: str = ""
    first_name: str
    last_name: str
    # Effective permissions inside `organization` — an owner reads back the
    # full set. Re-read from `/members/me`; an admin can change these while
    # the token is still valid.
    is_owner: bool = False
    permissions: list[str] = []
    # True until the user replaces the one-time password they were issued.
    must_change_password: bool = False
    organization: OrganizationOut | None = None


# ── Auth in/out ───────────────────────────────────────────────────────────────

class LoginIn(Schema):
    # Accepts a phone, an email or a username. Kept as `username` because
    # that is the field the frontend already sends; phone + OTP is the front
    # door now, and this route is the secondary one (§0.4).
    username: str
    password: str


class RefreshIn(Schema):
    refresh_token: str


class LogoutIn(Schema):
    refresh_token: str


# ── Phone + OTP ───────────────────────────────────────────────────────────────

class OtpRequestIn(Schema):
    phone: str


class OtpRequestOut(Schema):
    """
    `retry_after` is how long until a resend is allowed, so the UI can run a
    countdown instead of guessing.

    `account_exists` tells the app whether to show the login or the
    create-organization screen. It does leak whether a number is registered;
    that is accepted deliberately for the UX and mitigated by the rate limit
    rather than by removing the field.
    """

    retry_after: int
    account_exists: bool
    # DEBUG only: there is no SMS gateway yet, so a developer would otherwise
    # have to read the code out of the server log. Always null in production.
    debug_code: str | None = None


class OtpThrottledOut(Schema):
    """429 body. `detail` keeps the project-wide error shape."""

    detail: str
    retry_after: int


class OtpVerifyIn(Schema):
    phone: str
    code: str


class OtpVerifyOut(Schema):
    """
    Two outcomes on one 200, told apart by `status`.

    `authenticated` — the number belongs to an account: tokens and user,
    exactly like a password login.
    `no_account` — nobody owns this number: a short-lived, single-use
    `signup_token` for `POST /auth/org/signup`. Verifying never creates an
    account by itself (§0.2).
    """

    status: str
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None
    user: UserOut | None = None
    signup_token: str | None = None


class OrgSignUpIn(Schema):
    """
    The only public account-creating payload. `signup_token` carries the
    phone — the client cannot name a different one.
    """

    signup_token: str
    full_name: str
    organization_name: str
    region_id: UUID | None = None
    entity_type: str | None = None


class PasswordChangeIn(Schema):
    current_password: str
    new_password: str


class TokenOut(Schema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until access token expiry


class AuthOut(TokenOut):
    user: UserOut
