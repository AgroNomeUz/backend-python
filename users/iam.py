"""
users/iam.py
`/iam` — the read side of the console's IAM service: which permissions exist
and which role presets bundle them.

Both are readable by any member (§0.2: reads are open). Nothing here writes;
applying a preset is `PATCH /members/{id}` with the preset's codes.

The permission catalogue carries a `service` so the console can group codes
under Equipment / IAM / Inquiries without hard-coding the mapping. When the
per-service action registry ships, this endpoint is where its entries
surface, in the same shape.
"""

from django.db.models import Count, Q
from ninja import Field, Router, Schema

from .models import OrgPermission, User
from .permissions import caller_organization
from .presets import PRESETS

iam_router = Router(tags=["IAM"])

# code → (service id, service label, one-line explanation)
_PERMISSION_INFO = {
    OrgPermission.MANAGE_EQUIPMENT: (
        "equipment", "Equipment",
        "Add, edit and retire machines; create, publish and pause their listings.",
    ),
    OrgPermission.MANAGE_USERS: (
        "iam", "IAM",
        "Add and deactivate users, change permissions, reset passwords, edit the organization.",
    ),
    OrgPermission.MANAGE_INQUIRIES: (
        "inquiries", "Inquiries",
        "Send inquiries, reply in threads and mark them read.",
    ),
    OrgPermission.MANAGE_FARMS: (
        "farms", "Farms",
        "Create and edit farms, fields, boundaries, seasons and crop zones.",
    ),
}


class PermissionOut(Schema):
    code: str
    label: str
    service: str
    service_label: str
    description: str


class PresetOut(Schema):
    id: str
    name: str
    description: str
    permissions: list[str]
    # Active staff whose permissions equal this preset exactly. The owner is
    # never counted: they hold everything implicitly and have no preset.
    member_count: int = Field(0)
    is_builtin: bool = True


@iam_router.get("/permissions", response=list[PermissionOut])
async def list_permissions(request):
    """Every permission code a member can be granted, grouped by service."""
    caller_organization(request)
    rows = []
    for code in OrgPermission:
        service, service_label, description = _PERMISSION_INFO.get(
            code, ("other", "Other", "")
        )
        rows.append({
            "code": code.value,
            "label": code.label,
            "service": service,
            "service_label": service_label,
            "description": description,
        })
    return rows


@iam_router.get("/presets", response=list[PresetOut])
async def list_presets(request):
    """
    The built-in role presets, each with how many members match it.

    One query: the counts are conditional aggregates over the caller's active,
    non-owner staff, compared by exact (sorted) array equality.
    """
    organization = caller_organization(request)
    staff = User.objects.filter(
        organization=organization,
        is_active=True,
        owned_organization__isnull=True,
    )
    counts = await staff.aaggregate(**{
        preset.id: Count("pk", filter=Q(permissions=list(preset.permissions)))
        for preset in PRESETS
    })
    return [
        {
            "id": preset.id,
            "name": preset.name,
            "description": preset.description,
            "permissions": list(preset.permissions),
            "member_count": counts[preset.id] or 0,
        }
        for preset in PRESETS
    ]
