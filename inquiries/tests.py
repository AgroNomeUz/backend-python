"""
inquiries/tests.py
Two organizations, one conversation, and the line between them.

Most of what can go wrong here is a scoping mistake: an inquiry has two owners
and four ways to look at it, and every one of them has to show exactly one
side. So the tests are built around a pair of organizations that each own a
listing, and they check the negative case — what the *other* org sees — as
often as the positive one.

The invariant tests write through the ORM rather than the API, for the reason
listings/tests.py does: a view that forgot its guard could not make them pass.
"""

from datetime import date

from django.db import IntegrityError, transaction
from django.test import TestCase

from api.auth import create_access_token
from core.models import ActivityLog
from equipment.models import Asset, EquipmentCategory, EquipmentModel, Manufacturer
from inquiries.models import Inquiry
from listings.models import Listing
from users.models import OrgPermission, Organization, Region, User


def auth(user: User) -> dict:
    """Request kwargs carrying a bearer token for `user`."""
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}


class InquiryTestCase(TestCase):
    """
    A seller with a tractor on the market, and a buyer who also happens to
    list machines — because "can I enquire about my own listing?" is only a
    real question when both sides own something.
    """

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(
            name="Andijan Inquiry", code="ANI", soato="1703"
        )

        cls.seller_owner = User.objects.create_user(
            username="seller-owner", password="x", full_name="Seller Owner"
        )
        cls.seller_org = Organization.objects.create(
            name="Seller Org",
            owner=cls.seller_owner,
            region=cls.region,
            is_verified=True,
        )
        cls.seller_owner.organization = cls.seller_org
        cls.seller_owner.save(update_fields=["organization"])
        # No permission codes: reads everything in the org, writes nothing.
        cls.seller_member = User.objects.create_user(
            username="seller-member",
            password="x",
            organization=cls.seller_org,
            full_name="Seller Member",
        )

        cls.buyer_owner = User.objects.create_user(
            username="buyer-owner",
            password="x",
            full_name="Buyer Owner",
            phone="+998901112233",
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

        cls.seller_asset = Asset.objects.create(
            organization=cls.seller_org, equipment_model=equipment_model
        )
        cls.buyer_asset = Asset.objects.create(
            organization=cls.buyer_org, equipment_model=equipment_model
        )

        cls.listing = cls.make_listing(cls.seller_org, cls.seller_asset)
        # The buyer's own offer, for the self-inquiry case.
        cls.own_listing = cls.make_listing(cls.buyer_org, cls.buyer_asset)

    @classmethod
    def make_listing(cls, organization, asset, **overrides):
        fields = {
            "organization": organization,
            "asset": asset,
            "created_by": organization.owner,
            "region": cls.region,
            "listing_type": Listing.ListingType.RENT,
            "status": Listing.Status.ACTIVE,
            "title": "MTZ-82 tractor with operator",
            "price": "400000.00",
            "price_unit": Listing.PriceUnit.DAY,
        }
        fields.update(overrides)
        return Listing.objects.create(**fields)

    @classmethod
    def make_inquiry(cls, **overrides):
        fields = {
            "listing": cls.listing,
            "provider_organization": cls.seller_org,
            "customer_organization": cls.buyer_org,
            "created_by": cls.buyer_owner,
            "message": "Is it free the first week of April?",
        }
        fields.update(overrides)
        return Inquiry.objects.create(**fields)


class InvariantTests(InquiryTestCase):
    """The three rules the database keeps, checked where they are kept."""

    def test_an_organization_cannot_enquire_of_itself(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_inquiry(
                listing=self.own_listing,
                provider_organization=self.buyer_org,
                customer_organization=self.buyer_org,
            )

    def test_the_provider_must_be_the_listings_owner(self):
        """
        The composite foreign key. `provider_organization` is a copy of
        `listing.organization`, and nothing but this stops the copy drifting.
        """
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_inquiry(
                # The seller's listing, attributed to the buyer as provider.
                provider_organization=self.buyer_org,
                customer_organization=self.seller_org,
            )

    def test_the_end_date_cannot_precede_the_start(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_inquiry(
                start_date=date(2026, 4, 10), end_date=date(2026, 4, 1)
            )

    def test_a_single_day_range_is_allowed(self):
        """The bound is `>=`, not `>`: renting for one day is a real request."""
        inquiry = self.make_inquiry(
            start_date=date(2026, 4, 1), end_date=date(2026, 4, 1)
        )
        self.assertEqual(inquiry.start_date, inquiry.end_date)

    def test_a_one_sided_range_is_allowed(self):
        """"From the 3rd onwards" and "before the 10th" are both real asks."""
        self.make_inquiry(start_date=date(2026, 4, 3))
        self.make_inquiry(end_date=date(2026, 4, 10))

    def test_dates_are_optional_entirely(self):
        """A sale enquiry has no date range at all."""
        self.assertIsNone(self.make_inquiry().start_date)


class SendTests(InquiryTestCase):
    """`POST /inquiries` — the one write a renter organization makes."""

    def post_inquiry(self, user=None, **overrides):
        body = {
            "listing_id": str(self.listing.public_id),
            "message": "Is it free the first week of April?",
        }
        body.update(overrides)
        return self.client.post(
            "/api/v1/inquiries",
            data=body,
            content_type="application/json",
            **auth(user or self.buyer_owner),
        )

    def test_send_returns_201_with_both_halves_of_the_sender(self):
        response = self.post_inquiry(
            start_date="2026-04-01", end_date="2026-04-05"
        )
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()

        self.assertEqual(payload["listing_id"], str(self.listing.public_id))
        self.assertEqual(payload["listing"]["title"], self.listing.title)
        self.assertEqual(payload["listing"]["owner"]["name"], "Seller Org")
        # Organization *and* the acting member (§0.1) — either alone is
        # useless to whoever has to answer it.
        self.assertEqual(payload["renter"]["organization"]["name"], "Buyer Org")
        self.assertEqual(payload["renter"]["user"]["name"], "Buyer Owner")
        self.assertEqual(payload["renter"]["user"]["phone"], "+998901112233")
        self.assertFalse(payload["read"])
        self.assertIsNone(payload["read_at"])

    def test_a_sent_inquiry_never_shows_who_handled_it(self):
        """`handled_by` names a member of the other organization."""
        self.assertNotIn("handled_by", self.post_inquiry().json())

    def test_send_is_attributed_to_the_senders_organization(self):
        """
        §0.3, from the sending side: the audit row belongs to the org that
        acted, not to the one that was contacted.
        """
        self.post_inquiry()
        entry = ActivityLog.objects.get(content_type__model="inquiry")
        self.assertEqual(entry.organization, self.buyer_org)
        self.assertEqual(entry.actor, self.buyer_owner)
        self.assertEqual(entry.action, ActivityLog.Action.CREATED)
        # The message body is deliberately not copied into the audit row.
        self.assertNotIn("message", entry.changes)

    def test_send_requires_the_inquiries_permission(self):
        self.assertEqual(self.post_inquiry(user=self.buyer_member).status_code, 403)

    def test_a_member_with_the_code_may_send(self):
        self.buyer_member.permissions = [OrgPermission.MANAGE_INQUIRIES]
        self.buyer_member.save(update_fields=["permissions"])
        self.assertEqual(self.post_inquiry(user=self.buyer_member).status_code, 201)

    def test_cannot_enquire_of_your_own_organization(self):
        response = self.post_inquiry(listing_id=str(self.own_listing.public_id))
        self.assertEqual(response.status_code, 400)
        self.assertIn("own organization", response.json()["detail"])

    def test_an_unpublished_listing_is_a_404(self):
        """
        Not on the market, so not askable about — and answered exactly as a
        stranger's id is, so the two cases are indistinguishable (§0.1).
        """
        for status in (
            Listing.Status.DRAFT,
            Listing.Status.PAUSED,
            Listing.Status.ARCHIVED,
        ):
            with self.subTest(status=status):
                self.listing.status = status
                self.listing.save(update_fields=["status"])
                self.assertEqual(self.post_inquiry().status_code, 404)
        self.listing.status = Listing.Status.ACTIVE
        self.listing.save(update_fields=["status"])

    def test_a_machine_in_for_repair_cannot_be_asked_about(self):
        """The feed is an intersection (§0.5), and so is this."""
        self.seller_asset.operational_status = Asset.OperationalStatus.UNDER_MAINTENANCE
        self.seller_asset.save(update_fields=["operational_status"])
        self.assertEqual(self.post_inquiry().status_code, 404)

    def test_an_unknown_listing_is_a_404(self):
        response = self.post_inquiry(
            listing_id="00000000-0000-4000-8000-000000000000"
        )
        self.assertEqual(response.status_code, 404)

    def test_a_backwards_date_range_is_a_400_not_a_500(self):
        response = self.post_inquiry(
            start_date="2026-04-10", end_date="2026-04-01"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("end date", response.json()["detail"])

    def test_an_empty_message_is_refused_by_the_schema(self):
        self.assertEqual(self.post_inquiry(message="").status_code, 422)

    def test_an_over_long_message_is_refused_by_the_schema(self):
        self.assertEqual(self.post_inquiry(message="x" * 2001).status_code, 422)


class FolderTests(InquiryTestCase):
    """
    `/inquiries/received` and `/inquiries/sent` — the same rows from the two
    ends, each showing exactly one of them.
    """

    def setUp(self):
        self.inquiry = self.make_inquiry()

    def get(self, path, user, **params):
        return self.client.get(f"/api/v1/inquiries/{path}", params, **auth(user))

    def test_the_inbox_holds_what_was_asked_of_us(self):
        payload = self.get("received", self.seller_owner).json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["id"], str(self.inquiry.public_id))

    def test_the_inbox_is_readable_by_any_member(self):
        """Reads are never permission-gated (§0.2)."""
        self.assertEqual(self.get("received", self.seller_member).status_code, 200)

    def test_the_sender_does_not_see_it_in_their_own_inbox(self):
        self.assertEqual(self.get("received", self.buyer_owner).json()["count"], 0)

    def test_the_sent_folder_holds_what_we_asked(self):
        payload = self.get("sent", self.buyer_owner).json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["id"], str(self.inquiry.public_id))

    def test_the_provider_does_not_see_it_in_their_sent_folder(self):
        self.assertEqual(self.get("sent", self.seller_owner).json()["count"], 0)

    def test_the_inbox_names_who_handled_it_and_the_sent_folder_does_not(self):
        self.inquiry.read_at = "2026-04-01T10:00:00Z"
        self.inquiry.read_by = self.seller_member
        self.inquiry.save(update_fields=["read_at", "read_by"])

        inbox = self.get("received", self.seller_owner).json()["items"][0]
        self.assertEqual(inbox["handled_by"]["name"], "Seller Member")
        sent = self.get("sent", self.buyer_owner).json()["items"][0]
        self.assertTrue(sent["read"])
        self.assertNotIn("handled_by", sent)

    def test_the_unread_filter_is_the_unanswered_queue(self):
        self.make_inquiry(
            message="And the week after?",
            read_at="2026-04-01T10:00:00Z",
            read_by=self.seller_owner,
        )
        self.assertEqual(
            self.get("received", self.seller_owner, read="false").json()["count"], 1
        )
        self.assertEqual(
            self.get("received", self.seller_owner, read="true").json()["count"], 1
        )

    def test_handled_by_filters_the_inbox(self):
        self.make_inquiry(
            message="And the week after?",
            read_at="2026-04-01T10:00:00Z",
            read_by=self.seller_member,
        )
        payload = self.get(
            "received", self.seller_owner, handled_by=str(self.seller_member.public_id)
        ).json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["handled_by"]["name"], "Seller Member")

    def test_created_by_filters_the_sent_folder(self):
        """"What have I asked?", in an organization where several people ask."""
        self.make_inquiry(message="Mine", created_by=self.buyer_member)
        payload = self.get(
            "sent", self.buyer_owner, created_by=str(self.buyer_member.public_id)
        ).json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["message"], "Mine")

    def test_listing_id_filters_both_folders(self):
        other_listing = self.make_listing(
            self.seller_org,
            self.seller_asset,
            listing_type=Listing.ListingType.SALE,
            price_unit=Listing.PriceUnit.TOTAL,
            title="MTZ-82 for sale",
        )
        self.make_inquiry(listing=other_listing, message="What is the mileage?")

        for path, user in (("received", self.seller_owner), ("sent", self.buyer_owner)):
            with self.subTest(path=path):
                payload = self.get(
                    path, user, listing_id=str(other_listing.public_id)
                ).json()
                self.assertEqual(payload["count"], 1)
                self.assertEqual(payload["items"][0]["message"], "What is the mileage?")


class MarkReadTests(InquiryTestCase):
    """
    `POST /inquiries/{id}/read` — "read" with a name against it, which is the
    only version of it that means anything in a shared inbox.
    """

    def setUp(self):
        self.inquiry = self.make_inquiry()

    def mark_read(self, user=None, inquiry=None):
        target = inquiry or self.inquiry
        return self.client.post(
            f"/api/v1/inquiries/{target.public_id}/read",
            **auth(user or self.seller_owner),
        )

    def test_marking_read_records_the_reader(self):
        response = self.mark_read()
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertTrue(payload["read"])
        self.assertIsNotNone(payload["read_at"])
        self.assertEqual(payload["handled_by"]["name"], "Seller Owner")

        self.inquiry.refresh_from_db()
        self.assertEqual(self.inquiry.read_by, self.seller_owner)

    def test_marking_read_is_logged_as_a_status_change(self):
        """A read changes state; it does not edit the message."""
        self.mark_read()
        entry = ActivityLog.objects.get(content_type__model="inquiry")
        self.assertEqual(entry.organization, self.seller_org)
        self.assertEqual(entry.actor, self.seller_owner)
        self.assertEqual(entry.action, ActivityLog.Action.STATUS_CHANGED)

    def test_the_first_reader_keeps_their_name_on_it(self):
        """
        Idempotent, and deliberately not last-writer-wins: whoever picked the
        message up is the fact worth keeping, and it would be erased by the
        next person to open the inbox.
        """
        self.mark_read()
        self.inquiry.refresh_from_db()
        first_read_at = self.inquiry.read_at

        self.seller_member.permissions = [OrgPermission.MANAGE_INQUIRIES]
        self.seller_member.save(update_fields=["permissions"])
        response = self.mark_read(user=self.seller_member)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["handled_by"]["name"], "Seller Owner")
        self.inquiry.refresh_from_db()
        self.assertEqual(self.inquiry.read_by, self.seller_owner)
        self.assertEqual(self.inquiry.read_at, first_read_at)
        # And no second audit row for a no-op.
        self.assertEqual(ActivityLog.objects.filter(action="status_changed").count(), 1)

    def test_marking_read_requires_the_inquiries_permission(self):
        self.assertEqual(self.mark_read(user=self.seller_member).status_code, 403)

    def test_the_sender_cannot_mark_their_own_inquiry_read(self):
        """
        404 rather than 403: `/read` is an inbox action, and the sender's own
        inquiry is simply not in their inbox (§0.1).
        """
        self.assertEqual(self.mark_read(user=self.buyer_owner).status_code, 404)
