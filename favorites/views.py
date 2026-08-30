"""
favorites/views.py
Saving a listing, unsaving it, and the two ways of reading the result.

Three decisions shape all of it:

  * **No permission code.** §0.2 gates every write behind a code, and this is
    the one write that is not gated. The codes exist to control what a member
    may do *on behalf of* the organization — spend its money, publish under
    its name, answer its inbox. A favorite commits the org to nothing: the row
    is the member's own, and requiring an admin to grant a code before a new
    member can bookmark a tractor would be a support ticket, not a safeguard.
    Membership is the whole check.
  * **Idempotent by identity, not by retry.** `PUT` and `DELETE` name the
    *listing*, never the favorite, so the client does not have to remember an
    id it never chose — clicking save twice is one row, unsaving twice is one
    204. The database backs this up with `favorite_unique_per_member`; the
    view's `get_or_create` is the fast path, not the guarantee.
  * **A no-op writes no audit row.** §0.3 requires a write to be logged, but
    the second identical `PUT` is not a write. Logging it would fill an
    organization's history with the noise of one member's double-click — the
    same distinction `mark_inquiry_read` draws.

Async endpoints with the transactional body in a sync `_apply_*` helper, for
the reason equipment/views.py sets out: Django has no async
`transaction.atomic()`, and a write plus its audit row have to commit
together.
"""

from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import transaction
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.pagination import LimitOffsetPagination, paginate

from core.audit import log_activity
from core.models import ActivityLog
from listings.views import (
    LISTING_PREFETCH_RELATED,
    LISTING_SELECT_RELATED,
    published_listings,
)
from users.models import Organization
from users.permissions import caller_organization

from .models import Favorite
from .schemas import FavoriteOut

favorites_router = Router(tags=["Favorites"])


# ── querysets ─────────────────────────────────────────────────────────────────

def _favorite_relations(queryset):
    """
    Everything FavoriteOut serialises, joined up front.

    `FavoriteOut` nests the whole of `ListingOut`, so the listing's own joins
    are needed here too, one level deeper — taken from the tuples in
    listings/views.py rather than restated, since a relation added to
    `ListingOut` and missed here would not be an N+1 but a
    SynchronousOnlyOperation at serialisation time.
    """
    return queryset.select_related(
        "user",
        "listing",
        *(f"listing__{name}" for name in LISTING_SELECT_RELATED),
    ).prefetch_related(*(f"listing__{name}" for name in LISTING_PREFETCH_RELATED))


def org_favorites(organization: Organization):
    """
    The organization's shared shortlist — everything anyone here has saved.

    Not filtered to listings still on the market: a favorite records what a
    member chose to keep, and dropping the rows that have since been paused or
    archived would silently shrink the list they curated. `ListingOut.status`
    tells the client which ones are no longer available.
    """
    return _favorite_relations(Favorite.objects.filter(organization=organization))


# ── the two views ─────────────────────────────────────────────────────────────

@favorites_router.get("", response=list[FavoriteOut])
@paginate(LimitOffsetPagination)
async def list_favorites(request, scope: str | None = None):
    """
    Saved listings, newest first.

    The caller's own by default, because a shortlist is a personal working aid
    and a member opening `/favorites` means "mine". `?scope=organization` is
    the team's, which is the half §0.1 asks for and the reason the row carries
    both columns. Any other value of `scope` falls back to the personal list
    rather than 400ing — the parameter widens a view, and a typo should not
    turn a saved list into an error page.
    """
    queryset = org_favorites(caller_organization(request))
    if scope != "organization":
        queryset = queryset.filter(user=request.auth)
    return queryset


# ── saving and unsaving ───────────────────────────────────────────────────────

def _apply_save_favorite(request, organization, listing_id: UUID) -> Favorite:
    """Sync transactional core of `save_favorite`."""
    # Resolved through the public feed, exactly as an inquiry is: you may only
    # save something actually on the market, and a draft, a paused offer or a
    # machine in for repair 404s like a stranger's id does (§0.1). A listing
    # already saved that later comes off the market stays saved — this gate is
    # on the act of saving, not on the row.
    listing = get_object_or_404(published_listings(), public_id=listing_id)

    with transaction.atomic():
        favorite, created = Favorite.objects.get_or_create(
            organization=organization,
            # From the token, never from the body — a client-supplied user or
            # organization id is what §0.1 rules out.
            user=request.auth,
            listing=listing,
        )
        if created:
            # No `changes`: a favorite has no fields of its own to diff. What
            # the history needs is who, what and when, and `target_repr` plus
            # the row's own columns carry all three.
            log_activity(request, organization, ActivityLog.Action.CREATED, favorite)

    return org_favorites(organization).get(pk=favorite.pk)


@favorites_router.put("/{listing_id}", response=FavoriteOut)
async def save_favorite(request, listing_id: UUID):
    """
    Save a listing. Idempotent — the second call returns the first row.

    `200` either way rather than `201` then `200`: the point of an idempotent
    PUT is that the client need not care whether it had already saved this,
    and a status code that differs on the second click is exactly the thing it
    would have to care about.
    """
    organization = caller_organization(request)
    return await sync_to_async(_apply_save_favorite)(request, organization, listing_id)


def _apply_remove_favorite(request, organization, listing_id: UUID) -> None:
    """Sync transactional core of `remove_favorite`."""
    favorite = Favorite.objects.filter(
        organization=organization, user=request.auth, listing__public_id=listing_id
    ).first()
    if favorite is None:
        # Idempotent, and deliberately not a 404 for an unknown listing id
        # either: "it is not on your list" is the true and complete answer to
        # both, and telling the two apart would let the endpoint be used to
        # probe for listings the caller cannot otherwise see.
        return

    with transaction.atomic():
        # Read before the delete: the audit row keeps a label for something
        # that no longer exists, which is the whole reason it is written.
        log_activity(request, organization, ActivityLog.Action.DELETED, favorite)
        favorite.delete()


@favorites_router.delete("/{listing_id}", response={204: None})
async def remove_favorite(request, listing_id: UUID):
    """
    Unsave a listing. Idempotent — `204` whether or not it was on the list.

    Scoped to the caller's own favorites, not the organization's: the shared
    shortlist is readable by the team but a member's picks are theirs to
    remove. Taking someone else's off it is not something the endpoint can
    express.
    """
    organization = caller_organization(request)
    await sync_to_async(_apply_remove_favorite)(request, organization, listing_id)
    return 204, None
