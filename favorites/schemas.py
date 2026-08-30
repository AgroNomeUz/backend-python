"""
favorites/schemas.py
Response contracts for /favorites.

One schema, not two. `inquiries` splits its output because the two orgs
reading a row are not entitled to the same fields; here both views are read by
the *same* organization, so there is nothing to withhold — a member can list
the shared shortlist whenever they like, and hiding `saved_by` from their own
half of it would only make the two responses harder to render with one
component.

The listing is nested rather than flattened in: `id` on this payload is the
favorite's, and a saved listing that also carried a bare `id` would be a
standing invitation to send the wrong one back to
`PUT /favorites/{listing_id}`.
"""

from datetime import datetime
from uuid import UUID

from ninja import Field, Schema

from listings.schemas import ListingOut


class FavoriteUserOut(Schema):
    """
    Who saved it. A colleague, so name and id are the whole of it — `phone`
    belongs on `InquiryUserOut`, where the reader is a stranger who has to
    call back, and on `/members`, which is where an org looks its own people
    up.
    """

    id: UUID = Field(alias="public_id")
    name: str

    @staticmethod
    def resolve_name(obj) -> str:
        return obj.get_full_name() or obj.username


class FavoriteOut(Schema):
    """One saved listing."""

    id: UUID = Field(alias="public_id")

    # The full feed card (§0.5), so a saved list renders with the same
    # component as /listings and needs no second request per row. It carries
    # `status`, which is the point of returning archived and paused offers
    # here rather than filtering them out: the honest answer to "what did I
    # save?" includes the one that has since been withdrawn.
    listing: ListingOut

    saved_by: FavoriteUserOut
    saved_at: datetime

    @staticmethod
    def resolve_saved_by(obj):
        return obj.user

    @staticmethod
    def resolve_saved_at(obj) -> datetime:
        return obj.created_at
