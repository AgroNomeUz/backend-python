"""
core/views.py
`GET /activity` — the read side of §0.3's audit trail.

The rows are written by every domain (`core/audit.py`), so the endpoint that
reads them belongs here rather than in `equipment/views.py`, where it began
when assets were the only thing being logged. `/assets/{id}/activity` and
`/members/{id}/activity` stay with their own routers and share
`org_activity()` from here.

**Any member of the organization may read the whole history**, and §8 left
that open ("readable by admins; members see their own unless we decide
otherwise"). Decided: the whole org, for three reasons.

  * §0.2 says reads are open to every member and a permission code only ever
    gates a write. It has exactly one exception, `/favorites`, and that one
    narrows by *ownership* rather than by a code. A second exception with no
    principle separating it from the first would leave the rule meaning
    nothing.
  * It discloses nothing new. Every field on the payload is already readable
    by that member through `/members`, `/listings` or `/inquiries`; `context`
    — the address and user agent — is the one thing that would be new, and it
    is deliberately not serialised (see `core/schemas.py`).
  * An audit trail only admins can see is the weaker safeguard. The person
    best placed to notice that their permissions changed, or that a listing
    they published was archived, is the person it happened to.

Filters that cannot match anything answer with an empty page rather than an
error — an actor id from another organization, a target that was never in
this org — because saying "no such object" would confirm one exists (§0.1).
A filter the *server* cannot make sense of is the opposite: an unknown
`action` or `target_type` is a client bug, and answering it with an empty
list would read as "nothing ever happened", which is the one wrong answer an
audit log must never give.
"""

from datetime import date, datetime, time, timedelta
from uuid import UUID

from asgiref.sync import sync_to_async
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from users.models import Organization
from users.permissions import caller_organization

from .models import ActivityLog
from .schemas import ActivityLogOut

activity_router = Router(tags=["Activity"])


def org_activity(organization: Organization):
    """
    One organization's history, newest first (the model's own ordering).

    `target` is prefetched, not lazily loaded: `ActivityLogOut` serialises the
    target's `public_id`, and these endpoints are async — a relation fetched
    at serialisation time would raise SynchronousOnlyOperation rather than
    merely cost a query. Prefetching a generic FK costs one query per distinct
    target type on the page, not one per row.
    """
    return (
        ActivityLog.objects.filter(organization=organization)
        .select_related("actor", "content_type")
        .prefetch_related("target")
    )


def _content_type_or_400(target_type: str) -> ContentType:
    """
    Resolve `?target_type=listing` to the content type it names.

    Matched on the model name alone, which is how the filter has always
    worked and what a client can reasonably be expected to send. Two apps
    could in principle register the same model name; nothing in this codebase
    does, and if it ever happens the caller gets a 400 asking them to qualify
    it rather than a silently arbitrary answer.
    """
    matches = list(ContentType.objects.filter(model=target_type.lower()))
    if not matches:
        raise HttpError(400, f"Unknown target_type '{target_type}'")
    if len(matches) > 1:
        labels = ", ".join(sorted(f"{ct.app_label}.{ct.model}" for ct in matches))
        raise HttpError(400, f"Ambiguous target_type '{target_type}' — try one of: {labels}")
    return matches[0]


def _target_pk(content_type: ContentType, target_id: UUID) -> int | None:
    """
    The internal pk behind a public UUID, or None if nothing has that id.

    Looked up across every organization on purpose. It reads like a hole and
    is the opposite of one: the history itself is org-scoped, so another
    organization's object resolves to a pk that matches no row here and the
    caller gets an empty page — the same answer an id that never existed
    gets. Scoping the lookup instead would mean telling the two apart.
    """
    model = content_type.model_class()
    if model is None or not hasattr(model, "public_id"):
        raise HttpError(
            400, f"'{content_type.model}' has no public ids to filter by"
        )
    return model.objects.filter(public_id=target_id).values_list("pk", flat=True).first()


def _day_starts_at(day: date) -> datetime:
    """
    Midnight on `day`, in the server's timezone.

    The bounds are built as datetimes rather than filtering on `created_at__date`
    so the range stays sargable against the `(organization, -created_at)` index
    the model already carries — this table is append-only and only grows.
    """
    return timezone.make_aware(datetime.combine(day, time.min))


def filtered_activity(
    organization: Organization,
    *,
    action: str | None = None,
    target_type: str | None = None,
    target_id: UUID | None = None,
    actor: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
):
    """
    Build the filtered queryset. **Sync**: resolving `target_type` and
    `target_id` hits the database, and the endpoint that calls this is async.

    Returns a lazy queryset — nothing here evaluates it, so pagination and
    serialisation still happen where ninja expects them to.
    """
    queryset = org_activity(organization)

    if action:
        if action not in ActivityLog.Action.values:
            allowed = ", ".join(ActivityLog.Action.values)
            raise HttpError(400, f"Unknown action '{action}' — expected one of: {allowed}")
        queryset = queryset.filter(action=action)

    if target_id is not None and not target_type:
        # A UUID alone cannot be resolved: `object_id` is an integer pk, and
        # which table to look it up in is exactly what `target_type` says.
        raise HttpError(400, "target_id needs target_type — say which kind of object it is")

    if target_type:
        content_type = _content_type_or_400(target_type)
        queryset = queryset.filter(content_type=content_type)
        if target_id is not None:
            pk = _target_pk(content_type, target_id)
            if pk is None:
                return queryset.none()
            queryset = queryset.filter(object_id=pk)

    if actor is not None:
        queryset = queryset.filter(actor__public_id=actor)

    if date_from and date_to and date_from > date_to:
        raise HttpError(400, "date_from is after date_to")
    if date_from:
        queryset = queryset.filter(created_at__gte=_day_starts_at(date_from))
    if date_to:
        # Inclusive: `date_to=2026-09-10` means "up to the end of the 10th",
        # which is what a person filling in a date range means by it.
        queryset = queryset.filter(created_at__lt=_day_starts_at(date_to + timedelta(days=1)))

    return queryset


@activity_router.get("", response=list[ActivityLogOut])
@paginate(LimitOffsetPagination)
async def list_activity(
    request,
    action: str | None = None,
    target_type: str | None = None,
    target_id: UUID | None = None,
    actor: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
):
    """
    The organization's change history, newest first.

    Five filters, and between them they answer the questions §8 was written
    for: *who deleted this?* (`target_type` + `target_id`), *what has this
    person been doing?* (`actor`), *what changed while I was away?*
    (`date_from` / `date_to`). They combine — every one of them narrows the
    same query.
    """
    return await sync_to_async(filtered_activity)(
        caller_organization(request),
        action=action,
        target_type=target_type,
        target_id=target_id,
        actor=actor,
        date_from=date_from,
        date_to=date_to,
    )
