from ninja import Schema
from pydantic import EmailStr


# ── Region ────────────────────────────────────────────────────────────────────

class RegionOut(Schema):
    id: int
    name: str
    code: str | None


# ── Organization ──────────────────────────────────────────────────────────────

class OrganizationOut(Schema):
    id: int
    name: str
    address: str
    region: RegionOut
    tax_number: str
    phone: str
    email: str


# ── User ──────────────────────────────────────────────────────────────────────

class UserOut(Schema):
    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    organization: OrganizationOut | None = None


# ── Auth in/out ───────────────────────────────────────────────────────────────

class SignUpIn(Schema):
    username: str
    email: EmailStr
    password: str
    first_name: str = ""
    last_name: str = ""


class LoginIn(Schema):
    # Accepts username or email
    username: str
    password: str


class RefreshIn(Schema):
    refresh_token: str


class TokenOut(Schema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until access token expiry


class AuthOut(TokenOut):
    user: UserOut
