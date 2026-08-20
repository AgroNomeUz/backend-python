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


def normalize_phone(value: str | None) -> str | None:
    """
    Strip a phone number down to the digits (and leading +) it is stored as.

    Users type numbers with spaces, dashes and brackets; the column is unique,
    so `+998 90 123-45-67` and `+998901234567` must not become two accounts.
    Returns None for anything empty, because the column is NULL-not-blank —
    see the 0007 migration for why.
    """
    if not value:
        return None
    cleaned = "".join(char for char in value if char.isdigit() or char == "+")
    return cleaned or None


def _free_username(base: str) -> str:
    """
    The first unused username starting from `base`.

    Nobody chooses a username any more — accounts are created from an email
    or a phone — but `username` is still the field Django authenticates
    against and must stay unique across the whole install, so collisions get
    a numeric suffix.
    """
    from .models import User

    base = base[:140] or "user"
    candidate = base
    suffix = 1
    while User.objects.filter(username=candidate).exists():
        suffix += 1
        candidate = f"{base[:140 - len(str(suffix)) - 1]}-{suffix}"
    return candidate


def username_for_email(email: str) -> str:
    """Derive a free username from an email's local part."""
    return _free_username(slugify(email.split("@")[0]))


def username_for_phone(phone: str) -> str:
    """
    Derive a free username from a phone number.

    An org admin who signs up with OTP never types an email or a password,
    so the number is all there is to name the account after. The leading `+`
    is dropped because `username` is validated against Django's
    `UnicodeUsernameValidator`, which doesn't allow it.
    """
    return _free_username(f"u{slugify(phone)}")
