from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from users.models import Organization, User

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
