"""
users/permissions.py
Tenancy and authorization guards shared by every org-scoped endpoint.

Two questions every write endpoint has to answer, in this order:

  1. *Which* organization is the caller acting for? → `caller_organization`
  2. Are they allowed to do this in it?            → `require_perm`

Reads are open to any member of the organization; permissions only gate
writes. Ownership is not a permission code — the owner passes every check
(`User.has_org_perm`), so an organization can never lock itself out.
"""

from ninja.errors import HttpError

from .models import OrgPermission, Organization


def caller_organization(request) -> Organization:
    """
    The organization the authenticated user acts on behalf of.

    Signup always creates an org, so a user without one is a data anomaly
    (or an admin account created via `createsuperuser`) — not something an
    org-scoped endpoint can serve.
    """
    user = request.auth
    if user.organization_id is None:
        raise HttpError(403, "Your account does not belong to an organization")
    return user.organization


def require_perm(request, code: str) -> None:
    """Raise 403 unless the caller holds `code` in their own organization."""
    if not request.auth.has_org_perm(code):
        label = OrgPermission(code).label
        raise HttpError(403, f"You need the '{label}' permission to do this")


def normalize_permissions(values) -> list[str]:
    """
    Coerce incoming permission codes to a sorted, de-duplicated list of str.

    Accepts either `OrgPermission` members (what django-ninja parses request
    bodies into) or bare strings, so callers don't have to care which.
    """
    return sorted({getattr(value, "value", value) for value in values or []})
