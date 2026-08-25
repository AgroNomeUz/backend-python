"""
listings/tests.py
The marketplace layer: the three invariants §0.5 asks the database to keep,
the public feed's scoping and filters, and the org-side writes.

The invariant tests matter more than usual here. Two of the three are the
reason `Listing` exists as its own model, and all three are enforced in
Postgres rather than in a serializer — so the way to prove they hold is to
try to write around them with the ORM, which is exactly what a bug in a view
would end up doing.
"""

import tempfile
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings

from api.auth import create_access_token
from core.models import ActivityLog
from equipment.models import Asset, EquipmentCategory, EquipmentModel, Manufacturer
from listings.models import Listing, ListingImage
from users.models import OrgPermission, Organization, Region, User

# A real, minimal PNG — the upload endpoint sniffs the leading bytes, so a
# fixture of arbitrary bytes named ".png" would (correctly) be refused.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def auth(user: User) -> dict:
    """Request kwargs carrying a bearer token for `user`."""
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}


class ListingTestCase(TestCase):
    """Two organizations, each with a machine — enough to test org scoping."""

    @classmethod
    def setUpClass(cls):
        # Uploads go to a throwaway directory: the real MEDIA_ROOT is inside
        # the project, and a test run must not leave files there.
        cls._media = tempfile.TemporaryDirectory()
        cls.enterClassContext(override_settings(MEDIA_ROOT=cls._media.name))
        cls.addClassCleanup(cls._media.cleanup)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(
            name="Andijan Test", code="ANT", soato="1703"
        )
        cls.other_region = Region.objects.create(
            name="Fergana Test", code="FAT", soato="1730"
        )

        cls.owner = User.objects.create_user(username="listing-owner", password="x")
        cls.org = Organization.objects.create(
            name="Listing Org", owner=cls.owner, region=cls.region, is_verified=True
        )
        cls.owner.organization = cls.org
        cls.owner.save(update_fields=["organization"])

        # A member with no permission codes — reads everything in the org,
        # writes nothing (§0.2).
        cls.member = User.objects.create_user(
            username="listing-member", password="x", organization=cls.org
        )

        cls.rival_owner = User.objects.create_user(username="rival-owner", password="x")
        cls.rival_org = Organization.objects.create(
            name="Rival Org", owner=cls.rival_owner, region=cls.other_region
        )
        cls.rival_owner.organization = cls.rival_org
        cls.rival_owner.save(update_fields=["organization"])

        manufacturer = Manufacturer.objects.create(name="MTZ")
        cls.category = EquipmentCategory.objects.create(name="Tractor", slug="tractor")
        cls.other_category = EquipmentCategory.objects.create(
            name="Combine", slug="combine"
        )
        cls.equipment_model = EquipmentModel.objects.create(
            manufacturer=manufacturer,
            category=cls.category,
            name="82.1",
            engine_power_kw=60,
            is_self_propelled=True,
        )
        cls.other_model = EquipmentModel.objects.create(
            manufacturer=manufacturer, category=cls.other_category, name="Combine 5"
        )

        cls.asset = Asset.objects.create(
            organization=cls.org,
            equipment_model=cls.equipment_model,
            manufacture_year=2019,
            serial_number="SN-PRIVATE",
        )
        cls.rival_asset = Asset.objects.create(
            organization=cls.rival_org, equipment_model=cls.equipment_model
        )

    @classmethod
    def make_listing(cls, **overrides):
        fields = {
            "organization": cls.org,
            "asset": cls.asset,
            "created_by": cls.owner,
            "region": cls.region,
            "listing_type": Listing.ListingType.RENT,
            "status": Listing.Status.ACTIVE,
            "title": "MTZ-82 tractor with operator",
            "price": Decimal("400000.00"),
            "price_unit": Listing.PriceUnit.DAY,
        }
        fields.update(overrides)
        return Listing.objects.create(**fields)


class InvariantTests(ListingTestCase):
    """
    The invariants, each checked where it is enforced: the database.

    Every one of these is written through the ORM rather than the API, so a
    view that forgot its guard could not make the test pass.
    """

    def test_price_cannot_be_negative(self):
        """
        `MinValueValidator(0)` on the field is not enough on its own — it only
        runs under `full_clean()`, which no write path calls.
        """
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_listing(price=Decimal("-1.00"))

    def test_a_free_listing_is_allowed(self):
        """The bound is `>= 0`, not `> 0`: zero is a legitimate asking price."""
        listing = self.make_listing(price=Decimal("0.00"))
        self.assertEqual(listing.price, Decimal("0.00"))

    def test_listing_cannot_point_at_another_orgs_asset(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_listing(asset=self.rival_asset)

    def test_only_one_active_listing_per_asset_and_type(self):
        self.make_listing()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_listing()

    def test_the_same_asset_may_be_rented_and_sold_at_once(self):
        """The point of the constraint being per `(asset, listing_type)`."""
        self.make_listing()
        self.make_listing(
            listing_type=Listing.ListingType.SALE,
            price_unit=Listing.PriceUnit.TOTAL,
        )
        self.assertEqual(self.asset.listings.filter(status="active").count(), 2)

    def test_a_draft_may_sit_beside_an_active_listing(self):
        """The constraint is partial — only actives collide."""
        self.make_listing()
        self.make_listing(status=Listing.Status.DRAFT)

    def test_rental_cannot_be_priced_as_a_total(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_listing(price_unit=Listing.PriceUnit.TOTAL)

    def test_sale_must_be_priced_as_a_total(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_listing(listing_type=Listing.ListingType.SALE)

    def test_a_listing_only_has_one_primary_image(self):
        listing = self.make_listing()
        ListingImage.objects.create(
            listing=listing,
            file=SimpleUploadedFile("a.png", PNG_BYTES, "image/png"),
            is_primary=True,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ListingImage.objects.create(
                listing=listing,
                file=SimpleUploadedFile("b.png", PNG_BYTES, "image/png"),
                is_primary=True,
            )


class PublicFeedTests(ListingTestCase):
    """
    `GET /listings` — the one endpoint an anonymous visitor hits hardest.

    Two things are load-bearing: the feed is the *intersection* of
    publication state and machine state (§0.5), and it must never carry a
    field the fleet register keeps private.
    """

    def test_feed_needs_no_token(self):
        self.assertEqual(self.client.get("/api/v1/listings").status_code, 200)

    def test_only_active_listings_appear(self):
        active = self.make_listing()
        draft = self.make_listing(status=Listing.Status.DRAFT)
        paused = self.make_listing(status=Listing.Status.PAUSED)
        archived = self.make_listing(status=Listing.Status.ARCHIVED)

        ids = self._ids()
        self.assertIn(str(active.public_id), ids)
        for hidden in (draft, paused, archived):
            self.assertNotIn(str(hidden.public_id), ids)

    def test_listing_drops_out_when_the_machine_is_unavailable(self):
        """
        The half a single-model design could not express: the offer is still
        `active`, but the tractor is in the workshop.
        """
        listing = self.make_listing()
        self.assertIn(str(listing.public_id), self._ids())

        self.asset.operational_status = Asset.OperationalStatus.UNDER_MAINTENANCE
        self.asset.save(update_fields=["operational_status"])
        self.assertNotIn(str(listing.public_id), self._ids())

    def test_feed_never_leaks_private_asset_fields(self):
        self.make_listing()
        body = self.client.get("/api/v1/listings").content.decode()
        self.assertNotIn("SN-PRIVATE", body)

    def test_specs_are_read_through_the_equipment_model(self):
        self.make_listing()
        item = self.client.get("/api/v1/listings").json()["items"][0]
        self.assertEqual(item["equipment_type"], "tractor")
        self.assertEqual(item["brand"], "MTZ")
        self.assertEqual(item["model"], "82.1")
        self.assertEqual(item["year"], 2019)
        self.assertEqual(item["owner"]["name"], "Listing Org")
        self.assertTrue(item["owner"]["is_verified"])
        # No review model exists yet — present and null, not absent (§4).
        self.assertIsNone(item["rating"])

    def test_price_filters_and_sort(self):
        cheap = self.make_listing(price=Decimal("100000"))
        dear = self.make_listing(
            listing_type=Listing.ListingType.SALE,
            price_unit=Listing.PriceUnit.TOTAL,
            price=Decimal("900000"),
        )

        self.assertEqual(self._ids(min_price=500000), {str(dear.public_id)})
        self.assertEqual(self._ids(max_price=500000), {str(cheap.public_id)})

        ascending = self.client.get("/api/v1/listings?sort=price_asc").json()["items"]
        self.assertEqual(
            [item["id"] for item in ascending],
            [str(cheap.public_id), str(dear.public_id)],
        )
        descending = self.client.get("/api/v1/listings?sort=price_desc").json()["items"]
        self.assertEqual(descending[0]["id"], str(dear.public_id))

    def test_tied_prices_sort_deterministically(self):
        """
        `order_by()` replaces the model's default ordering outright, so without
        an explicit tiebreaker equal prices come back in whatever order
        Postgres feels like — and this feed is paginated, which turns that into
        a row appearing on two pages or on neither.
        """
        for _ in range(3):
            asset = Asset.objects.create(
                organization=self.org, equipment_model=self.equipment_model
            )
            self.make_listing(asset=asset, price=Decimal("250000"))

        pages = [
            [
                item["id"]
                for item in self.client.get(
                    "/api/v1/listings?sort=price_asc"
                ).json()["items"]
            ]
            for _ in range(3)
        ]
        self.assertEqual(pages[0], pages[1])
        self.assertEqual(pages[1], pages[2])

    def test_unknown_sort_is_a_400(self):
        self.assertEqual(
            self.client.get("/api/v1/listings?sort=cheapest").status_code, 400
        )

    def test_rating_sort_is_accepted_while_no_reviews_exist(self):
        """Accepted, not rejected — the UI's sort control shouldn't 400."""
        self.make_listing()
        self.assertEqual(
            self.client.get("/api/v1/listings?sort=rating").status_code, 200
        )

    def test_offer_shaped_filters(self):
        with_operator = self.make_listing(has_operator=True, has_delivery=False)
        with_delivery = self.make_listing(
            listing_type=Listing.ListingType.SALE,
            price_unit=Listing.PriceUnit.TOTAL,
            has_operator=False,
            has_delivery=True,
        )
        self.assertEqual(self._ids(has_operator=True), {str(with_operator.public_id)})
        self.assertEqual(self._ids(has_delivery=True), {str(with_delivery.public_id)})
        self.assertEqual(self._ids(listing_type="sale"), {str(with_delivery.public_id)})

    def test_machine_shaped_filters_reach_through_the_asset(self):
        listing = self.make_listing()
        self.assertEqual(self._ids(equipment_type="tractor"), {str(listing.public_id)})
        # `category` is the spelling the asset endpoints use; both work.
        self.assertEqual(self._ids(category="tractor"), {str(listing.public_id)})
        self.assertEqual(self._ids(equipment_type="combine"), set())
        self.assertEqual(self._ids(min_power_kw=50), {str(listing.public_id)})
        self.assertEqual(self._ids(min_power_kw=100), set())
        self.assertEqual(self._ids(is_self_propelled=True), {str(listing.public_id)})

    def test_region_accepts_a_slug_or_a_code(self):
        listing = self.make_listing()
        self.assertEqual(self._ids(region="andijan-test"), {str(listing.public_id)})
        self.assertEqual(self._ids(region="ANT"), {str(listing.public_id)})
        self.assertEqual(self._ids(region="FAT"), set())

    def test_verified_only(self):
        mine = self.make_listing()
        theirs = Listing.objects.create(
            organization=self.rival_org,
            asset=self.rival_asset,
            listing_type=Listing.ListingType.RENT,
            status=Listing.Status.ACTIVE,
            title="Unverified offer",
            price=Decimal("1"),
            price_unit=Listing.PriceUnit.DAY,
        )
        ids = self._ids(verified_only=True)
        self.assertIn(str(mine.public_id), ids)
        self.assertNotIn(str(theirs.public_id), ids)

    def _ids(self, **params) -> set[str]:
        query = "&".join(f"{key}={value}" for key, value in params.items())
        response = self.client.get(f"/api/v1/listings?{query}")
        self.assertEqual(response.status_code, 200, response.content)
        return {item["id"] for item in response.json()["items"]}


class ListingDetailTests(ListingTestCase):
    """
    `GET /listings/{id}` is the one endpoint with *optional* auth: public for
    a published offer, wider for the organization that owns it.
    """

    def test_anonymous_sees_a_published_listing(self):
        listing = self.make_listing()
        response = self.client.get(f"/api/v1/listings/{listing.public_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], listing.title)

    def test_anonymous_cannot_see_a_draft(self):
        listing = self.make_listing(status=Listing.Status.DRAFT)
        self.assertEqual(
            self.client.get(f"/api/v1/listings/{listing.public_id}").status_code, 404
        )

    def test_the_owning_org_sees_its_own_draft(self):
        listing = self.make_listing(status=Listing.Status.DRAFT)
        response = self.client.get(
            f"/api/v1/listings/{listing.public_id}", **auth(self.member)
        )
        self.assertEqual(response.status_code, 200)

    def test_another_org_gets_404_not_403(self):
        """An id must not be probeable across organizations (§0.1)."""
        listing = self.make_listing(status=Listing.Status.DRAFT)
        response = self.client.get(
            f"/api/v1/listings/{listing.public_id}", **auth(self.rival_owner)
        )
        self.assertEqual(response.status_code, 404)

    def test_a_junk_token_reads_as_anonymous_rather_than_401(self):
        listing = self.make_listing()
        response = self.client.get(
            f"/api/v1/listings/{listing.public_id}",
            HTTP_AUTHORIZATION="Bearer not-a-token",
        )
        self.assertEqual(response.status_code, 200)


class ListingWriteTests(ListingTestCase):
    """
    The org-side CRUD: permission gates, org scoping, and an audit row per
    write (§0.3 — a write endpoint without one is incomplete).
    """

    def post_listing(self, user=None, **overrides):
        body = {
            "asset_id": str(self.asset.public_id),
            "listing_type": "rent",
            "title": "MTZ-82 with operator",
            "price": "400000.00",
            "price_unit": "day",
        }
        body.update(overrides)
        return self.client.post(
            "/api/v1/listings",
            data=body,
            content_type="application/json",
            **auth(user or self.owner),
        )

    def test_create_returns_201_and_audits(self):
        response = self.post_listing()
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()
        self.assertEqual(payload["status"], "draft")
        # Defaulted from the organization rather than supplied by the client.
        self.assertEqual(payload["region"]["code"], "ANT")
        self.assertEqual(payload["region"]["soato"], "1703")

        listing = Listing.objects.get(public_id=payload["id"])
        self.assertEqual(listing.created_by, self.owner)
        entry = ActivityLog.objects.get(
            organization=self.org, content_type__model="listing"
        )
        self.assertEqual(entry.action, ActivityLog.Action.CREATED)
        self.assertEqual(entry.actor, self.owner)

    def test_create_requires_the_equipment_permission(self):
        self.assertEqual(self.post_listing(user=self.member).status_code, 403)

    def test_a_member_with_the_code_may_create(self):
        self.member.permissions = [OrgPermission.MANAGE_EQUIPMENT]
        self.member.save(update_fields=["permissions"])
        self.assertEqual(self.post_listing(user=self.member).status_code, 201)

    def test_cannot_list_another_orgs_machine(self):
        response = self.post_listing(asset_id=str(self.rival_asset.public_id))
        self.assertEqual(response.status_code, 404)

    def test_creating_a_second_active_listing_is_a_409(self):
        self.make_listing()
        response = self.post_listing(status="active")
        self.assertEqual(response.status_code, 409)

    def test_publishing_at_creation_stamps_published_at(self):
        response = self.post_listing(status="active")
        self.assertEqual(response.status_code, 201)
        self.assertIsNotNone(response.json()["published_at"])

    def test_a_listing_cannot_be_born_paused(self):
        self.assertEqual(self.post_listing(status="paused").status_code, 400)

    def test_sale_priced_per_day_is_rejected_with_a_message(self):
        response = self.post_listing(listing_type="sale", price_unit="day")
        self.assertEqual(response.status_code, 400)
        self.assertIn("total", response.json()["detail"])

    def test_an_unknown_status_is_refused_by_the_schema(self):
        self.assertEqual(self.post_listing(status="on-fire").status_code, 422)

    def test_patch_records_a_field_diff(self):
        listing = self.make_listing(status=Listing.Status.DRAFT)
        response = self.client.patch(
            f"/api/v1/listings/{listing.public_id}",
            data={"title": "Renamed", "price": "500000.00"},
            content_type="application/json",
            **auth(self.owner),
        )
        self.assertEqual(response.status_code, 200, response.content)
        entry = ActivityLog.objects.filter(action=ActivityLog.Action.UPDATED).get()
        self.assertEqual(entry.changes["title"]["to"], "Renamed")

    def test_status_only_patch_is_logged_as_a_status_change(self):
        listing = self.make_listing(status=Listing.Status.DRAFT)
        self.client.patch(
            f"/api/v1/listings/{listing.public_id}",
            data={"status": "active"},
            content_type="application/json",
            **auth(self.owner),
        )
        entry = ActivityLog.objects.filter(
            action=ActivityLog.Action.STATUS_CHANGED
        ).get()
        self.assertEqual(entry.changes["status"]["to"], "active")
        listing.refresh_from_db()
        self.assertIsNotNone(listing.published_at)

    def test_published_at_survives_a_pause_and_reactivate(self):
        """It records when the offer first went to market, not last."""
        listing = self.make_listing()
        listing.published_at = None
        listing.save(update_fields=["published_at"])

        for status in ("paused", "active"):
            self.client.patch(
                f"/api/v1/listings/{listing.public_id}",
                data={"status": status},
                content_type="application/json",
                **auth(self.owner),
            )
        listing.refresh_from_db()
        first = listing.published_at
        self.assertIsNotNone(first)

        for status in ("paused", "active"):
            self.client.patch(
                f"/api/v1/listings/{listing.public_id}",
                data={"status": status},
                content_type="application/json",
                **auth(self.owner),
            )
        listing.refresh_from_db()
        self.assertEqual(listing.published_at, first)

    def test_patch_on_another_orgs_listing_is_404(self):
        listing = self.make_listing()
        response = self.client.patch(
            f"/api/v1/listings/{listing.public_id}",
            data={"title": "Hijacked"},
            content_type="application/json",
            **auth(self.rival_owner),
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_archives_rather_than_deleting(self):
        listing = self.make_listing()
        response = self.client.delete(
            f"/api/v1/listings/{listing.public_id}", **auth(self.owner)
        )
        self.assertEqual(response.status_code, 204)

        listing.refresh_from_db()
        self.assertEqual(listing.status, Listing.Status.ARCHIVED)
        self.assertTrue(
            ActivityLog.objects.filter(action=ActivityLog.Action.DELETED).exists()
        )

    def test_my_listings_shows_every_status(self):
        active = self.make_listing()
        draft = self.make_listing(status=Listing.Status.DRAFT)
        response = self.client.get("/api/v1/listings/my", **auth(self.member))
        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["items"]}
        self.assertEqual(ids, {str(active.public_id), str(draft.public_id)})

    def test_my_listings_filters_by_creator(self):
        mine = self.make_listing(created_by=self.owner)
        self.make_listing(status=Listing.Status.DRAFT, created_by=self.member)
        response = self.client.get(
            f"/api/v1/listings/my?created_by={self.owner.public_id}",
            **auth(self.owner),
        )
        ids = {item["id"] for item in response.json()["items"]}
        self.assertEqual(ids, {str(mine.public_id)})

    def test_my_listings_never_shows_another_orgs_work(self):
        self.make_listing()
        response = self.client.get("/api/v1/listings/my", **auth(self.rival_owner))
        self.assertEqual(response.json()["items"], [])


class ListingValidationTests(ListingTestCase):
    """
    The boundary between what the schema accepts and what the columns hold.

    Every case here used to be a 500: Django enforces neither `max_length` nor
    a field validator on `save()`, so an over-long string reached Postgres as
    a DataError — which isn't an IntegrityError and so isn't something
    `_integrity_error` can translate — and an explicitly-sent `null` reached a
    NOT NULL column. They are 4xx now, and the point of these tests is that
    none of them is a 5xx.
    """

    def patch(self, listing, **body):
        return self.client.patch(
            f"/api/v1/listings/{listing.public_id}",
            data=body,
            content_type="application/json",
            **auth(self.owner),
        )

    def post(self, **overrides):
        body = {
            "asset_id": str(self.asset.public_id),
            "listing_type": "rent",
            "title": "MTZ-82",
            "price": "400000.00",
            "price_unit": "day",
        }
        body.update(overrides)
        return self.client.post(
            "/api/v1/listings",
            data=body,
            content_type="application/json",
            **auth(self.owner),
        )

    # ── over-long strings ────────────────────────────────────────────────────

    def test_an_over_long_title_is_refused(self):
        self.assertEqual(self.post(title="x" * 256).status_code, 422)

    def test_a_currency_that_is_not_three_characters_is_refused(self):
        self.assertEqual(self.post(currency="NOT-A-CURRENCY").status_code, 422)
        self.assertEqual(self.post(currency="UZ").status_code, 422)

    def test_an_over_long_district_is_refused(self):
        self.assertEqual(self.post(district="d" * 121).status_code, 422)

    def test_an_over_long_availability_is_refused(self):
        self.assertEqual(self.post(availability="a" * 256).status_code, 422)

    def test_an_over_long_title_is_refused_on_patch_too(self):
        listing = self.make_listing(status=Listing.Status.DRAFT)
        self.assertEqual(self.patch(listing, title="x" * 256).status_code, 422)

    # ── negative prices ──────────────────────────────────────────────────────

    def test_a_negative_price_is_refused(self):
        self.assertEqual(self.post(price="-5000.00").status_code, 422)
        self.assertFalse(Listing.objects.filter(price__lt=0).exists())

    def test_a_negative_price_is_refused_on_patch_too(self):
        listing = self.make_listing(status=Listing.Status.DRAFT)
        self.assertEqual(self.patch(listing, price="-1.00").status_code, 422)
        listing.refresh_from_db()
        self.assertEqual(listing.price, Decimal("400000.00"))

    # ── explicit nulls ───────────────────────────────────────────────────────

    def test_an_explicit_null_on_a_required_field_is_a_400(self):
        listing = self.make_listing(status=Listing.Status.DRAFT)
        for field in ("title", "price", "price_unit", "currency", "status"):
            with self.subTest(field=field):
                response = self.patch(listing, **{field: None})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn(field, response.json()["detail"])

    def test_the_listing_is_untouched_after_a_null_is_refused(self):
        listing = self.make_listing(status=Listing.Status.DRAFT)
        self.patch(listing, title=None)
        listing.refresh_from_db()
        self.assertEqual(listing.title, "MTZ-82 tractor with operator")

    def test_a_null_region_is_allowed_because_the_column_is_nullable(self):
        """The one field whose `null` means "clear it" rather than a mistake."""
        listing = self.make_listing(status=Listing.Status.DRAFT)
        response = self.patch(listing, region_id=None)
        self.assertEqual(response.status_code, 200, response.content)
        listing.refresh_from_db()
        self.assertIsNone(listing.region)


@override_settings(LISTING_IMAGE_MAX_COUNT=2)
class ListingImageTests(ListingTestCase):
    """
    Uploads. The validation is three separate checks because each catches
    something the others miss — most importantly, the extension is a name the
    client chose, so the leading bytes are checked too.
    """

    def setUp(self):
        super().setUp()
        self.listing = self.make_listing()
        self.url = f"/api/v1/listings/{self.listing.public_id}/images"

    def upload(self, name="photo.png", content=PNG_BYTES, user=None):
        return self.client.post(
            self.url,
            data={"file": SimpleUploadedFile(name, content, "image/png")},
            **auth(user or self.owner),
        )

    def test_upload_returns_an_absolute_url_and_audits(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()
        self.assertTrue(payload["url"].startswith("http://"))
        self.assertTrue(payload["is_primary"], "the first photo is the card image")
        self.assertTrue(
            ActivityLog.objects.filter(
                organization=self.org, action=ActivityLog.Action.UPDATED
            ).exists()
        )

    def test_upload_requires_the_equipment_permission(self):
        self.assertEqual(self.upload(user=self.member).status_code, 403)

    def test_a_disallowed_extension_is_refused(self):
        self.assertEqual(self.upload(name="payload.svg").status_code, 400)

    def test_a_png_that_is_not_a_png_is_refused(self):
        """The extension is a claim, not evidence."""
        response = self.upload(name="photo.png", content=b"<svg>not an image</svg>")
        self.assertEqual(response.status_code, 400)

    @override_settings(LISTING_IMAGE_MAX_BYTES=10)
    def test_an_oversized_image_is_refused(self):
        self.assertEqual(self.upload().status_code, 400)

    def test_the_count_cap_holds(self):
        self.assertEqual(self.upload().status_code, 201)
        self.assertEqual(self.upload().status_code, 201)
        self.assertEqual(self.upload().status_code, 400)

    def test_images_appear_on_the_listing_payload(self):
        self.upload()
        item = self.client.get(f"/api/v1/listings/{self.listing.public_id}").json()
        self.assertEqual(len(item["images"]), 1)
        self.assertTrue(item["images"][0]["url"].startswith("http://"))

    def test_deleting_the_primary_promotes_the_next(self):
        first = self.upload().json()
        second = self.upload().json()
        self.assertTrue(first["is_primary"])
        self.assertFalse(second["is_primary"])

        response = self.client.delete(
            f"{self.url}/{first['id']}", **auth(self.owner)
        )
        self.assertEqual(response.status_code, 204)

        promoted = ListingImage.objects.get(public_id=second["id"])
        self.assertTrue(promoted.is_primary)
        self.assertEqual(self.listing.images.count(), 1)

    def test_deleting_an_image_removes_the_file_once_the_row_is_gone(self):
        """
        The file delete is queued with `transaction.on_commit`, not done
        inline: a filesystem delete cannot be rolled back, so doing it before
        the row is certainly gone risks a surviving ListingImage whose URL
        404s for good. `captureOnCommitCallbacks` runs what the commit would.
        """
        uploaded = self.upload().json()
        path = ListingImage.objects.get(public_id=uploaded["id"]).file.path
        self.assertTrue(Path(path).exists())

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(
                f"{self.url}/{uploaded['id']}", **auth(self.owner)
            )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Path(path).exists())

    def test_cannot_upload_to_another_orgs_listing(self):
        self.assertEqual(self.upload(user=self.rival_owner).status_code, 404)


class DeprecatedPublicPathTests(ListingTestCase):
    """
    `/public/listings` and `/public/regions` are frozen, not rewritten: they
    are what the deployed frontend parses. They keep their asset-rooted
    payloads, and `/listings` and `/regions` are the canonical replacements.
    """

    def test_public_listings_still_answers_from_the_asset_root(self):
        response = self.client.get("/api/v1/public/listings")
        self.assertEqual(response.status_code, 200)
        items = {item["id"]: item for item in response.json()["items"]}
        # Rooted on Asset, so the ids are asset ids — not listing ids.
        self.assertIn(str(self.asset.public_id), items)
        item = items[str(self.asset.public_id)]
        self.assertIn("equipment_model", item)
        self.assertIn("current_meter_hours", item)

    def test_public_regions_keeps_its_original_fields(self):
        response = self.client.get("/api/v1/public/regions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json()[0]), {"id", "name", "code", "listing_count"}
        )


@override_settings(LISTING_IMAGE_MAX_COUNT=3)
class ConcurrentUploadTests(TransactionTestCase):
    """
    Uploads arriving at once.

    Counting the existing images, checking them against the cap and inserting
    the new row are one critical section, serialised on the listing row. Read
    outside a transaction, overlapping uploads all see the same count: they
    slip past the cap together, claim the same `sort_order`, and — on a listing
    with no photos yet — all elect themselves primary, which the partial unique
    index turns into a 500.

    A `TransactionTestCase` because real threads need real commits: the usual
    `TestCase` wraps everything in one transaction that no other connection can
    see. The assertions are about invariants rather than about a particular
    interleaving, so nothing here depends on the race actually being hit.
    """

    def setUp(self):
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        override = override_settings(MEDIA_ROOT=self._media.name)
        override.enable()
        self.addCleanup(override.disable)

        region = Region.objects.create(name="Race Region", code="RC", soato="9999")
        owner = User.objects.create_user(username="race-owner", password="x")
        org = Organization.objects.create(name="Race Org", owner=owner, region=region)
        owner.organization = org
        owner.save(update_fields=["organization"])

        manufacturer = Manufacturer.objects.create(name="MTZ")
        category = EquipmentCategory.objects.create(name="Tractor", slug="tractor")
        model = EquipmentModel.objects.create(
            manufacturer=manufacturer, category=category, name="82.1"
        )
        asset = Asset.objects.create(organization=org, equipment_model=model)

        self.listing = Listing.objects.create(
            organization=org,
            asset=asset,
            region=region,
            listing_type=Listing.ListingType.RENT,
            status=Listing.Status.ACTIVE,
            title="Contended listing",
            price=Decimal("1"),
            price_unit=Listing.PriceUnit.DAY,
        )
        self.url = f"/api/v1/listings/{self.listing.public_id}/images"
        self.token = create_access_token(owner.public_id)

    def _upload(self, _n):
        try:
            return Client().post(
                self.url,
                data={"file": SimpleUploadedFile("p.png", PNG_BYTES, "image/png")},
                HTTP_AUTHORIZATION=f"Bearer {self.token}",
            ).status_code
        finally:
            # Each thread opens its own connection; leaving them behind makes
            # the post-test database teardown hang on the open handles.
            connection.close()

    def test_simultaneous_uploads_hold_every_image_invariant(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(self._upload, range(8)))

        images = ListingImage.objects.filter(listing=self.listing)
        sort_orders = list(images.values_list("sort_order", flat=True))

        self.assertNotIn(500, codes)
        self.assertLessEqual(images.count(), 3, "the cap was exceeded")
        self.assertEqual(images.filter(is_primary=True).count(), 1)
        self.assertEqual(
            len(set(sort_orders)), len(sort_orders), "duplicate sort_order"
        )
