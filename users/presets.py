"""
users/presets.py
Role presets: named starting points for a member's permission set.

**A preset is not a role.** The settled rule (§0.2) stands: authorization is
the permission codes stored on `User.permissions`, there is no `role` column,
and no check ever consults a preset. Applying a preset is just a client
sending its codes in `PATCH /members/{id}`; afterwards an admin may hand-tune
the set. The console labels a member with the preset whose codes match theirs
exactly, and "Custom" otherwise — derived on read, never stored, so a preset
and a member can't disagree.

Presets are built-in and code-defined. They are written against today's
coarse codes; when the per-service action registry lands
(`equipment:CreateAsset`, …) they are re-expressed in those actions here, and
nothing outside this module changes. Until then two of them overlap more than
their names suggest — `equipment.manage` covers both machines and listings,
so Sales can also edit the fleet. That is the granularity problem the
registry exists to fix, not something a preset can paper over.
"""

from dataclasses import dataclass

from .models import VIEW_PERMISSIONS, OrgPermission

OWNER_PRESET_ID = "owner"


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    description: str
    permissions: tuple[str, ...]


P = OrgPermission

# What every member could see before view permissions existed. The presets
# below, and the backfill migration, both start from it.
ALL_VIEW = VIEW_PERMISSIONS

PRESETS: tuple[Preset, ...] = (
    Preset(
        "org_admin",
        "Org Admin",
        "Runs the account: people, the fleet, offers and inquiries.",
        tuple(sorted(P.values)),
    ),
    Preset(
        "fleet_manager",
        "Fleet Manager",
        "Maintains machines and their listings.",
        tuple(sorted((P.MANAGE_EQUIPMENT.value, *ALL_VIEW))),
    ),
    Preset(
        "sales",
        "Sales",
        "Publishes offers and answers inquiries.",
        tuple(sorted((P.MANAGE_EQUIPMENT.value, P.MANAGE_INQUIRIES.value, *ALL_VIEW))),
    ),
    Preset(
        "viewer",
        "Viewer",
        "Sees everything, changes nothing. For accountants and observers.",
        tuple(sorted(ALL_VIEW)),
    ),
)

_BY_CODES = {frozenset(p.permissions): p for p in PRESETS}


def match_preset(user) -> str | None:
    """
    The id of the preset `user`'s permissions equal exactly.

    `owner` for the organization owner (who holds everything implicitly and
    can't be given a preset), None for a hand-tuned set.
    """
    if user.is_organization_owner:
        return OWNER_PRESET_ID
    preset = _BY_CODES.get(frozenset(user.permissions or []))
    return preset.id if preset else None
