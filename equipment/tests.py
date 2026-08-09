from decimal import Decimal

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from users.models import Organization, Region, User

from .models import (
    Asset,
    Booking,
    BookingItem,
    EquipmentCategory,
    EquipmentModel,
    Manufacturer,
    PricingRule,
)


class ModelInvariantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="owner", password="x")
        cls.org = Organization.objects.create(name="Org", owner=cls.user)
        manufacturer = Manufacturer.objects.create(name="John Deere")
        category = EquipmentCategory.objects.create(name="Tractor")
        cls.equipment_model = EquipmentModel.objects.create(
            manufacturer=manufacturer, category=category, name="6155M"
        )
        cls.asset = Asset.objects.create(
            organization=cls.org, equipment_model=cls.equipment_model
        )

    # --- 1. is_bookable derives from NON_BOOKABLE_STATUSES ------------------

    def test_is_bookable_matches_non_bookable_statuses(self):
        for status in Asset.OperationalStatus.values:
            self.asset.operational_status = status
            expected = status not in Asset.NON_BOOKABLE_STATUSES
            self.assertEqual(self.asset.is_bookable, expected, status)

    # --- 2. line_total is Decimal, not float --------------------------------

    def test_line_total_is_exact_decimal(self):
        booking = self._booking()
        item = BookingItem(
            booking=booking,
            asset=self.asset,
            pricing_unit=BookingItem.PricingUnit.HOUR,
            unit_price=Decimal("0.10"),
            quantity=Decimal("3"),
        )
        self.assertIsInstance(item.line_total, Decimal)
        self.assertEqual(item.line_total, Decimal("0.30"))

    # --- 3. PricingRule target is enforced in the database ------------------

    def test_pricing_rule_without_target_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            PricingRule.objects.create(
                organization=self.org,
                pricing_unit=PricingRule.PricingUnit.DAY,
                price=Decimal("100.00"),
            )

    def test_pricing_rule_with_target_is_accepted(self):
        PricingRule.objects.create(
            organization=self.org,
            asset=self.asset,
            pricing_unit=PricingRule.PricingUnit.DAY,
            price=Decimal("100.00"),
        )
        self.assertEqual(self.org.pricing_rules.count(), 1)

    # --- 4. Booking status transitions are enforced -------------------------

    def _booking(self, **kwargs):
        now = timezone.now()
        return Booking.objects.create(
            customer_organization=self.org,
            provider_organization=self.org,
            start_at=now,
            end_at=now + timezone.timedelta(days=1),
            **kwargs,
        )

    def test_illegal_transition_on_plain_save_is_rejected(self):
        booking = self._booking()
        booking.status = Booking.Status.COMPLETED
        with self.assertRaises(ValidationError):
            booking.save()
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.DRAFT)

    def test_legal_transition_on_plain_save_is_allowed(self):
        booking = self._booking()
        booking.status = Booking.Status.REQUESTED
        booking.save()
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.REQUESTED)

    def test_booking_cannot_be_created_in_terminal_status(self):
        with self.assertRaises(ValidationError):
            self._booking(status=Booking.Status.COMPLETED)

    def test_transition_to_writes_history(self):
        booking = self._booking()
        entry = booking.transition_to(
            Booking.Status.REQUESTED, changed_by=self.user, notes="sent"
        )
        self.assertEqual(entry.from_status, Booking.Status.DRAFT)
        self.assertEqual(entry.to_status, Booking.Status.REQUESTED)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.REQUESTED)

    def test_transition_to_rejects_illegal_move(self):
        booking = self._booking()
        with self.assertRaises(ValidationError):
            booking.transition_to(Booking.Status.COMPLETED)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.DRAFT)

    def test_chained_transitions_after_save(self):
        booking = self._booking()
        booking.transition_to(Booking.Status.REQUESTED)
        booking.transition_to(Booking.Status.CONFIRMED)
        booking.transition_to(Booking.Status.IN_PROGRESS)
        booking.transition_to(Booking.Status.COMPLETED)
        self.assertEqual(booking.status_history.count(), 4)

    def test_deferred_status_load_does_not_block_saves(self):
        booking = self._booking()
        partial = Booking.objects.only("pk", "notes").get(pk=booking.pk)
        partial.notes = "touched"
        partial.save(update_fields=["notes"])
        booking.refresh_from_db()
        self.assertEqual(booking.notes, "touched")

    def test_can_transition_helper(self):
        self.assertTrue(
            Booking.can_transition(Booking.Status.DRAFT, Booking.Status.REQUESTED)
        )
        self.assertFalse(
            Booking.can_transition(Booking.Status.DRAFT, Booking.Status.COMPLETED)
        )
        self.assertTrue(
            Booking.can_transition(Booking.Status.DRAFT, Booking.Status.DRAFT)
        )


class AppConfigTests(TestCase):
    # --- 5. explicit BigAutoField ------------------------------------------

    def test_apps_declare_big_auto_field(self):
        from django.apps import apps

        for label in ("equipment", "farms", "core", "users", "api"):
            self.assertEqual(
                apps.get_app_config(label).default_auto_field,
                "django.db.models.BigAutoField",
                label,
            )


class PublicEndpointTests(TestCase):
    """
    /api/v1/public/* is the only part of the API meant to work without a
    token. These tests guard the two things that matter most: that it really
    doesn't need one, and that it never leaks the private fields AssetOut and
    OrganizationOut expose to authenticated org members.
    """

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="pub-owner", password="x")
        cls.region = Region.objects.create(name="Tashkent Region", code="TK")
        cls.org = Organization.objects.create(
            name="Public Org",
            owner=cls.owner,
            region=cls.region,
            phone="+998900000000",
            email="secret@example.com",
        )
        manufacturer = Manufacturer.objects.create(name="Case IH")
        category = EquipmentCategory.objects.create(name="Tractor", slug="tractor")
        cls.equipment_model = EquipmentModel.objects.create(
            manufacturer=manufacturer, category=category, name="Puma 150"
        )
        cls.available_asset = Asset.objects.create(
            organization=cls.org,
            equipment_model=cls.equipment_model,
            operational_status=Asset.OperationalStatus.AVAILABLE,
            serial_number="SN-SECRET-1",
            vin_or_pin="VIN-SECRET-1",
            notes="internal notes nobody outside the org should see",
        )
        cls.retired_asset = Asset.objects.create(
            organization=cls.org,
            equipment_model=cls.equipment_model,
            operational_status=Asset.OperationalStatus.RETIRED,
        )
        PricingRule.objects.create(
            organization=cls.org,
            asset=cls.available_asset,
            pricing_unit=PricingRule.PricingUnit.DAY,
            price=Decimal("250000.00"),
        )

    def setUp(self):
        # The stats endpoint caches its payload under a fixed key; without
        # this, whichever test runs first would poison the rest.
        cache.clear()

    def test_listings_require_no_token(self):
        response = self.client.get("/api/v1/public/listings")
        self.assertEqual(response.status_code, 200)

    def test_listings_only_show_available_assets(self):
        response = self.client.get("/api/v1/public/listings")
        ids = {item["id"] for item in response.json()["items"]}
        self.assertIn(str(self.available_asset.public_id), ids)
        self.assertNotIn(str(self.retired_asset.public_id), ids)

    def test_retired_asset_detail_404s(self):
        response = self.client.get(
            f"/api/v1/public/listings/{self.retired_asset.public_id}"
        )
        self.assertEqual(response.status_code, 404)

    def test_listing_detail_includes_price_and_provider(self):
        response = self.client.get(
            f"/api/v1/public/listings/{self.available_asset.public_id}"
        )
        data = response.json()
        self.assertEqual(data["price"]["amount"], 250000.0)
        self.assertEqual(data["provider"]["name"], "Public Org")
        self.assertEqual(data["provider"]["region"]["code"], "TK")
        self.assertFalse(data["provider"]["is_verified"])

    def test_model_level_price_does_not_leak_across_organizations(self):
        """
        equipment_model is shared catalog data, so its pricing_rules can hold
        rows from organizations other than the asset's owner. An asset with
        no asset-level price must fall back to its own org's model-level
        rule, never a cheaper rule some other org set on the same model.
        """
        other_owner = User.objects.create_user(username="other-owner", password="x")
        other_org = Organization.objects.create(name="Other Org", owner=other_owner)
        PricingRule.objects.create(
            organization=other_org,
            equipment_model=self.equipment_model,
            pricing_unit=PricingRule.PricingUnit.DAY,
            price=Decimal("1.00"),
        )
        PricingRule.objects.create(
            organization=self.org,
            equipment_model=self.equipment_model,
            pricing_unit=PricingRule.PricingUnit.DAY,
            price=Decimal("300000.00"),
        )
        unpriced_asset = Asset.objects.create(
            organization=self.org,
            equipment_model=self.equipment_model,
            operational_status=Asset.OperationalStatus.AVAILABLE,
        )

        response = self.client.get(f"/api/v1/public/listings/{unpriced_asset.public_id}")

        self.assertEqual(response.json()["price"]["amount"], 300000.0)

    def test_listing_detail_hides_private_fields(self):
        response = self.client.get(
            f"/api/v1/public/listings/{self.available_asset.public_id}"
        )
        body = response.content.decode()
        for leaked in (
            self.available_asset.serial_number,
            self.available_asset.vin_or_pin,
            "internal notes",
            self.org.phone,
            self.org.email,
        ):
            self.assertNotIn(leaked, body)

    def test_regions_endpoint_no_token(self):
        response = self.client.get("/api/v1/public/regions")
        self.assertEqual(response.status_code, 200)
        codes = {row["code"] for row in response.json()}
        self.assertIn(self.region.code, codes)

    def test_stats_shape(self):
        response = self.client.get("/api/v1/public/stats")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        expected_keys = {
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
        }
        self.assertEqual(set(data.keys()), expected_keys)
        # No review model exists yet — this must stay null, not a fabricated number.
        self.assertIsNone(data["average_rating"])
        self.assertGreaterEqual(data["total_active_listings"], 1)

    def test_stats_includes_regions_with_no_listings(self):
        empty_region = Region.objects.create(name="Empty Region", code="EM")
        response = self.client.get("/api/v1/public/stats")
        by_region = {
            row["code"]: row["listing_count"]
            for row in response.json()["listings_by_region"]
        }
        self.assertEqual(by_region.get(empty_region.code), 0)

    def test_org_scoped_assets_still_require_a_token(self):
        response = self.client.get("/api/v1/assets")
        self.assertEqual(response.status_code, 401)

    def test_catalog_is_now_public_too(self):
        response = self.client.get("/api/v1/catalog/manufacturers")
        self.assertEqual(response.status_code, 200)
