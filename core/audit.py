"""
core/audit.py
Helpers for writing ActivityLog rows: JSON-safe field diffs, the request
fingerprint stored beside them, and the one-line recorder every write endpoint
calls (§0.3 — a write endpoint without an audit row is incomplete).
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


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


def log_activity(request, organization, action, target, changes=None) -> None:
    """
    Record one write against the organization's history.

    Lives here rather than in a single app's views because every org-owned
    domain has to call it — assets, listings, images, and inquiries and
    bookings when they land.
    """
    from core.models import ActivityLog

    ActivityLog.record(
        organization=organization,
        actor=request.auth,
        action=action,
        target=target,
        changes=changes,
        context=request_context(request),
    )
