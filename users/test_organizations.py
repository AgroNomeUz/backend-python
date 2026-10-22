"""
users/test_organizations.py
The public seller profile — seller-profile-proposal.md §2/§3/§8.

`organizations_router` isn't mounted in `api/views.py` yet (that's a
separate integration step, done once the sibling write-side work in `deals`
lands too), so these tests build a small standalone `NinjaAPI` that mirrors
production's auth config (`auth=JWTBearer()`) and mounts only this router —
`ninja.testing.TestAsyncClient` against that, rather than against the real
`/api/v1/...` app. This still exercises the real view functions, the real
auth enforcement and the real response schemas; it just doesn't prove the
mount point itself, which is the one thing left for integration.

Built around one provider org (the profile being viewed) and one customer
org (a past counterparty), the same "two organizations" shape
inquiries/tests.py uses, since a deal is exactly the same pair of sides.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from ninja import NinjaAPI
from ninja.testing import TestAsyncClient

from api.auth import JWTBearer, create_access_token
from deals.models import Deal, Review
from equipment.models import Asset, EquipmentCategory, EquipmentModel, Manufacturer
from listings.models import Listing
from users.models import Organization, Region, User
from users.organizations import organizations_router


def build_test_api() -> NinjaAPI:
    """
    A standalone API carrying only `organizations_router`, with the same
    default auth production uses. A fresh `urls_namespace` per call, because
    django-ninja registers URL names globally and every test needs its own.
    """
    api = NinjaAPI(auth=JWTBearer(), urls_namespace=f"test-organizations-{uuid.uuid4().hex}")
    api.add_router("/organizations", organizations_router)
    return api


def auth_headers(user: User) -> dict:
    token = create_access_token(user.public_id)
    return {"Authorization": f"Bearer {token}"}


class OrganizationProfileTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(name="Fergana Profile", code="FEP", soato="1704")

        cls.provider_owner = User.objects.create_user(
            username="provider-owner", password="x", full_name="Provider Owner"
        )
        cls.provider_org = Organization.objects.create(
            name="Provider Org",
            owner=cls.provider_owner,
            region=cls.region,
            is_verified=True,
            entity_type=Organization.EntityType.LEGAL_ENTITY,
        )
        cls.provider_owner.organization = cls.provider_org
        cls.provider_owner.save(update_fields=["organization"])

        cls.customer_owner = User.objects.create_user(
            username="customer-owner",
            password="x",
            full_name="Alisher Karimov",
        )
        cls.customer_org = Organization.objects.create(
            name="Alisher Karimov",
            owner=cls.customer_owner,
            region=cls.region,
            entity_type=Organization.EntityType.INDIVIDUAL,
        )
        cls.customer_owner.organization = cls.customer_org
        cls.customer_owner.save(update_fields=["organization"])

        cls.stranger_owner = User.objects.create_user(
            username="stranger-owner", password="x", full_name="Stranger Owner"
        )

        manufacturer = Manufacturer.objects.create(name="MTZ")
        category = EquipmentCategory.objects.create(name="Tractor", slug="tractor")
        equipment_model = EquipmentModel.objects.create(
            manufacturer=manufacturer, category=category, name="82.1"
        )
        cls.asset = Asset.objects.create(
            organization=cls.provider_org, equipment_model=equipment_model
        )

    def setUp(self):
        self.api = build_test_api()
        self.client = TestAsyncClient(self.api)

    def make_listing(self, **overrides):
        fields = {
            "organization": self.provider_org,
            "asset": self.asset,
            "created_by": self.provider_owner,
            "region": self.region,
            "listing_type": Listing.ListingType.RENT,
            "status": Listing.Status.ACTIVE,
            "title": "MTZ-82.1",
            "price": Decimal("400000.00"),
            "price_unit": Listing.PriceUnit.DAY,
        }
        fields.update(overrides)
        return Listing.objects.create(**fields)

    def make_deal(self, **overrides):
        fields = {
            "provider_organization": self.provider_org,
            "customer_organization": self.customer_org,
            "deal_type": Deal.DealType.RENT,
            "status": Deal.Status.COMPLETED,
            "completed_at": timezone.now(),
        }
        fields.update(overrides)
        return Deal.objects.create(**fields)


class ProfileVisibilityTests(OrganizationProfileTestCase):
    async def test_an_org_with_nothing_public_404s(self):
        resp = await self.client.get(f"/organizations/{self.provider_org.public_id}")
        self.assertEqual(resp.status_code, 404)

    async def test_an_unknown_id_404s(self):
        resp = await self.client.get(f"/organizations/{uuid.uuid4()}")
        self.assertEqual(resp.status_code, 404)

    async def test_an_active_listing_makes_the_profile_public(self):
        await self._acreate_listing()
        resp = await self.client.get(f"/organizations/{self.provider_org.public_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], str(self.provider_org.public_id))
        self.assertEqual(data["name"], "Provider Org")
        self.assertEqual(data["entity_type"], "legal_entity")
        self.assertTrue(data["is_verified"])
        self.assertEqual(data["region"]["code"], "FEP")
        self.assertIsNone(data["logo_url"])
        self.assertEqual(data["stats"]["active_listings"], 1)
        self.assertEqual(data["stats"]["completed_deals"], 0)
        self.assertIsNone(data["stats"]["rating_avg"])
        self.assertEqual(data["stats"]["review_count"], 0)

    async def test_a_completed_deal_alone_makes_the_profile_public_too(self):
        """No listing needed — a completed deal is public evidence on its own."""
        await self._acreate_deal()
        resp = await self.client.get(f"/organizations/{self.provider_org.public_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["stats"]["completed_deals"], 1)

    async def test_a_pending_deal_alone_does_not_make_the_profile_public(self):
        await self._acreate_deal(status=Deal.Status.PENDING_CONFIRMATION, completed_at=None)
        resp = await self.client.get(f"/organizations/{self.provider_org.public_id}")
        self.assertEqual(resp.status_code, 404)

    async def test_stats_reflect_a_review(self):
        deal = await self._acreate_deal()
        await Review.objects.acreate(
            deal=deal, author_organization=self.customer_org, rating=5, comment="Good"
        )
        resp = await self.client.get(f"/organizations/{self.provider_org.public_id}")
        data = resp.json()
        self.assertEqual(data["stats"]["rating_avg"], 5.0)
        self.assertEqual(data["stats"]["review_count"], 1)

    async def _acreate_listing(self, **overrides):
        from asgiref.sync import sync_to_async

        return await sync_to_async(self.make_listing)(**overrides)

    async def _acreate_deal(self, **overrides):
        from asgiref.sync import sync_to_async

        return await sync_to_async(self.make_deal)(**overrides)


class ContactsTests(OrganizationProfileTestCase):
    async def test_a_guest_is_refused(self):
        resp = await self.client.get(
            f"/organizations/{self.provider_org.public_id}/contacts"
        )
        self.assertEqual(resp.status_code, 401)

    async def test_any_authenticated_user_may_read_contacts_regardless_of_org(self):
        """Not gated on membership — a stranger's account is enough."""
        from asgiref.sync import sync_to_async

        def _setup():
            self.provider_org.phone = "+998901234567"
            self.provider_org.email = "info@provider.uz"
            self.provider_org.save(update_fields=["phone", "email"])
            self.make_listing()

        await sync_to_async(_setup)()

        resp = await self.client.get(
            f"/organizations/{self.provider_org.public_id}/contacts",
            headers=auth_headers(self.stranger_owner),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["phone"], "+998901234567")
        self.assertEqual(data["email"], "info@provider.uz")
        self.assertIsNone(data["address"])

    async def test_contacts_404_for_an_org_with_nothing_public(self):
        resp = await self.client.get(
            f"/organizations/{self.provider_org.public_id}/contacts",
            headers=auth_headers(self.stranger_owner),
        )
        self.assertEqual(resp.status_code, 404)


class DealsAndReviewsListTests(OrganizationProfileTestCase):
    async def test_deals_list_shape_and_display_name(self):
        from asgiref.sync import sync_to_async

        def _setup():
            listing = self.make_listing()
            deal = self.make_deal(listing=listing)
            Review.objects.create(
                deal=deal,
                author_organization=self.customer_org,
                rating=4,
                comment="Solid tractor.",
            )

        await sync_to_async(_setup)()

        resp = await self.client.get(
            f"/organizations/{self.provider_org.public_id}/deals"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 1)
        row = data["items"][0]
        self.assertEqual(row["deal_type"], "rent")
        self.assertEqual(row["listing"]["title"], "MTZ-82.1")
        self.assertEqual(row["listing"]["equipment_type"], "tractor")
        # Individual entity_type -> first name + last initial, never the raw
        # `Organization.name`, which is a person's full name here.
        self.assertEqual(row["counterparty"]["display_name"], "Alisher K.")
        self.assertEqual(row["review"]["rating"], 4)

    async def test_a_deal_with_no_review_yet_carries_a_null_review(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(self.make_deal)()
        resp = await self.client.get(
            f"/organizations/{self.provider_org.public_id}/deals"
        )
        self.assertIsNone(resp.json()["items"][0]["review"])

    async def test_pending_deals_are_excluded(self):
        from asgiref.sync import sync_to_async

        def _setup():
            self.make_listing()  # gives the org something public on its own
            self.make_deal(status=Deal.Status.PENDING_CONFIRMATION, completed_at=None)

        await sync_to_async(_setup)()
        resp = await self.client.get(
            f"/organizations/{self.provider_org.public_id}/deals"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 0)

    async def test_a_deleted_listing_leaves_a_null_listing_on_the_deal_row(self):
        from asgiref.sync import sync_to_async

        def _setup():
            listing = self.make_listing()
            self.make_deal(listing=listing)
            listing.delete()

        await sync_to_async(_setup)()

        resp = await self.client.get(
            f"/organizations/{self.provider_org.public_id}/deals"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["items"][0]["listing"])

    async def test_reviews_list_shape_and_legal_entity_display_name(self):
        """A legal_entity counterparty shows its registered name."""
        from asgiref.sync import sync_to_async

        def _setup():
            legal_owner = User.objects.create_user(
                username="legal-owner", password="x", full_name="Legal Owner"
            )
            legal_org = Organization.objects.create(
                name="Legal Customer LLC",
                owner=legal_owner,
                region=self.region,
                entity_type=Organization.EntityType.LEGAL_ENTITY,
            )
            legal_owner.organization = legal_org
            legal_owner.save(update_fields=["organization"])

            listing = self.make_listing()
            deal = self.make_deal(customer_organization=legal_org, listing=listing)
            Review.objects.create(
                deal=deal, author_organization=legal_org, rating=3, comment="Fine."
            )

        await sync_to_async(_setup)()

        resp = await self.client.get(
            f"/organizations/{self.provider_org.public_id}/reviews"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 1)
        row = data["items"][0]
        self.assertEqual(row["rating"], 3)
        self.assertEqual(row["author"]["display_name"], "Legal Customer LLC")
        self.assertEqual(row["deal"]["listing_title"], "MTZ-82.1")

    async def test_reviews_are_scoped_to_this_organization_as_provider(self):
        """A review of a *different* provider's deal must not show up here."""
        from asgiref.sync import sync_to_async

        def _setup():
            other_owner = User.objects.create_user(
                username="other-provider-owner", password="x", full_name="Other Provider"
            )
            other_org = Organization.objects.create(
                name="Other Provider Org", owner=other_owner, region=self.region
            )
            other_owner.organization = other_org
            other_owner.save(update_fields=["organization"])
            other_deal = Deal.objects.create(
                provider_organization=other_org,
                customer_organization=self.customer_org,
                deal_type=Deal.DealType.RENT,
                status=Deal.Status.COMPLETED,
                completed_at=timezone.now(),
            )
            Review.objects.create(
                deal=other_deal, author_organization=self.customer_org, rating=5
            )
            self.make_deal()  # gives the provider org something public

        await sync_to_async(_setup)()

        resp = await self.client.get(
            f"/organizations/{self.provider_org.public_id}/reviews"
        )
        self.assertEqual(resp.json()["count"], 0)


class OldCompletedDealStillListsTests(OrganizationProfileTestCase):
    """
    Sanity check that a long-completed deal still lists correctly — the
    30-day review *window* is a write-side rule (§4), not something the
    read side filters on.
    """

    async def test_an_old_completed_deal_still_lists(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(self.make_deal)(
            completed_at=timezone.now() - timedelta(days=90)
        )
        resp = await self.client.get(
            f"/organizations/{self.provider_org.public_id}/deals"
        )
        self.assertEqual(resp.json()["count"], 1)
