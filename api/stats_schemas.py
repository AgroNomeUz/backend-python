"""
api/stats_schemas.py
Response contracts for `/stats` (§7).

Deliberately not shared with `api/public_schemas.py`, whose near-twins
(`PublicRegionListingsOut`, `CategoryListingsOut`, `RegionOrdersOut`) are
frozen around the asset-rooted counts the deployed frontend still parses. The
duplication is the point: when `/public/stats` is finally deleted, that module
goes with it and nothing here has to be untangled first.

The one visible difference in the region rows is `slug`. §3 made the slug the
key a region page is addressed by, and the landing map links straight to those
pages — with only `code` the client would need a second call to build a link.
"""

from uuid import UUID

from ninja import Field, Schema


# ── the landing page ─────────────────────────────────────────────────────────

class RegionListingsOut(Schema):
    """One region and how many listings are on the market in it."""

    id: UUID = Field(alias="public_id")
    name: str
    code: str
    slug: str
    listing_count: int


class CategoryListingsOut(Schema):
    """One equipment category and its share of the market."""

    id: UUID = Field(alias="public_id")
    name: str
    slug: str
    listing_count: int


class RegionOrdersOut(Schema):
    id: UUID = Field(alias="public_id")
    name: str
    code: str
    completed_orders: int


class LandingStatsOut(Schema):
    """
    The headline numbers, counted off `Listing`.

    Field-for-field the payload `/public/stats` returns, plus `slug` on the
    region rows, so migrating is a change of path and nothing else — but the
    six listing-shaped numbers now count *offers on the market* rather than
    available machines, which is what they always claimed to (§7).

    `total_equipment` and `total_owner_organizations` are the exception and
    stay asset-rooted on purpose: they are the fleet behind the market — how
    many machines exist and how many organizations own one — and re-rooting
    them on listings would make them a second, worse copy of
    `total_active_listings`.
    """

    total_active_listings: int
    total_equipment: int
    total_owner_organizations: int
    total_users: int
    regions_with_listings: int
    listings_by_region: list[RegionListingsOut]
    listings_by_category: list[CategoryListingsOut]
    completed_bookings: int
    completed_bookings_by_region: list[RegionOrdersOut]
    verified_owners: int
    # No review/rating model exists yet — always null until /rentals builds one.
    average_rating: float | None = None
    new_listings_last_7_days: int
    new_listings_last_30_days: int
    average_owner_response_minutes: float | None = None


# ── the owner dashboard ──────────────────────────────────────────────────────

class ListingViewsOut(Schema):
    """
    Views of this organization's listings, by window.

    Daily unique viewers, not page loads, and never the organization's own
    staff — see `listings.models.ListingView`. The windows include today, so
    `last_7_days` is today and the six days before it.
    """

    total: int
    last_7_days: int
    last_30_days: int


class MemberActivityOut(Schema):
    """
    What one member has done, read off `ActivityLog` rather than counted on
    the objects themselves.

    That is what makes `inquiries_handled` answerable at all: an inquiry
    records `read_by`, but only the audit trail knows it was *this* member who
    marked it, in this organization, and the same source will answer the next
    such question without another column.
    """

    id: UUID = Field(alias="public_id")
    name: str
    listings_created: int
    inquiries_handled: int


class OwnerStatsOut(Schema):
    """
    The organization dashboard.

    `active_listings` is the KPI §7 asks for — listings the organization has
    published — and `listings_on_market` is how many of those a stranger can
    actually find. They differ whenever a machine is in for repair, and a
    dashboard that showed only the first would be explaining that gap in a
    support ticket instead.
    """

    active_listings: int
    listings_on_market: int
    # Every status, including the ones that are zero, so the client can render
    # a fixed set of tiles without inventing the missing keys.
    listings_by_status: dict[str, int]

    unread_inquiries: int
    inquiries_received: int
    inquiries_sent: int

    views: ListingViewsOut
    members: list[MemberActivityOut]
