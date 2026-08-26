"""
inquiries/schemas.py
Request and response contracts for /inquiries.

Two audiences read the same row from opposite ends, and they are not entitled
to the same fields:

  * The **provider** — the organization that owns the listing — sees who is
    asking, including the sender's phone number. That is the entire point of
    the feature: an inquiry the owner cannot answer is a dead end.
  * The **sender** sees the same message back, but not `handled_by`. Which
    member of the other organization opened it is that organization's internal
    business, not a read receipt owed to a stranger.

`InquiryOut` is the shape both sides share; `InquiryInboxOut` adds the one
field only the provider's own views return.
"""

from datetime import date, datetime
from uuid import UUID

from ninja import Field, Schema

from api.public_schemas import PublicProviderOut


class InquiryOrgOut(PublicProviderOut):
    """
    An organization as the other party sees it.

    `PublicProviderOut` already carries the safe subset (name, region,
    verified badge) and is what `/listings` publishes, so reusing it means an
    inquiry cannot accidentally expose more of an organization than its own
    listings do. `rating` is added because §5 asks for it; there is no review
    model yet, so it is null until /rentals lands — the same treatment
    `ListingOut.rating` gets.
    """

    rating: float | None = None


class InquiryUserOut(Schema):
    """
    The person on one end of the conversation.

    `phone` is here deliberately, and it is why every inquiry endpoint is
    scoped to the two organizations party to it: this is the number the owner
    calls back on.
    """

    id: UUID = Field(alias="public_id")
    name: str
    phone: str | None = None

    @staticmethod
    def resolve_name(obj) -> str:
        return obj.get_full_name() or obj.username


class InquiryListingOut(Schema):
    """
    Enough of the listing to render an inbox row without a second request.

    Not the full `ListingOut`: an inbox is a list of conversations, not a
    catalogue, and the specs are one click away on the listing itself.

    `status` goes to *both* sides, including the sender. It looks like it
    exposes the seller's workflow, but draft, paused and archived all say the
    same thing to a counterparty — "not on the market" — and the sender has a
    real need to know that the offer they asked about has been withdrawn.
    Nothing here is private to the owning org: an inquiry only exists because
    the listing was published in the first place.
    """

    id: UUID = Field(alias="public_id")
    title: str
    listing_type: str
    status: str
    owner: InquiryOrgOut

    @staticmethod
    def resolve_owner(obj) -> dict:
        org = obj.organization
        return {"name": org.name, "region": org.region, "is_verified": org.is_verified}


class InquiryRenterOut(Schema):
    """
    Who is asking — the organization *and* the member acting for it (§0.1).

    Both halves, because either alone is useless: the organization is who the
    deal would be with, the person is who to actually call. `user` is nullable
    only because `created_by` is SET_NULL, so history survives an account
    being deleted.
    """

    organization: InquiryOrgOut
    user: InquiryUserOut | None = None


class InquiryOut(Schema):
    """One inquiry, as its sender sees it."""

    id: UUID = Field(alias="public_id")
    listing_id: UUID
    listing: InquiryListingOut
    renter: InquiryRenterOut

    message: str
    start_date: date | None = None
    end_date: date | None = None

    read: bool
    read_at: datetime | None = None
    created_at: datetime

    @staticmethod
    def resolve_listing_id(obj) -> UUID:
        # Flat, alongside the nested object, because §5's payload has it and
        # it is what the frontend already sends back.
        return obj.listing.public_id

    @staticmethod
    def resolve_renter(obj) -> dict:
        org = obj.customer_organization
        return {
            "organization": {
                "name": org.name,
                "region": org.region,
                "is_verified": org.is_verified,
            },
            "user": obj.created_by,
        }

    @staticmethod
    def resolve_read(obj) -> bool:
        return obj.read_at is not None


class InquiryInboxOut(InquiryOut):
    """
    The provider's view: the same inquiry plus who in their organization
    picked it up. §5 calls this `handled_by`; the column is `read_by`, since
    marking it read *is* the act of taking it on.
    """

    handled_by: InquiryUserOut | None = None

    @staticmethod
    def resolve_handled_by(obj):
        return obj.read_by


# Restated here so pydantic refuses an over-long message before it reaches the
# database, for the reason listings/schemas.py restates its column widths.
# `message` is a TextField and has no width of its own, so this is a product
# decision rather than a mirror: long enough for a real question, short enough
# that the inbox is not a file upload.
MESSAGE_MAX = 2000


class InquiryCreateIn(Schema):
    listing_id: UUID
    message: str = Field(min_length=1, max_length=MESSAGE_MAX)
    # Both optional and independently so: "from the 3rd onwards" is a real
    # request, and a sale inquiry has no dates at all. Ordering is checked in
    # the view and by the inquiry_dates_ordered constraint.
    start_date: date | None = None
    end_date: date | None = None
