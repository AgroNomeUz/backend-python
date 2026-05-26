from ninja import Schema
from pydantic import EmailStr


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


class UserOut(Schema):
    id: int
    username: str
    email: str
    first_name: str
    last_name: str


class TokenOut(Schema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until access token expiry


class AuthOut(TokenOut):
    user: UserOut
