"""
core/schemas.py
The read side of the audit trail (§8).

Here rather than in `equipment/schemas.py`, where it started, for the reason
§0.3 already moved `log_activity()` into `core/audit.py`: every domain writes
these rows and three routers now read them, so the one app that happened to
need it first should not own the shape.

**`context` is deliberately not on the wire.** `ActivityLog` stores the
caller's address and user agent, and that is worth keeping — but the question
§8 exists to answer is "who changed this?", which `actor` answers. An IP
address per row would turn a feature every member can read into a record of
where their colleagues were sitting, and nothing in the product asks for it.
It stays queryable in the admin, where the audience is different.
"""

from datetime import datetime
from uuid import UUID

from ninja import Field, Schema


class ActivityLogOut(Schema):
    id: UUID = Field(alias="public_id")
    action: str
    actor_id: UUID | None = None
    actor_username: str | None = None
    # The human label, because "who did what" is read by a person: a history
    # that says `u-4471` where it could say `Alisher Karimov` is a lookup
    # table the client has to build for itself. `actor_username` stays for the
    # callers already parsing it.
    actor_name: str | None = None
    target_type: str | None = None
    target_id: UUID | None = None
    target_repr: str
    changes: dict = {}
    created_at: datetime

    @staticmethod
    def resolve_actor_id(obj) -> UUID | None:
        return obj.actor.public_id if obj.actor_id else None

    @staticmethod
    def resolve_actor_username(obj) -> str | None:
        return obj.actor.username if obj.actor_id else None

    @staticmethod
    def resolve_actor_name(obj) -> str | None:
        if not obj.actor_id:
            return None
        return obj.actor.get_full_name() or obj.actor.username

    @staticmethod
    def resolve_target_type(obj) -> str | None:
        return obj.content_type.model if obj.content_type_id else None

    @staticmethod
    def resolve_target_id(obj) -> UUID | None:
        """
        The target's `public_id`, so a row can be fed straight back into
        `?target_type=…&target_id=…` — or into whatever endpoint owns that
        object — without a second lookup.

        `None` in three cases, all of them legitimate: the row has no target,
        the model has been deleted from the codebase since (`content_type` is
        SET_NULL), or the object itself is gone. The row survives either way;
        that is what `target_repr` is for.
        """
        if obj.content_type_id is None or obj.object_id is None:
            return None
        target = obj.target
        return getattr(target, "public_id", None) if target is not None else None
