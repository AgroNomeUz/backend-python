"""
inquiries/views.py
The two ends of one conversation: an organization asking, and the one asked.

Three rules shape every endpoint here:

  * An inquiry belongs to **two** organizations (§0.1), so every query is
    anchored to one side of it — `provider_organization` for the inbox,
    `customer_organization` for the sent folder — and there is no endpoint
    that can see both sides of someone else's conversation. An id from the
    wrong side returns **404, not 403**.
  * **Reads are open to any member** of the organization, writes need
    `inquiries.manage` (§0.2). An inbox nobody but the owner can open is not
    an inbox.
  * Every write lands in `ActivityLog` **against the actor's own
    organization** (§0.3) — sending is logged for the sender, marking read for
    the provider. `ActivityLog` is org-scoped, and each org's history should
    answer "who here did this?", not "what did the other side do?".

Async endpoints with the transactional body in a sync `_apply_*` helper, for
the reason equipment/views.py sets out: Django has no async
`transaction.atomic()`, and a write plus its audit row have to commit
together.
"""

from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import IntegrityError, transaction
from django.shortcuts import aget_object_or_404, get_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from core.audit import diff, log_activity, snapshot
from core.models import ActivityLog
from listings.views import published_listings
from users.models import OrgPermission, Organization
from users.permissions import caller_organization, require_perm

from .models import Inquiry
from .schemas import InquiryCreateIn, InquiryInboxOut, InquiryOut

inquiries_router = Router(tags=["Inquiries"])


# ── helpers ───────────────────────────────────────────────────────────────────

def writable_organization(request) -> Organization:
    """
    The caller's organization, for an endpoint about to write an inquiry.

    Sending one, and taking one on, both need `inquiries.manage`; reading the
    inbox needs nothing beyond membership. The owner holds the code
    implicitly, so a newly created organization can contact people before
    anyone has configured anything.
    """
    organization = caller_organization(request)
    require_perm(request, OrgPermission.MANAGE_INQUIRIES)
    return organization


# What an organization's history should show about an inquiry. The message
# body is **not** here on purpose: it is already on the row, it never changes,
# and an audit trail is a record of who acted, not a second copy of the text.
INQUIRY_AUDIT_FIELDS = ["start_date", "end_date", "read_at"]


def inquiry_snapshot(inquiry: Inquiry) -> dict:
    """Audit-friendly view of an inquiry, with both relations as labels."""
    values = snapshot(inquiry, INQUIRY_AUDIT_FIELDS)
    values["listing"] = str(inquiry.listing)
    values["provider"] = str(inquiry.provider_organization)
    return values


def _integrity_error(exc: IntegrityError) -> HttpError:
    """
    Turn a constraint violation into the status it deserves.

    The view checks each of these first; this is the backstop for the race the
    checks cannot cover, where only the database can arbitrate.
    """
    message = str(exc)
    if "inquiry_not_to_own_org" in message:
        return HttpError(400, "You cannot send an inquiry to your own organization.")
    if "inquiry_dates_ordered" in message:
        return HttpError(400, "The end date cannot precede the start date.")
    if "inquiry_listing_same_org_fk" in message:
        return HttpError(400, "That listing belongs to a different organization.")
    raise exc


# ── querysets ─────────────────────────────────────────────────────────────────

def _inquiry_relations(queryset):
    """
    Everything the schemas serialise, joined up front.

    An async view cannot lazily load a relation — it raises
    SynchronousOnlyOperation at serialisation time — so this is correctness,
    not just an N+1 guard.
    """
    return queryset.select_related(
        "listing",
        "listing__organization",
        "listing__organization__region",
        "customer_organization",
        "customer_organization__region",
        "created_by",
        "read_by",
    )


def received_inquiries(organization: Organization):
    """The organization's inbox — everything asked *of* it."""
    return _inquiry_relations(
        Inquiry.objects.filter(provider_organization=organization)
    )


def sent_inquiries(organization: Organization):
    """Everything the organization has asked of someone else."""
    return _inquiry_relations(
        Inquiry.objects.filter(customer_organization=organization)
    )


# ── sending ───────────────────────────────────────────────────────────────────

def _apply_create_inquiry(request, organization, data: InquiryCreateIn) -> Inquiry:
    """Sync transactional core of `create_inquiry`."""
    if data.start_date and data.end_date and data.end_date < data.start_date:
        raise HttpError(400, "The end date cannot precede the start date.")

    # Resolved through the public feed, so you may only ask about something
    # actually on the market: a draft, a paused offer or a machine in for
    # repair is not visible here and its id simply 404s — the same answer a
    # stranger's id gets, which is the point (§0.1).
    listing = get_object_or_404(published_listings(), public_id=data.listing_id)

    # A 400 rather than a 404: the listing is yours, so nothing is being
    # revealed, and a "not found" would be a lie the seller can disprove by
    # looking at their own catalogue.
    if listing.organization_id == organization.pk:
        raise HttpError(400, "You cannot send an inquiry to your own organization.")

    with transaction.atomic():
        try:
            inquiry = Inquiry.objects.create(
                listing=listing,
                # From the listing and the token, never from the body — a
                # client-supplied organization id is exactly what §0.1 rules
                # out.
                provider_organization=listing.organization,
                customer_organization=organization,
                created_by=request.auth,
                message=data.message,
                start_date=data.start_date,
                end_date=data.end_date,
            )
        except IntegrityError as exc:
            raise _integrity_error(exc)

        # Logged against the sender: this is a thing *this* organization did.
        log_activity(
            request,
            organization,
            ActivityLog.Action.CREATED,
            inquiry,
            changes=diff({}, inquiry_snapshot(inquiry)),
        )

    # Re-read through the joined queryset: the response serialises the
    # listing, its owner and that owner's region, none of which an async
    # response can fetch lazily.
    return sent_inquiries(organization).get(pk=inquiry.pk)


@inquiries_router.post("", response={201: InquiryOut})
async def create_inquiry(request, data: InquiryCreateIn):
    """
    Ask the organization behind a listing about it.

    Organization → organization, sent by a named member (§0.1): the response
    carries both, because the provider needs to know who they would be dealing
    with *and* who to call.
    """
    organization = writable_organization(request)
    inquiry = await sync_to_async(_apply_create_inquiry)(request, organization, data)
    return 201, inquiry


# ── the two folders ───────────────────────────────────────────────────────────

@inquiries_router.get("/received", response=list[InquiryInboxOut])
@paginate(LimitOffsetPagination)
async def list_received_inquiries(
    request,
    read: bool | None = None,
    handled_by: UUID | None = None,
    listing_id: UUID | None = None,
):
    """
    The organization's inbox, newest first.

    Readable by any member — reads are never permission-gated (§0.2), and an
    inbox only one person can open stops being a team's inbox. `read=false` is
    the unanswered queue; `handled_by` answers "what has this member picked
    up?" (§0.3).
    """
    qs = received_inquiries(caller_organization(request))
    if read is not None:
        qs = qs.filter(read_at__isnull=not read)
    if handled_by:
        qs = qs.filter(read_by__public_id=handled_by)
    if listing_id:
        qs = qs.filter(listing__public_id=listing_id)
    return qs


@inquiries_router.get("/sent", response=list[InquiryOut])
@paginate(LimitOffsetPagination)
async def list_sent_inquiries(
    request,
    created_by: UUID | None = None,
    listing_id: UUID | None = None,
):
    """
    Everything the organization has asked, newest first.

    `InquiryOut` rather than `InquiryInboxOut`: `handled_by` names a member of
    the *other* organization, and who over there opened the message is not a
    read receipt they owe us.
    """
    qs = sent_inquiries(caller_organization(request))
    if created_by:
        qs = qs.filter(created_by__public_id=created_by)
    if listing_id:
        qs = qs.filter(listing__public_id=listing_id)
    return qs


# ── taking one on ─────────────────────────────────────────────────────────────

def _apply_mark_read(request, organization, inquiry: Inquiry) -> Inquiry:
    """Sync transactional core of `mark_inquiry_read`."""
    if inquiry.read_at is not None:
        # Idempotent, and the first reader keeps their name on it: "who picked
        # this up" is the fact worth having, and it would be worthless if the
        # next person to open the inbox overwrote it.
        return inquiry

    before = inquiry_snapshot(inquiry)
    read_at = timezone.now()

    with transaction.atomic():
        # A conditional UPDATE rather than a save, so two members opening the
        # inbox at the same moment cannot both claim it — the loser's update
        # matches no rows and it falls through to the branch below.
        # `updated_at` is set by hand because `.update()` does not run
        # `auto_now`.
        claimed = Inquiry.objects.filter(pk=inquiry.pk, read_at__isnull=True).update(
            read_at=read_at, read_by=request.auth, updated_at=read_at
        )
        if not claimed:
            return received_inquiries(organization).get(pk=inquiry.pk)

        inquiry.read_at = read_at
        inquiry.read_by = request.auth
        inquiry.updated_at = read_at

        # A read is a change of state, not an edit of the message — the same
        # distinction `update_listing` draws for a status-only patch.
        log_activity(
            request,
            organization,
            ActivityLog.Action.STATUS_CHANGED,
            inquiry,
            changes=diff(before, inquiry_snapshot(inquiry)),
        )

    return inquiry


@inquiries_router.post("/{inquiry_id}/read", response=InquiryInboxOut)
async def mark_inquiry_read(request, inquiry_id: UUID):
    """
    Mark an inquiry read, and record who read it.

    In a shared inbox the flag alone means nothing: `handled_by` is what turns
    "someone saw this" into "Alisher has it". Scoped to the caller's inbox, so
    another organization's id is a 404 (§0.1).
    """
    organization = writable_organization(request)
    inquiry = await aget_object_or_404(
        received_inquiries(organization), public_id=inquiry_id
    )
    return await sync_to_async(_apply_mark_read)(request, organization, inquiry)
