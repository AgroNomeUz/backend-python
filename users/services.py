"""
users/services.py
Account-creation mechanics kept out of the view layer.

Two things here are easy to get subtly wrong and are therefore written once:
deriving a unique `username` for an account nobody chose a username for, and
generating a one-time password that is safe to read aloud over the phone.
"""

from django.utils.crypto import get_random_string
from django.utils.text import slugify

# No 0/O/1/l/I — a one-time password gets dictated over the phone or copied
# out of a chat message, and those characters are where that goes wrong.
_PASSWORD_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz"


def generate_temporary_password(groups: int = 3, size: int = 4) -> str:
    """A readable one-time password, e.g. `7Kq2-mXve-91Ld`."""
    return "-".join(
        get_random_string(size, _PASSWORD_ALPHABET) for _ in range(groups)
    )


def username_for_email(email: str) -> str:
    """
    Derive a free username from an email's local part.

    Staff accounts are created from an email alone, but `username` is still
    the field Django authenticates against and it must stay unique across the
    whole install — so collisions get a numeric suffix.
    """
    from .models import User

    base = slugify(email.split("@")[0])[:140] or "user"
    candidate = base
    suffix = 1
    while User.objects.filter(username=candidate).exists():
        suffix += 1
        candidate = f"{base[:140 - len(str(suffix)) - 1]}-{suffix}"
    return candidate
