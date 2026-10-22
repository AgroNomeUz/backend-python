"""
deals/schemas.py
Request/response contracts for the deal/review write flow
(seller-profile-proposal.md §4, decided in §8).

Kept to what these four endpoints themselves need to echo back. The richer,
nested shape `GET /organizations/{id}/deals` and `/reviews` return lives in
`users/organizations.py` — a different read, built for the public profile,
not for confirming what was just written.
"""

from datetime import datetime
from uuid import UUID

from ninja import Field, Schema

from .models import Deal


class DealCreateIn(Schema):
    # Typed against the enum, the way `ListingCreateIn.listing_type` is
    # (listings/schemas.py), so an unknown value is a plain 422 rather than
    # reaching the view's own check.
    deal_type: Deal.DealType


class DealOut(Schema):
    id: UUID = Field(alias="public_id")
    deal_type: str
    status: str
    completed_at: datetime | None = None
    created_at: datetime
    listing_id: UUID | None = None
    provider_organization_id: UUID
    customer_organization_id: UUID

    @staticmethod
    def resolve_listing_id(obj) -> UUID | None:
        return obj.listing.public_id if obj.listing_id else None

    @staticmethod
    def resolve_provider_organization_id(obj) -> UUID:
        return obj.provider_organization.public_id

    @staticmethod
    def resolve_customer_organization_id(obj) -> UUID:
        return obj.customer_organization.public_id


class ReviewCreateIn(Schema):
    # `ge=1, le=5` restates `review_rating_range` as a readable 422, the same
    # move `ListingCreateIn.price`'s `ge=0` makes against its own constraint.
    rating: int = Field(ge=1, le=5)
    comment: str = ""


class ReviewOut(Schema):
    id: UUID = Field(alias="public_id")
    rating: int
    comment: str
    created_at: datetime
    deal_id: UUID

    @staticmethod
    def resolve_deal_id(obj) -> UUID:
        return obj.deal.public_id
