"""
core/audit.py
Helpers for producing JSON-safe field diffs for ActivityLog.changes, and
for fingerprinting the request that caused them.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address


def to_jsonable(value):
    """Coerce ORM field values into something JSONField can store."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def snapshot(instance, fields: list[str]) -> dict:
    """Capture the current value of `fields` on a model instance."""
    return {name: to_jsonable(getattr(instance, name)) for name in fields}


def diff(before: dict, after: dict) -> dict:
    """
    Build {"field": {"from": old, "to": new}} for keys whose value changed.
    Keys absent from `after` are ignored — partial updates only diff what
    the caller actually touched.
    """
    return {
        key: {"from": before.get(key), "to": value}
        for key, value in after.items()
        if before.get(key) != value
    }


def request_context(request) -> dict:
    """Minimal request fingerprint stored alongside each audit row."""
    meta = getattr(request, "META", {}) or {}
    forwarded = meta.get("HTTP_X_FORWARDED_FOR", "")
    return {
        "ip": (forwarded.split(",")[0].strip() if forwarded else meta.get("REMOTE_ADDR")),
        "user_agent": meta.get("HTTP_USER_AGENT", "")[:255],
        "method": getattr(request, "method", ""),
        "path": getattr(request, "path", ""),
    }


def client_ip(request) -> str | None:
    """
    The caller's address, or None if it isn't one.

    `X-Forwarded-For` is client-controlled and `PhoneOtp.ip` is a Postgres
    `inet` column — an unvalidated header would turn a junk value into a 500
    rather than a rate-limit entry.

    Lives next to `request_context`, whose `ip` key it re-reads, rather than in
    the view module that first needed it: both `/auth/otp/request` and
    `/users/me/phone` rate-limit per address, and they sit on opposite sides of
    the api ↔ users import edge.
    """
    ip = (request_context(request) or {}).get("ip")
    try:
        validate_ipv46_address(ip)
    except (ValidationError, TypeError):
        return None
    return ip
