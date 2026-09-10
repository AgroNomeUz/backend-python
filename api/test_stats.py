"""
api/test_stats.py
`/stats/landing` and `/stats/owner` (§7).

Two things are worth proving here and nothing else really is.

The first is that the landing numbers moved: every figure that says
"listings" now counts offers on the market rather than machines that happen to
be available, and the sharpest way to show it is to stand the new endpoint
next to the old one over the same fixture and watch them disagree —
`test_an_asset_with_no_listing_counts_on_the_old_path_only` is that test.

The second is that the dashboard is scoped to the caller's own organization
and derived from the token, never from a parameter (§0.1). So the fixture is
two organizations that both have listings, inquiries and views, and most of
what follows checks that neither can see the other's.

In its own module rather than in `api/tests.py`, which is the OTP lifecycle
end to end and has no fixture in common with this.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from api.auth import create_access_token
from equipment.models import Asset, EquipmentCategory, EquipmentModel, Manufacturer
from inquiries.models import Inquiry
from listings.models import Listing, ListingView
from users.models import OrgPermission, Organization, Region, User

LANDING = "/api/v1/stats/landing"
OWNER = "/api/v1/stats/owner"


def auth(user: User) -> dict:
    """Request kwargs carrying a bearer token for `user`."""
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}


class StatsTestCase(TestCase):
    """
    A seller with a fleet and a buyer who looks at it.

    The seller has two members, because the per-member breakdown is only a
    real question with more than one person in the room, and two machines, so
    "on the market" and "published" can be made to differ without emptying the
    fixture.
    """

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(
            name="Andijan Stats", code="ANS", soato="1705"
        )
        cls.other_region = Region.objects.create(
            name="Fergana Stats", code="FGS", soato="1731"
        )

        cls.seller_owner = User.objects.create_user(
            username="stats-seller-owner", password="x", full_name="Seller Owner"
        )
        cls.seller_org = Organization.objects.create(
            name="Stats Seller",
            owner=cls.seller_owner,
            region=cls.region,
            is_verified=True,
        )
        cls.seller_owner.organization = cls.seller_org
        cls.seller_owner.save(update_fields=["organization"])

        # A member who can publish and answer the inbox, so the breakdown has
        # somebody other than the owner to attribute work to.
        cls.seller_member = User.objects.create_user(
            username="stats-seller-member",
            password="x",
            organization=cls.seller_org,
            full_name="Seller Member",
            permissions=[
                OrgPermission.MANAGE_EQUIPMENT,
                OrgPermission.MANAGE_INQUIRIES,
            ],
        )

        cls.buyer_owner = User.objects.create_user(
            username="stats-buyer-owner", password="x", full_name="Buyer Owner"
        )
        cls.buyer_org = Organization.objects.create(
            name="Stats Buyer", owner=cls.buyer_owner, region=cls.other_region
        )
        cls.buyer_owner.organization = cls.buyer_org
        cls.buyer_owner.save(update_fields=["organization"])

        manufacturer = Manufacturer.objects.create(name="MTZ")
        cls.category = EquipmentCategory.objects.create(name="Tractor", slug="tractor")
        cls.other_category = EquipmentCategory.objects.create(
            name="Combine", slug="combine"
        )
        cls.equipment_model = EquipmentModel.objects.create(
            manufacturer=manufacturer, category=cls.category, name="82.1"
        )

        cls.asset = Asset.objects.create(
            organization=cls.seller_org, equipment_model=cls.equipment_model
        )
        cls.spare_asset = Asset.objects.create(
            organization=cls.seller_org, equipment_model=cls.equipment_model
        )

    def setUp(self):
        # `/stats/landing` caches its payload under a fixed key; without this,
        # whichever test ran first would poison the rest.
        cache.clear()

    @classmethod
    def make_listing(cls, **overrides):
        fields = {
            "organization": cls.seller_org,
            "asset": cls.asset,
            "created_by": cls.seller_owner,
            "region": cls.region,
            "listing_type": Listing.ListingType.RENT,
            "status": Listing.Status.ACTIVE,
            "title": "MTZ-82 tractor with operator",
            "price": Decimal("400000.00"),
            "price_unit": Listing.PriceUnit.DAY,
            "published_at": timezone.now(),
        }
        fields.update(overrides)
        return Listing.objects.create(**fields)


class LandingStatsTests(StatsTestCase):
    """The public payload, and the re-rooting §7 asked for."""

    def test_no_token_required(self):
        self.assertEqual(self.client.get(LANDING).status_code, 200)

    def test_shape(self):
        data = self.client.get(LANDING).json()
        self.assertEqual(
            set(data),
            {
                "total_active_listings",
                "total_equipment",
                "total_owner_organizations",
                "total_users",
                "regions_with_listings",
                "listings_by_region",
                "listings_by_category",
                "completed_bookings",
                "completed_bookings_by_region",
                "verified_owners",
                "average_rating",
                "new_listings_last_7_days",
                "new_listings_last_30_days",
                "average_owner_response_minutes",
            },
        )
        # No review model exists yet — this must stay null, not a fabricated
        # number.
        self.assertIsNone(data["average_rating"])

    def test_an_asset_with_no_listing_counts_on_the_old_path_only(self):
        """
        The whole of §7's 🟠, in one comparison: both endpoints see the same
        two available machines, and only the deprecated one calls them
        listings.
        """
        self.assertEqual(self.client.get(LANDING).json()["total_active_listings"], 0)
        old = self.client.get("/api/v1/public/stats").json()
        self.assertEqual(old["total_active_listings"], 2)

    def test_only_listings_on_the_market_are_counted(self):
        self.make_listing()
        self.make_listing(
            asset=self.spare_asset, status=Listing.Status.DRAFT, title="Not published"
        )
        self.assertEqual(self.client.get(LANDING).json()["total_active_listings"], 1)

    def test_a_machine_in_for_repair_leaves_the_count(self):
        """The feed is the intersection of both statuses (§0.5), and so is this."""
        self.make_listing()
        self.assertEqual(self.client.get(LANDING).json()["total_active_listings"], 1)

        self.asset.operational_status = Asset.OperationalStatus.UNDER_MAINTENANCE
        self.asset.save(update_fields=["operational_status"])
        cache.clear()

        self.assertEqual(self.client.get(LANDING).json()["total_active_listings"], 0)

    def test_listings_by_region_counts_listings_and_carries_a_slug(self):
        self.make_listing()
        rows = {row["code"]: row for row in self.client.get(LANDING).json()["listings_by_region"]}
        self.assertEqual(rows[self.region.code]["listing_count"], 1)
        self.assertEqual(rows[self.region.code]["slug"], self.region.slug)
        # Every region, including the empty ones — the map draws them all.
        self.assertEqual(rows[self.other_region.code]["listing_count"], 0)

    def test_listings_by_category_counts_listings(self):
        self.make_listing()
        rows = {
            row["slug"]: row["listing_count"]
            for row in self.client.get(LANDING).json()["listings_by_category"]
        }
        self.assertEqual(rows["tractor"], 1)
        self.assertEqual(rows["combine"], 0)

    def test_regions_with_listings(self):
        data = self.client.get(LANDING).json()
        self.assertEqual(data["regions_with_listings"], 0)

        self.make_listing()
        cache.clear()
        self.assertEqual(self.client.get(LANDING).json()["regions_with_listings"], 1)

    def test_new_listings_counts_when_it_reached_the_public(self):
        """
        `published_at`, not `created_at`: a listing is new to the public when
        the public can see it. A draft written today is not new to anyone.
        """
        self.make_listing(
            asset=self.spare_asset,
            status=Listing.Status.DRAFT,
            published_at=None,
            title="Still a draft",
        )
        self.make_listing(published_at=timezone.now() - timedelta(days=40))
        data = self.client.get(LANDING).json()
        self.assertEqual(data["new_listings_last_7_days"], 0)
        self.assertEqual(data["new_listings_last_30_days"], 0)

        self.make_listing(
            asset=self.spare_asset,
            listing_type=Listing.ListingType.SALE,
            price_unit=Listing.PriceUnit.TOTAL,
            title="On the market today",
        )
        cache.clear()
        data = self.client.get(LANDING).json()
        self.assertEqual(data["new_listings_last_7_days"], 1)
        self.assertEqual(data["new_listings_last_30_days"], 1)

    def test_the_payload_is_cached(self):
        self.client.get(LANDING)
        self.make_listing()
        self.assertEqual(self.client.get(LANDING).json()["total_active_listings"], 0)


class OwnerStatsAccessTests(StatsTestCase):
    """Who may read a dashboard at all."""

    def test_anonymous_is_401(self):
        self.assertEqual(self.client.get(OWNER).status_code, 401)

    def test_an_account_with_no_organization_is_403(self):
        stray = User.objects.create_user(username="stats-stray", password="x")
        self.assertEqual(self.client.get(OWNER, **auth(stray)).status_code, 403)

    def test_a_member_with_no_permission_codes_may_read_it(self):
        """Reads are open to any member of the org (§0.2) — no code gates this."""
        plain = User.objects.create_user(
            username="stats-plain", password="x", organization=self.seller_org
        )
        self.assertEqual(self.client.get(OWNER, **auth(plain)).status_code, 200)


class OwnerListingStatsTests(StatsTestCase):
    """The listing half of the dashboard."""

    def test_published_and_on_the_market_can_differ(self):
        self.make_listing()
        self.asset.operational_status = Asset.OperationalStatus.UNDER_MAINTENANCE
        self.asset.save(update_fields=["operational_status"])

        data = self.client.get(OWNER, **auth(self.seller_owner)).json()
        self.assertEqual(data["active_listings"], 1)
        self.assertEqual(data["listings_on_market"], 0)

    def test_every_status_is_present_even_at_zero(self):
        self.make_listing()
        data = self.client.get(OWNER, **auth(self.seller_owner)).json()
        self.assertEqual(
            data["listings_by_status"],
            {"draft": 0, "active": 1, "paused": 0, "archived": 0},
        )

    def test_another_organizations_listings_are_not_counted(self):
        self.make_listing()
        data = self.client.get(OWNER, **auth(self.buyer_owner)).json()
        self.assertEqual(data["active_listings"], 0)


class OwnerInquiryStatsTests(StatsTestCase):
    """The inbox half, both sides of it."""

    def setUp(self):
        super().setUp()
        self.listing = self.make_listing()
        self.inquiry = Inquiry.objects.create(
            listing=self.listing,
            provider_organization=self.seller_org,
            customer_organization=self.buyer_org,
            created_by=self.buyer_owner,
            message="Is it free next week?",
        )

    def test_the_provider_sees_it_as_received_and_unread(self):
        data = self.client.get(OWNER, **auth(self.seller_owner)).json()
        self.assertEqual(data["inquiries_received"], 1)
        self.assertEqual(data["unread_inquiries"], 1)
        self.assertEqual(data["inquiries_sent"], 0)

    def test_the_customer_sees_it_as_sent(self):
        data = self.client.get(OWNER, **auth(self.buyer_owner)).json()
        self.assertEqual(data["inquiries_sent"], 1)
        self.assertEqual(data["inquiries_received"], 0)
        self.assertEqual(data["unread_inquiries"], 0)

    def test_reading_it_clears_the_badge(self):
        response = self.client.post(
            f"/api/v1/inquiries/{self.inquiry.public_id}/read",
            **auth(self.seller_member),
        )
        self.assertEqual(response.status_code, 200)

        data = self.client.get(OWNER, **auth(self.seller_owner)).json()
        self.assertEqual(data["unread_inquiries"], 0)
        self.assertEqual(data["inquiries_received"], 1)


class OwnerViewStatsTests(StatsTestCase):
    """Views, which is the KPI §7 had no source for until now."""

    def setUp(self):
        super().setUp()
        self.listing = self.make_listing()

    def read_the_listing(self, **extra):
        return self.client.get(f"/api/v1/listings/{self.listing.public_id}", **extra)

    def test_a_strangers_read_shows_up_on_the_dashboard(self):
        self.read_the_listing()
        data = self.client.get(OWNER, **auth(self.seller_owner)).json()
        self.assertEqual(
            data["views"], {"total": 1, "last_7_days": 1, "last_30_days": 1}
        )

    def test_the_sellers_own_reads_are_not_views(self):
        self.read_the_listing(**auth(self.seller_owner))
        self.read_the_listing(**auth(self.seller_member))
        data = self.client.get(OWNER, **auth(self.seller_owner)).json()
        self.assertEqual(data["views"]["total"], 0)

    def test_the_windows_are_windows(self):
        today = timezone.localdate()
        for age in (0, 3, 10, 100):
            ListingView.objects.create(
                listing=self.listing,
                organization=self.seller_org,
                viewer_key=f"{age:064d}",
                viewed_on=today - timedelta(days=age),
            )
        data = self.client.get(OWNER, **auth(self.seller_owner)).json()
        self.assertEqual(
            data["views"], {"total": 4, "last_7_days": 2, "last_30_days": 3}
        )

    def test_another_organizations_views_are_not_counted(self):
        self.read_the_listing()
        data = self.client.get(OWNER, **auth(self.buyer_owner)).json()
        self.assertEqual(data["views"]["total"], 0)


class OwnerMemberBreakdownTests(StatsTestCase):
    """
    Per-member contributions, read off `ActivityLog`.

    Driven through the endpoints rather than by writing audit rows by hand:
    the point of the breakdown is that §0.3's coverage is what makes it
    answerable, and a fixture that wrote its own rows would still pass if a
    write endpoint stopped logging.
    """

    def dashboard(self, user=None):
        data = self.client.get(OWNER, **auth(user or self.seller_owner)).json()
        return {row["name"]: row for row in data["members"]}

    def test_every_current_member_appears_even_at_zero(self):
        members = self.dashboard()
        self.assertEqual(set(members), {"Seller Owner", "Seller Member"})
        self.assertEqual(members["Seller Member"]["listings_created"], 0)
        self.assertEqual(members["Seller Member"]["inquiries_handled"], 0)

    def test_a_deactivated_member_drops_off_the_roster(self):
        self.seller_member.is_active = False
        self.seller_member.save(update_fields=["is_active"])
        self.assertEqual(set(self.dashboard()), {"Seller Owner"})

    def test_listings_are_attributed_to_whoever_published_them(self):
        response = self.client.post(
            "/api/v1/listings",
            data={
                "asset_id": str(self.asset.public_id),
                "listing_type": "rent",
                "status": "active",
                "title": "Published by the member",
                "price": "400000.00",
                "price_unit": "day",
            },
            content_type="application/json",
            **auth(self.seller_member),
        )
        self.assertEqual(response.status_code, 201)

        members = self.dashboard()
        self.assertEqual(members["Seller Member"]["listings_created"], 1)
        self.assertEqual(members["Seller Owner"]["listings_created"], 0)

    def test_inquiries_are_attributed_to_whoever_picked_them_up(self):
        listing = self.make_listing()
        inquiry = Inquiry.objects.create(
            listing=listing,
            provider_organization=self.seller_org,
            customer_organization=self.buyer_org,
            created_by=self.buyer_owner,
            message="Still free?",
        )
        self.client.post(
            f"/api/v1/inquiries/{inquiry.public_id}/read", **auth(self.seller_member)
        )

        members = self.dashboard()
        self.assertEqual(members["Seller Member"]["inquiries_handled"], 1)
        self.assertEqual(members["Seller Owner"]["inquiries_handled"], 0)

    def test_sending_an_inquiry_is_not_handling_one(self):
        """
        An inquiry logs against the actor's own org (§0.3), so the sender's
        history has a `created` row for it. That is "we sent one", and it must
        not read as "we answered one" on their dashboard.
        """
        listing = self.make_listing()
        response = self.client.post(
            "/api/v1/inquiries",
            data={
                "listing_id": str(listing.public_id),
                "message": "Is this available in March?",
            },
            content_type="application/json",
            **auth(self.buyer_owner),
        )
        self.assertEqual(response.status_code, 201)

        members = self.dashboard(self.buyer_owner)
        self.assertEqual(members["Buyer Owner"]["inquiries_handled"], 0)
