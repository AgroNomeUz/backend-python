"""
deals/tests.py
An inquiry becoming a deal, the other side confirming or declining it, and
the review that follows a completed one.

Built around the same seller/buyer pair `inquiries/tests.py` uses, plus one
inquiry already sitting in the seller's inbox — most of what can go wrong
here is a scoping mistake (the provider's write vs. the customer's), so the
tests check the wrong side's attempt as often as the right one.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from api.auth import create_access_token
from core.models import ActivityLog
from deals.models import Deal, Review
from equipment.models import Asset, EquipmentCategory, EquipmentModel, Manufacturer
from inquiries.models import Inquiry
from listings.models import Listing
from users.models import OrgPermission, Organization, Region, User


def auth(user: User) -> dict:
    """Request kwargs carrying a bearer token for `user`."""
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}


class DealTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(name="Andijan Deal", code="AND", soato="1704")

        cls.seller_owner = User.objects.create_user(
            username="seller-owner", password="x", full_name="Seller Owner"
        )
        cls.seller_org = Organization.objects.create(
            name="Seller Org", owner=cls.seller_owner, region=cls.region, is_verified=True
        )
        cls.seller_owner.organization = cls.seller_org
        cls.seller_owner.save(update_fields=["organization"])
        cls.seller_member = User.objects.create_user(
            username="seller-member",
            password="x",
            organization=cls.seller_org,
            full_name="Seller Member",
        )

        cls.buyer_owner = User.objects.create_user(
            username="buyer-owner", password="x", full_name="Buyer Owner"
        )
        cls.buyer_org = Organization.objects.create(
            name="Buyer Org", owner=cls.buyer_owner, region=cls.region
        )
        cls.buyer_owner.organization = cls.buyer_org
        cls.buyer_owner.save(update_fields=["organization"])
        cls.buyer_member = User.objects.create_user(
            username="buyer-member",
            password="x",
            organization=cls.buyer_org,
            full_name="Buyer Member",
        )

        manufacturer = Manufacturer.objects.create(name="MTZ")
        category = EquipmentCategory.objects.create(name="Tractor", slug="tractor")
        equipment_model = EquipmentModel.objects.create(
            manufacturer=manufacturer, category=category, name="82.1"
        )
        cls.asset = Asset.objects.create(
            organization=cls.seller_org, equipment_model=equipment_model
        )

    @classmethod
    def make_listing(cls, **overrides):
        fields = {
            "organization": cls.seller_org,
            "asset": cls.asset,
            "created_by": cls.seller_owner,
            "region": cls.region,
            "listing_type": Listing.ListingType.RENT,
            "status": Listing.Status.ACTIVE,
            "title": "MTZ-82 tractor",
            "price": "400000.00",
            "price_unit": Listing.PriceUnit.DAY,
        }
        fields.update(overrides)
        return Listing.objects.create(**fields)

    @classmethod
    def make_inquiry(cls, listing=None, **overrides):
        fields = {
            "listing": listing or cls.listing,
            "provider_organization": cls.seller_org,
            "customer_organization": cls.buyer_org,
            "created_by": cls.buyer_owner,
            "message": "Interested — is it available?",
        }
        fields.update(overrides)
        return Inquiry.objects.create(**fields)

    def setUp(self):
        # Fresh per test: several tests create a deal and mutate its
        # status, and a class-level fixture would leak between them.
        self.listing = self.make_listing()
        self.inquiry = self.make_inquiry(listing=self.listing)

    def grant_deals_permission(self, *users):
        for user in users:
            user.permissions = [OrgPermission.MANAGE_DEALS]
            user.save(update_fields=["permissions"])

    def setUpAuthorizedActors(self):
        self.grant_deals_permission(self.seller_owner, self.buyer_owner)


class CreateDealTests(DealTestCase):
    """`POST /inquiries/{id}/deal` — the provider's write."""

    def create_deal(self, user=None, inquiry=None, deal_type="rent"):
        target = inquiry or self.inquiry
        return self.client.post(
            f"/api/v1/inquiries/{target.public_id}/deal",
            data={"deal_type": deal_type},
            content_type="application/json",
            **auth(user or self.seller_owner),
        )

    def test_the_provider_can_create_a_deal(self):
        response = self.create_deal()
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()
        self.assertEqual(payload["deal_type"], "rent")
        self.assertEqual(payload["status"], "pending_confirmation")
        self.assertIsNone(payload["completed_at"])
        self.assertEqual(payload["listing_id"], str(self.listing.public_id))
        self.assertEqual(payload["provider_organization_id"], str(self.seller_org.public_id))
        self.assertEqual(payload["customer_organization_id"], str(self.buyer_org.public_id))

    def test_creating_a_deal_requires_the_deals_permission(self):
        self.assertEqual(self.create_deal(user=self.seller_member).status_code, 403)

    def test_a_member_with_the_code_may_create_one(self):
        self.grant_deals_permission(self.seller_member)
        self.assertEqual(self.create_deal(user=self.seller_member).status_code, 201)

    def test_the_customer_cannot_create_a_deal_on_their_own_inquiry(self):
        """
        `create_deal` is the provider's write — resolved through their own
        inbox, so the customer's id for the same inquiry is a 404 (§0.1).
        """
        self.assertEqual(self.create_deal(user=self.buyer_owner).status_code, 404)

    def test_a_third_organizations_inquiry_id_is_a_404(self):
        stranger_owner = User.objects.create_user(
            username="stranger-owner", password="x", full_name="Stranger Owner"
        )
        stranger_org = Organization.objects.create(
            name="Stranger Org", owner=stranger_owner, region=self.region
        )
        stranger_owner.organization = stranger_org
        stranger_owner.save(update_fields=["organization"])
        self.grant_deals_permission(stranger_owner)
        self.assertEqual(self.create_deal(user=stranger_owner).status_code, 404)

    def test_a_second_deal_on_the_same_inquiry_is_a_409(self):
        first = self.create_deal()
        self.assertEqual(first.status_code, 201)
        second = self.create_deal()
        self.assertEqual(second.status_code, 409)

    def test_creation_is_logged_against_the_provider(self):
        self.create_deal()
        entry = ActivityLog.objects.get(content_type__model="deal")
        self.assertEqual(entry.organization, self.seller_org)
        self.assertEqual(entry.actor, self.seller_owner)
        self.assertEqual(entry.action, ActivityLog.Action.CREATED)

    def test_an_unknown_deal_type_is_a_422(self):
        self.assertEqual(self.create_deal(deal_type="lease").status_code, 422)


class ConfirmDeclineTests(DealTestCase):
    """`POST /deals/{id}/confirm` and `/decline` — the customer's write."""

    def setUp(self):
        super().setUp()
        self.deal = Deal.objects.create(
            listing=self.listing,
            inquiry=self.inquiry,
            provider_organization=self.seller_org,
            customer_organization=self.buyer_org,
            deal_type=Deal.DealType.RENT,
            created_by=self.seller_owner,
        )

    def confirm(self, user=None, deal=None):
        target = deal or self.deal
        return self.client.post(
            f"/api/v1/deals/{target.public_id}/confirm", **auth(user or self.buyer_owner)
        )

    def decline(self, user=None, deal=None):
        target = deal or self.deal
        return self.client.post(
            f"/api/v1/deals/{target.public_id}/decline", **auth(user or self.buyer_owner)
        )

    def test_the_customer_can_confirm(self):
        response = self.confirm()
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertIsNotNone(payload["completed_at"])

    def test_the_provider_cannot_confirm_their_own_deal(self):
        """The whole point: confirming is the *other* side's act (§4)."""
        self.assertEqual(self.confirm(user=self.seller_owner).status_code, 404)

    def test_confirming_requires_the_deals_permission(self):
        self.assertEqual(self.confirm(user=self.buyer_member).status_code, 403)

    def test_confirming_a_second_time_is_a_409(self):
        first = self.confirm()
        self.assertEqual(first.status_code, 200)
        second = self.confirm()
        self.assertEqual(second.status_code, 409)

    def test_a_sale_confirmation_marks_the_listing_sold(self):
        sale_listing = self.make_listing(
            listing_type=Listing.ListingType.SALE,
            price_unit=Listing.PriceUnit.TOTAL,
            title="MTZ-82 for sale",
        )
        sale_inquiry = self.make_inquiry(listing=sale_listing)
        sale_deal = Deal.objects.create(
            listing=sale_listing,
            inquiry=sale_inquiry,
            provider_organization=self.seller_org,
            customer_organization=self.buyer_org,
            deal_type=Deal.DealType.SALE,
            created_by=self.seller_owner,
        )
        response = self.confirm(deal=sale_deal)
        self.assertEqual(response.status_code, 200, response.content)
        sale_listing.refresh_from_db()
        self.assertEqual(sale_listing.status, Listing.Status.SOLD)

        listing_entry = ActivityLog.objects.get(content_type__model="listing")
        self.assertEqual(listing_entry.organization, self.seller_org)
        self.assertEqual(listing_entry.action, ActivityLog.Action.STATUS_CHANGED)

    def test_a_rent_confirmation_does_not_touch_the_listing(self):
        self.confirm()
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, Listing.Status.ACTIVE)

    def test_confirming_is_logged_against_the_customer(self):
        self.confirm()
        entry = ActivityLog.objects.get(
            content_type__model="deal", action=ActivityLog.Action.STATUS_CHANGED
        )
        self.assertEqual(entry.organization, self.buyer_org)
        self.assertEqual(entry.actor, self.buyer_owner)

    def test_the_customer_can_decline(self):
        response = self.decline()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "declined")

    def test_the_provider_cannot_decline_their_own_deal(self):
        self.assertEqual(self.decline(user=self.seller_owner).status_code, 404)

    def test_declining_an_already_completed_deal_is_a_409(self):
        self.confirm()
        self.assertEqual(self.decline().status_code, 409)


class ReviewTests(DealTestCase):
    """`POST /deals/{id}/review` — the customer, once, within the window."""

    def setUp(self):
        super().setUp()
        self.deal = Deal.objects.create(
            listing=self.listing,
            inquiry=self.inquiry,
            provider_organization=self.seller_org,
            customer_organization=self.buyer_org,
            deal_type=Deal.DealType.RENT,
            created_by=self.seller_owner,
            status=Deal.Status.COMPLETED,
            completed_at=timezone.now(),
        )

    def review(self, user=None, deal=None, rating=5, comment="Good experience."):
        target = deal or self.deal
        return self.client.post(
            f"/api/v1/deals/{target.public_id}/review",
            data={"rating": rating, "comment": comment},
            content_type="application/json",
            **auth(user or self.buyer_owner),
        )

    def test_the_customer_can_review_a_completed_deal(self):
        response = self.review()
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()
        self.assertEqual(payload["rating"], 5)
        self.assertEqual(payload["comment"], "Good experience.")
        self.assertEqual(payload["deal_id"], str(self.deal.public_id))

    def test_the_provider_cannot_review_their_own_deal(self):
        self.assertEqual(self.review(user=self.seller_owner).status_code, 404)

    def test_reviewing_requires_the_deals_permission(self):
        self.assertEqual(self.review(user=self.buyer_member).status_code, 403)

    def test_a_pending_deal_cannot_be_reviewed(self):
        self.deal.status = Deal.Status.PENDING_CONFIRMATION
        self.deal.completed_at = None
        self.deal.save(update_fields=["status", "completed_at"])
        self.assertEqual(self.review().status_code, 409)

    def test_reviewing_twice_is_a_409(self):
        first = self.review()
        self.assertEqual(first.status_code, 201)
        second = self.review(rating=1, comment="Second attempt")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(Review.objects.filter(deal=self.deal).count(), 1)

    def test_reviewing_past_the_window_is_a_400(self):
        self.deal.completed_at = timezone.now() - timedelta(days=31)
        self.deal.save(update_fields=["completed_at"])
        self.assertEqual(self.review().status_code, 400)

    def test_an_out_of_range_rating_is_a_422(self):
        self.assertEqual(self.review(rating=6).status_code, 422)
        self.assertEqual(self.review(rating=0).status_code, 422)

    def test_reviewing_is_logged_against_the_customer(self):
        self.review()
        entry = ActivityLog.objects.get(content_type__model="review")
        self.assertEqual(entry.organization, self.buyer_org)
        self.assertEqual(entry.actor, self.buyer_owner)
        self.assertEqual(entry.action, ActivityLog.Action.CREATED)
