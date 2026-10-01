"""
deals/views.py
Turning an inquiry into a deal, confirming or declining it, and reviewing a
completed one (seller-profile-proposal.md §4, decided in §8).

Two routers, because the four endpoints live at two different prefixes:
`deal_creation_router` mounts at the existing `/inquiries` prefix alongside
`inquiries.views.inquiries_router` — django-ninja only rejects mounting the
*same* `Router` object at a prefix twice without `url_name_prefix`; two
distinct `Router` instances sharing a prefix is fine — and `deals_router`
mounts at `/deals`.

One permission code gates all four writes, `deals.manage` (§8.5): turning an
inquiry into a deal is the provider's write, confirming/declining/reviewing
are the customer's. Requiring the *other* side to confirm — never the
provider who proposed it — is the one thing stopping an owner from farming
fake completed-deal history for themselves (§4).

Async endpoints with the transactional body in a sync `_apply_*` helper, for
the reason equipment/views.py sets out: Django has no async
`transaction.atomic()`, and a write plus its audit row have to commit
together.
"""

from datetime import timedelta
from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import IntegrityError, transaction
from django.shortcuts import aget_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from core.audit import log_activity
from core.models import ActivityLog
from inquiries.views import received_inquiries
from listings.models import Listing
from users.models import OrgPermission
from users.permissions import caller_organization, require_perm

from .models import Deal, Review
from .schemas import DealCreateIn, DealOut, ReviewCreateIn, ReviewOut

deal_creation_router = Router(tags=["Deals"])
deals_router = Router(tags=["Deals"])

# §4 suggests 30 days; nothing else in the proposal names a different figure,
# so this is the one place it needs to live.
REVIEW_WINDOW = timedelta(days=30)


def writable_organization(request):
    """The caller's organization, for an endpoint about to write a deal."""
    organization = caller_organization(request)
    require_perm(request, OrgPermission.MANAGE_DEALS)
    return organization


def _deal_relations(queryset):
    """
    Everything `DealOut` serialises, joined up front.

    An async view cannot lazily load a relation — it raises
    SynchronousOnlyOperation at serialisation time — so this is correctness,
    not just an N+1 guard (the same reason inquiries/views.py's
    `_inquiry_relations` exists).
    """
    return queryset.select_related(
        "listing", "provider_organization", "customer_organization"
    )


def customer_deals(organization):
    """Deals where this organization is the customer — confirm/decline/review."""
    return _deal_relations(Deal.objects.filter(customer_organization=organization))


def provider_deals(organization):
    """Deals where this organization is the provider — created here."""
    return _deal_relations(Deal.objects.filter(provider_organization=organization))


def _integrity_error(exc: IntegrityError) -> HttpError:
    """
    Turn a constraint violation into the status it deserves.

    The view checks each of these first; this is the backstop for the race
    the checks cannot cover, where only the database can arbitrate.
    """
    message = str(exc)
    if "deal_one_per_inquiry" in message:
        return HttpError(409, "This inquiry already has a deal.")
    if "deal_not_own_org" in message:
        return HttpError(400, "An organization cannot deal with itself.")
    if "review_rating_range" in message:
        return HttpError(400, "Rating must be between 1 and 5.")
    raise exc


# ── turning an inquiry into a deal ───────────────────────────────────────────

def _apply_create_deal(request, organization, inquiry, data: DealCreateIn) -> Deal:
    """Sync transactional core of `create_deal`."""
    # The deal has to be the same kind of transaction the listing actually
    # offers — `Listing.ListingType` and `Deal.DealType` share their values
    # ("rent"/"sale") for exactly this comparison. Without this, a `sale`
    # deal_type on an inquiry about a `rent` listing would, on confirmation,
    # mark a rental listing `sold` (§8.4) and record a sale that was never
    # actually offered.
    if data.deal_type != inquiry.listing.listing_type:
        raise HttpError(
            400,
            f"This inquiry is about a '{inquiry.listing.listing_type}' listing; "
            "deal_type must match.",
        )

    with transaction.atomic():
        if data.deal_type == Deal.DealType.SALE and inquiry.listing_id:
            # A machine can only be sold once. `select_for_update` locks the
            # listing row for the rest of this transaction, so two sale
            # deals proposed on the same listing at the same moment (from
            # two different inquiries — `deal_one_per_inquiry` only stops a
            # *second* deal on the *same* inquiry) serialize here rather than
            # both passing this check and racing each other to confirmation.
            Listing.objects.select_for_update().get(pk=inquiry.listing_id)
            conflicting = Deal.objects.filter(
                listing_id=inquiry.listing_id,
                deal_type=Deal.DealType.SALE,
                status__in=[Deal.Status.PENDING_CONFIRMATION, Deal.Status.COMPLETED],
            ).exists()
            if conflicting:
                raise HttpError(
                    409, "This listing already has a pending or completed sale."
                )
        try:
            deal = Deal.objects.create(
                listing=inquiry.listing,
                inquiry=inquiry,
                # Copied from the inquiry, never from the request body —
                # the same rule `_apply_create_inquiry` follows for
                # `provider_organization` off the listing (§0.1).
                provider_organization=inquiry.provider_organization,
                customer_organization=inquiry.customer_organization,
                deal_type=data.deal_type,
                created_by=request.auth,
            )
        except IntegrityError as exc:
            raise _integrity_error(exc)

        # Logged against the provider: turning an inquiry into a deal is
        # that organization's act (§0.3's asymmetry, same as
        # `_apply_create_inquiry` logs against the sender).
        log_activity(
            request,
            organization,
            ActivityLog.Action.CREATED,
            deal,
            changes={"deal_type": {"from": None, "to": deal.deal_type}},
        )

    # Re-read through the joined queryset: DealOut resolves the listing and
    # both organizations, none of which an async response can fetch lazily.
    return provider_deals(organization).get(pk=deal.pk)


@deal_creation_router.post("/{inquiry_id}/deal", response={201: DealOut})
async def create_deal(request, inquiry_id: UUID, data: DealCreateIn):
    """
    Turn an inquiry into a deal — the provider's write.

    Resolved through the caller's own inbox (`received_inquiries`), so
    another organization's inquiry id is a 404, never a 403 (§0.1).
    """
    organization = writable_organization(request)
    inquiry = await aget_object_or_404(
        received_inquiries(organization), public_id=inquiry_id
    )
    deal = await sync_to_async(_apply_create_deal)(request, organization, inquiry, data)
    return 201, deal


# ── confirming or declining ──────────────────────────────────────────────────

def _require_pending(deal: Deal) -> None:
    if deal.status != Deal.Status.PENDING_CONFIRMATION:
        raise HttpError(
            409, f"This deal is already {deal.get_status_display().lower()}."
        )


def _apply_confirm_deal(request, organization, deal: Deal) -> Deal:
    """Sync transactional core of `confirm_deal`."""
    _require_pending(deal)
    with transaction.atomic():
        # Locked and re-checked here, not read off `deal.listing` (which may
        # have been loaded before another, concurrently-confirmed sale deal
        # on the same listing committed): this is the authoritative check
        # that actually prevents one machine being sold twice. The
        # creation-time check in `_apply_create_deal` only rejects a second
        # *pending* sale deal early — it cannot see a confirm that is
        # in-flight in another transaction, which is exactly the race this
        # lock closes. Two different inquiries on the same sale listing can
        # still each become a pending deal if they're created far enough
        # apart that the first hasn't completed yet; this is what stops both
        # from completing.
        listing = None
        if deal.deal_type == Deal.DealType.SALE and deal.listing_id:
            listing = Listing.objects.select_for_update().get(pk=deal.listing_id)
            if listing.status == Listing.Status.SOLD:
                raise HttpError(
                    409, "This listing has already been sold to another customer."
                )

        deal.status = Deal.Status.COMPLETED
        deal.completed_at = timezone.now()
        deal.save(update_fields=["status", "completed_at", "updated_at"])

        # This is the customer's act — they are the ones confirming.
        log_activity(
            request,
            organization,
            ActivityLog.Action.STATUS_CHANGED,
            deal,
            changes={
                "status": {
                    "from": Deal.Status.PENDING_CONFIRMATION,
                    "to": Deal.Status.COMPLETED,
                }
            },
        )

        # §8.4: a completed sale moves the listing to `sold` — more
        # informative on the profile's deal history than `archived` (a
        # success, not a withdrawal). Logged against the *provider*
        # organization, whose listing this is, even though the confirming
        # member belongs to the customer org: the row is about what happened
        # to the listing, not about who held the token.
        if listing is not None:
            before_status = listing.status
            listing.status = Listing.Status.SOLD
            listing.save(update_fields=["status", "updated_at"])
            log_activity(
                request,
                deal.provider_organization,
                ActivityLog.Action.STATUS_CHANGED,
                listing,
                changes={"status": {"from": before_status, "to": Listing.Status.SOLD}},
            )

    return deal


@deals_router.post("/{deal_id}/confirm", response=DealOut)
async def confirm_deal(request, deal_id: UUID):
    """
    The customer side confirms a pending deal.

    Requiring the *other* side to confirm — never the provider who proposed
    it — is what stops an owner farming fake completed-deal history (§4).
    Scoped to deals where the caller is the customer, so the provider's own
    id here is a 404 (§0.1), not a 403.
    """
    organization = writable_organization(request)
    deal = await aget_object_or_404(customer_deals(organization), public_id=deal_id)
    return await sync_to_async(_apply_confirm_deal)(request, organization, deal)


def _apply_decline_deal(request, organization, deal: Deal) -> Deal:
    """Sync transactional core of `decline_deal`."""
    _require_pending(deal)
    with transaction.atomic():
        deal.status = Deal.Status.DECLINED
        deal.save(update_fields=["status", "updated_at"])
        log_activity(
            request,
            organization,
            ActivityLog.Action.STATUS_CHANGED,
            deal,
            changes={
                "status": {
                    "from": Deal.Status.PENDING_CONFIRMATION,
                    "to": Deal.Status.DECLINED,
                }
            },
        )
    return deal


@deals_router.post("/{deal_id}/decline", response=DealOut)
async def decline_deal(request, deal_id: UUID):
    """The customer side declines a pending deal."""
    organization = writable_organization(request)
    deal = await aget_object_or_404(customer_deals(organization), public_id=deal_id)
    return await sync_to_async(_apply_decline_deal)(request, organization, deal)


# ── reviewing a completed deal ───────────────────────────────────────────────

def _apply_create_review(
    request, organization, deal: Deal, data: ReviewCreateIn
) -> Review:
    """Sync transactional core of `create_review`."""
    if deal.status != Deal.Status.COMPLETED:
        raise HttpError(409, "Only a completed deal can be reviewed.")
    # `deal.review` is a reverse OneToOne accessor — `hasattr` is the
    # standard way to probe one without it raising `RelatedObjectDoesNotExist`
    # on a deal that has none yet. This runs inside `sync_to_async`, so the
    # lazy query behind it is allowed here even though the view itself is
    # async.
    if hasattr(deal, "review"):
        raise HttpError(409, "This deal has already been reviewed.")
    if timezone.now() > deal.completed_at + REVIEW_WINDOW:
        raise HttpError(400, "The review window for this deal has closed.")

    with transaction.atomic():
        try:
            review = Review.objects.create(
                deal=deal,
                # Never client-supplied — the reviewer is always the
                # customer side of the deal being reviewed (§0.1).
                author_organization=organization,
                created_by=request.auth,
                rating=data.rating,
                comment=data.comment,
            )
        except IntegrityError as exc:
            raise _integrity_error(exc)

        log_activity(
            request,
            organization,
            ActivityLog.Action.CREATED,
            review,
            changes={"rating": {"from": None, "to": review.rating}},
        )
    return review


@deals_router.post("/{deal_id}/review", response={201: ReviewOut})
async def create_review(request, deal_id: UUID, data: ReviewCreateIn):
    """
    Review a completed deal — the customer side, once, within 30 days of
    `completed_at` (§4).
    """
    organization = writable_organization(request)
    deal = await aget_object_or_404(customer_deals(organization), public_id=deal_id)
    review = await sync_to_async(_apply_create_review)(request, organization, deal, data)
    return 201, review
