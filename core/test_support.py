"""Shared by every app's tests."""

from api.auth import create_access_token


def auth(user) -> dict:
    """Request kwargs carrying a bearer token for `user`."""
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}
