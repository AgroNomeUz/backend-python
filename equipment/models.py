"""
equipment/models.py
Agricultural equipment rental platform — MVP model layer.

Domain split:
  1. Catalog       — Manufacturer, EquipmentCategory, EquipmentModel, EquipmentModelCompatibility
  2. Assets        — Asset (physical machine)
  3. Farms/Fields  — Farm, Field, CropSeason
  4. Pricing       — AvailabilityPeriod, PricingRule, DepositRule
  5. Bookings      — Booking, BookingItem, BookingStatusHistory
  6. Work          — WorkOrder, WorkSession
  7. Maintenance   — MaintenanceRecord, Inspection, FaultReport
  8. Documents     — Document (generic FK attachment)
  9. Events/Ext    — AssetEvent, ExternalReference

Internal units: kW, ha, mm, kg, L, km, UTC, WGS84 (SRID 4326).
"""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.gis.db import models as gis_models
from django.core.exceptions import ValidationError
from django.db import models, transaction

from core.models import PublicIdModel
from farms.models import Farm, Field
from users.models import Organization

# =============================================================================
# 1. CATALOG — Shared reference data, not per-organization
# =============================================================================


class Manufacturer(PublicIdModel):
    """Equipment brand: John Deere, CLAAS, Case IH, New Holland, etc."""

    name = models.CharField(max_length=100, unique=True)
    country = models.CharField(max_length=60, blank=True)
    website = models.URLField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class EquipmentCategory(PublicIdModel):
    """
    Hierarchical category tree (self-referencing).
    Top-level nodes: "Vehicle", "Implement".
    Leaves: "Tractor", "Plough", "Combine Harvester", etc.
    """

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100, unique=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="subcategories",
    )
    is_self_propelled = models.BooleanField(
        default=False,
        help_text="True for tractors, combines, sprayers; False for towed/mounted implements.",
    )

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Equipment Categories"

    def __str__(self) -> str:
        return self.name


class HitchCategory(models.TextChoices):
    """Three-point hitch categories (shared by models and compatibility rules)."""

    CAT_1 = "cat1", "Category 1"
    CAT_2 = "cat2", "Category 2"
    CAT_3 = "cat3", "Category 3"
    CAT_4 = "cat4", "Category 4"
    DRAWBAR = "drawbar", "Drawbar"


class EquipmentModel(PublicIdModel):
    """
    Make/model specification — 'John Deere 8R 410'.
    Describes what a class of machines looks like; not a physical unit.

    Common searchable specs are explicit columns (filtered in queries).
    Everything else goes into `specifications` (JSON, display-only).
    """

    class FuelType(models.TextChoices):
        DIESEL = "diesel", "Diesel"
        PETROL = "petrol", "Petrol"
        ELECTRIC = "electric", "Electric"
        HYBRID = "hybrid", "Hybrid"
        NA = "na", "N/A (implement)"

    manufacturer = models.ForeignKey(
        Manufacturer, on_delete=models.PROTECT, related_name="equipment_models"
    )
    category = models.ForeignKey(
        EquipmentCategory, on_delete=models.PROTECT, related_name="equipment_models"
    )
    name = models.CharField(max_length=120, help_text="Model name, e.g. '8R 410'")

    # Production range
    year_from = models.PositiveSmallIntegerField(null=True, blank=True)
    year_to = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Null = still in production"
    )

    # Explicit, filterable specs (use these in WHERE clauses)
    engine_power_kw = models.DecimalField(
        max_digits=7,
        decimal_places=1,
        null=True,
        blank=True,
        help_text="Engine power in kilowatts",
    )
    weight_kg = models.PositiveIntegerField(null=True, blank=True)
    length_mm = models.PositiveIntegerField(null=True, blank=True)
    width_mm = models.PositiveIntegerField(null=True, blank=True)
    height_mm = models.PositiveIntegerField(null=True, blank=True)
    working_width_mm = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Working width for seeders, sprayers, cultivators, etc.",
    )
    fuel_type = models.CharField(
        max_length=10, choices=FuelType.choices, default=FuelType.DIESEL
    )
    is_self_propelled = models.BooleanField(default=False)
    hitch_category = models.CharField(
        max_length=10,
        choices=HitchCategory.choices,
        blank=True,
        help_text=(
            "Hitch this machine PROVIDES (tractors). Blank for implements — "
            "what an implement REQUIRES lives in EquipmentModelCompatibility."
        ),
    )

    # Category-specific, display-only attributes
    specifications = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Category-specific attributes. "
            "E.g. {'furrows': 5} for a plough, "
            "{'rows': 12, 'row_spacing_cm': 70} for a seeder."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("manufacturer", "name")]
        ordering = ["manufacturer__name", "name"]
        indexes = [
            models.Index(fields=["category", "is_self_propelled"]),
            models.Index(fields=["engine_power_kw"]),
        ]

    def __str__(self) -> str:
        return f"{self.manufacturer.name} {self.name}"


class EquipmentModelCompatibility(PublicIdModel):
    """
    Coupling requirements between an implement and a power unit.

    Example: "Lemken Diamant 12 (5-furrow plough) requires:
      - ≥ 150 kW tractor
      - Category 3 hitch
      - 540 RPM PTO
      - ≥ 80 L/min hydraulic flow"

    primary_model  = the implement / attachment that declares requirements.
    compatible_with = specific tractor model that meets them (null = any).
    """

    class CompatibilityType(models.TextChoices):
        TRACTOR_IMPLEMENT = "tractor_implement", "Tractor → Implement"
        COMBINE_HEADER = "combine_header", "Combine → Header"
        OTHER = "other", "Other"

    primary_model = models.ForeignKey(
        EquipmentModel,
        on_delete=models.CASCADE,
        related_name="compatibility_as_implement",
        help_text="The implement / attachment that has requirements",
    )
    compatible_with = models.ForeignKey(
        EquipmentModel,
        on_delete=models.CASCADE,
        related_name="compatibility_as_tractor",
        null=True,
        blank=True,
        help_text="Specific tractor model; null = requirements apply to any tractor",
    )
    compatibility_type = models.CharField(
        max_length=30, choices=CompatibilityType.choices
    )

    # Power window
    min_power_kw = models.DecimalField(
        max_digits=7, decimal_places=1, null=True, blank=True
    )
    max_power_kw = models.DecimalField(
        max_digits=7, decimal_places=1, null=True, blank=True
    )

    # Mechanical interface
    hitch_category = models.CharField(
        max_length=10, choices=HitchCategory.choices, blank=True
    )
    pto_speed_rpm = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Required PTO speed: 540 or 1000"
    )
    hydraulic_flow_l_min = models.DecimalField(
        max_digits=6, decimal_places=1, null=True, blank=True
    )
    isobus_required = models.BooleanField(default=False)

    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = "Equipment Model Compatibility"
        verbose_name_plural = "Equipment Model Compatibilities"

    def __str__(self) -> str:
        return f"{self.primary_model} ← {self.compatible_with or 'any tractor'}"


# =============================================================================
# 2. ASSETS — Physical machines owned by organizations
# =============================================================================


class Asset(PublicIdModel):
    """
    A real, physical machine or implement owned by an Organization.

    Separating Asset from EquipmentModel lets multiple identical tractors
    share one spec entry while each having its own serial number, hours, status.
    """

    class OwnershipStatus(models.TextChoices):
        OWNED = "owned", "Owned"
        LEASED = "leased", "Leased"
        FINANCED = "financed", "Financed"

    class OperationalStatus(models.TextChoices):
        AVAILABLE = "available", "Available"
        RESERVED = "reserved", "Reserved"
        RENTED = "rented", "Rented"
        IN_TRANSIT = "in_transit", "In Transit"
        UNDER_MAINTENANCE = "under_maintenance", "Under Maintenance"
        OUT_OF_SERVICE = "out_of_service", "Out of Service"
        RETIRED = "retired", "Retired"

    # Statuses that block new bookings
    NON_BOOKABLE_STATUSES = frozenset(
        [
            OperationalStatus.UNDER_MAINTENANCE,
            OperationalStatus.OUT_OF_SERVICE,
            OperationalStatus.RETIRED,
            OperationalStatus.RENTED,
            OperationalStatus.RESERVED,
        ]
    )

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="assets"
    )
    equipment_model = models.ForeignKey(
        EquipmentModel, on_delete=models.PROTECT, related_name="assets"
    )

    # Physical identification
    serial_number = models.CharField(max_length=64, blank=True, db_index=True)
    vin_or_pin = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text="VIN (vehicles) or PIN (Product Identification Number)",
    )
    internal_inventory_number = models.CharField(max_length=64, blank=True)
    registration_number = models.CharField(max_length=64, blank=True)
    manufacture_year = models.PositiveSmallIntegerField(null=True, blank=True)

    # Status
    ownership_status = models.CharField(
        max_length=20,
        choices=OwnershipStatus.choices,
        default=OwnershipStatus.OWNED,
    )
    operational_status = models.CharField(
        max_length=20,
        choices=OperationalStatus.choices,
        default=OperationalStatus.AVAILABLE,
        db_index=True,
    )

    # Current state — updated on events, not real-time
    current_meter_hours = models.DecimalField(
        max_digits=9,
        decimal_places=1,
        default=0,
        help_text="Engine / meter hours at last update",
    )
    current_location = gis_models.PointField(
        null=True,
        blank=True,
        srid=4326,
        help_text="Last known location (WGS84). Updated on lifecycle events, not live.",
    )

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["organization", "equipment_model__name"]
        indexes = [
            models.Index(fields=["operational_status"]),
            models.Index(fields=["organization", "operational_status"]),
        ]

    def __str__(self) -> str:
        label = (
            self.serial_number
            or self.internal_inventory_number
            or str(self.pk)
        )
        return f"{self.equipment_model} / {label}"

    @property
    def is_bookable(self) -> bool:
        """Derived from NON_BOOKABLE_STATUSES so the two never diverge."""
        return self.operational_status not in self.NON_BOOKABLE_STATUSES

# =============================================================================
# 4. PRICING & AVAILABILITY
# =============================================================================


class AvailabilityPeriod(PublicIdModel):
    """
    Explicit availability / block windows for an asset.
    Confirmed bookings create BLOCKED entries (via the service layer).
    Query overlap: starts_at < requested_end AND ends_at > requested_start.
    """

    class PeriodType(models.TextChoices):
        AVAILABLE = "available", "Available"
        BLOCKED = "blocked", "Blocked (manual)"
        MAINTENANCE = "maintenance", "Maintenance"

    asset = models.ForeignKey(
        Asset, on_delete=models.CASCADE, related_name="availability_periods"
    )
    period_type = models.CharField(
        max_length=20,
        choices=PeriodType.choices,
        default=PeriodType.AVAILABLE,
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["starts_at"]
        indexes = [
            models.Index(fields=["asset", "starts_at", "ends_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.asset} [{self.period_type}] {self.starts_at}–{self.ends_at}"

    def clean(self) -> None:
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "ends_at must be after starts_at."})


class PricingRule(PublicIdModel):
    """
    Flexible pricing attached to an asset or an equipment model.
    Asset-level rules take precedence over model-level rules.
    At least one of `asset` or `equipment_model` must be set.
    """

    class PricingUnit(models.TextChoices):
        HOUR = "hour", "Per Hour"
        DAY = "day", "Per Day"
        HECTARE = "hectare", "Per Hectare"
        SHIFT = "shift", "Per Shift"
        OPERATION = "operation", "Per Operation"
        KM = "km", "Per Kilometre"

    asset = models.ForeignKey(
        Asset,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pricing_rules",
    )
    equipment_model = models.ForeignKey(
        EquipmentModel,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pricing_rules",
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="pricing_rules"
    )

    pricing_unit = models.CharField(max_length=20, choices=PricingUnit.choices)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="UZS")
    minimum_quantity = models.DecimalField(
        max_digits=8, decimal_places=2, default=1
    )

    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)

    includes_operator = models.BooleanField(default=False)
    includes_fuel = models.BooleanField(default=False)

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["organization", "pricing_unit"]
        indexes = [
            models.Index(fields=["asset", "pricing_unit"]),
            models.Index(fields=["equipment_model", "pricing_unit"]),
        ]
        constraints = [
            # Enforced in the database as well, so a plain objects.create()
            # cannot insert an orphaned rule that targets nothing.
            models.CheckConstraint(
                condition=models.Q(asset__isnull=False)
                | models.Q(equipment_model__isnull=False),
                name="pricingrule_has_target",
                violation_error_message=(
                    "A pricing rule must target either an asset or an equipment model."
                ),
            ),
        ]

    def __str__(self) -> str:
        target = self.asset or self.equipment_model or "org-wide"
        return f"{target} — {self.get_pricing_unit_display()} {self.price} {self.currency}"

    def clean(self) -> None:
        if not self.asset and not self.equipment_model:
            raise ValidationError(
                "A pricing rule must target either an asset or an equipment model."
            )


class DepositRule(PublicIdModel):
    """Deposit amount required when booking an asset."""

    asset = models.ForeignKey(
        Asset,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="deposit_rules",
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="deposit_rules"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="UZS")
    is_refundable = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"Deposit {self.amount} {self.currency}"


# =============================================================================
# 5. BOOKINGS
# =============================================================================


class Booking(PublicIdModel):
    """
    The commercial rental agreement between a customer and an organization.
    Operational detail lives in WorkOrder (child records).
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        REQUESTED = "requested", "Requested"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        DISPUTED = "disputed", "Disputed"

    # Enforced by the model itself: `clean()` validates it, `save()` rejects an
    # illegal change, and `transition_to()` is the helper callers should use.
    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.DRAFT: {Status.REQUESTED, Status.CANCELLED},
        Status.REQUESTED: {Status.CONFIRMED, Status.REJECTED, Status.CANCELLED},
        Status.CONFIRMED: {Status.IN_PROGRESS, Status.CANCELLED},
        Status.IN_PROGRESS: {Status.COMPLETED, Status.DISPUTED},
        Status.DISPUTED: {Status.COMPLETED, Status.CANCELLED},
        Status.REJECTED: set(),
        Status.CANCELLED: set(),
        Status.COMPLETED: set(),
    }
    customer_organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="bookings_as_customer",
    )
    provider_organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="bookings_as_provider"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bookings_created",
    )
    farm = models.ForeignKey(
        Farm,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bookings",
    )

    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )

    currency = models.CharField(max_length=3, default="UZS")
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    delivery_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    operator_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    deposit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "start_at"]),
            models.Index(fields=["customer_organization", "status"]),
            models.Index(fields=["provider_organization", "status"]),
        ]

    def __str__(self) -> str:
        return (
            f"Booking #{self.pk} {self.status} "
            f"({self.start_at.date()}–{self.end_at.date()})"
        )

    # ---- status transitions -------------------------------------------------

    #: Statuses a brand-new booking may be created in.
    INITIAL_STATUSES: frozenset[str] = frozenset({Status.DRAFT, Status.REQUESTED})

    @classmethod
    def from_db(cls, db, field_names, values):
        """Remember the persisted status so save() can validate the change."""
        instance = super().from_db(db, field_names, values)
        instance._loaded_status = (
            instance.status if "status" in field_names else models.DEFERRED
        )
        return instance

    @classmethod
    def can_transition(cls, from_status: str, to_status: str) -> bool:
        """True if `from_status` → `to_status` is a legal move (no-ops allowed)."""
        if from_status == to_status:
            return True
        return to_status in cls.ALLOWED_TRANSITIONS.get(from_status, set())

    def _validate_status_transition(self) -> None:
        previous = getattr(self, "_loaded_status", None)
        if previous is models.DEFERRED:
            # `status` was deferred on load; there is nothing to compare against.
            return
        if previous is None:
            # Unsaved instance — only the documented entry points are valid.
            if self.status not in self.INITIAL_STATUSES:
                raise ValidationError(
                    {
                        "status": (
                            f"A new booking cannot start in '{self.status}'. "
                            f"Allowed: {sorted(self.INITIAL_STATUSES)}."
                        )
                    }
                )
            return
        if not self.can_transition(previous, self.status):
            allowed = sorted(self.ALLOWED_TRANSITIONS.get(previous, set()))
            raise ValidationError(
                {
                    "status": (
                        f"Illegal status transition '{previous}' → '{self.status}'. "
                        f"Allowed from '{previous}': {allowed or 'none (terminal)'}."
                    )
                }
            )

    def clean(self) -> None:
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValidationError({"end_at": "end_at must be after start_at."})
        self._validate_status_transition()

    def save(self, *args, **kwargs):
        """
        Guard the status machine on every write, not just on full_clean(), so
        `booking.status = "completed"; booking.save()` cannot skip the rules.
        """
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "status" in set(update_fields):
            self._validate_status_transition()
        super().save(*args, **kwargs)
        self._loaded_status = self.status

    def transition_to(
        self,
        new_status: str,
        *,
        changed_by=None,
        notes: str = "",
    ) -> "BookingStatusHistory":
        """
        Central entry point for status changes: validates the move, persists it
        and appends the audit row. Callers should use this instead of assigning
        `status` by hand.
        """
        previous = getattr(self, "_loaded_status", self.status)
        if previous is models.DEFERRED:
            previous = type(self).objects.values_list("status", flat=True).get(pk=self.pk)
        if not self.can_transition(previous, new_status):
            allowed = sorted(self.ALLOWED_TRANSITIONS.get(previous, set()))
            raise ValidationError(
                {
                    "status": (
                        f"Illegal status transition '{previous}' → '{new_status}'. "
                        f"Allowed from '{previous}': {allowed or 'none (terminal)'}."
                    )
                }
            )
        with transaction.atomic():
            self.status = new_status
            self.save(update_fields=["status", "updated_at"])
            return BookingStatusHistory.objects.create(
                booking=self,
                from_status=previous,
                to_status=new_status,
                changed_by=changed_by,
                notes=notes,
            )


class BookingItem(PublicIdModel):
    """One asset line within a booking."""

    class PricingUnit(models.TextChoices):
        HOUR = "hour", "Per Hour"
        DAY = "day", "Per Day"
        HECTARE = "hectare", "Per Hectare"
        SHIFT = "shift", "Per Shift"
        OPERATION = "operation", "Per Operation"
        KM = "km", "Per Kilometre"

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="items")
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="booking_items")

    pricing_unit = models.CharField(max_length=20, choices=PricingUnit.choices)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)

    meter_start = models.DecimalField(
        max_digits=9,
        decimal_places=1,
        null=True,
        blank=True,
        help_text="Meter hours at rental start",
    )
    meter_end = models.DecimalField(
        max_digits=9,
        decimal_places=1,
        null=True,
        blank=True,
        help_text="Meter hours at rental end",
    )

    notes = models.TextField(blank=True)

    @property
    def line_total(self) -> Decimal:
        """
        Money — stays Decimal end to end. Rounded to the same 2 places the
        monetary columns use, so summing lines matches Booking.subtotal exactly.
        """
        return (self.unit_price * self.quantity).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    def __str__(self) -> str:
        return f"{self.asset} × {self.quantity} {self.pricing_unit}"


class BookingStatusHistory(PublicIdModel):
    """Immutable audit log — every booking status transition is recorded here."""

    booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name="status_history"
    )
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_status_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["changed_at"]

    def __str__(self) -> str:
        return (
            f"Booking #{self.booking_id}: {self.from_status} → {self.to_status}"
        )


# =============================================================================
# 6. WORK ORDERS & SESSIONS
# =============================================================================


class WorkOrder(PublicIdModel):
    """
    Operational task derived from a booking.
    Booking = 'rent this tractor for 3 days'.
    WorkOrder = 'plough Field A (36 ha) at 25 cm depth starting Monday 08:00'.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ASSIGNED = "assigned", "Assigned"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class OperationType(models.TextChoices):
        PLOUGHING = "ploughing", "Ploughing"
        SEEDING = "seeding", "Seeding"
        SPRAYING = "spraying", "Spraying"
        HARVESTING = "harvesting", "Harvesting"
        CULTIVATING = "cultivating", "Cultivating"
        BALING = "baling", "Baling"
        TRANSPORT = "transport", "Transport"
        FERTILIZING = "fertilizing", "Fertilizing"
        OTHER = "other", "Other"

    booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name="work_orders"
    )
    farm = models.ForeignKey(Farm, on_delete=models.PROTECT, related_name="work_orders")
    field = models.ForeignKey(
        Field,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_orders",
    )

    operation_type = models.CharField(max_length=30, choices=OperationType.choices)
    planned_start_at = models.DateTimeField()
    planned_end_at = models.DateTimeField()
    planned_area_ha = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Planned area in hectares",
    )
    required_depth_cm = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        help_text="Required working depth in cm (ploughing, cultivating)",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    instructions = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["planned_start_at"]
        indexes = [
            models.Index(fields=["booking", "status"]),
            models.Index(fields=["farm", "planned_start_at"]),
        ]

    def __str__(self) -> str:
        return f"WO#{self.pk} {self.operation_type} @ {self.farm}"

    def clean(self) -> None:
        if self.planned_area_ha is not None and self.planned_area_ha <= 0:
            raise ValidationError({"planned_area_ha": "Planned area must be positive."})


class WorkSession(PublicIdModel):
    """
    A single continuous operating period by one asset + operator on a work order.
    Tracks GPS, hours, area covered, fuel.
    `source` indicates how data arrived (manual entry vs telemetry).
    """

    class DataSource(models.TextChoices):
        MANUAL = "manual", "Manual Entry"
        MOBILE_APP = "mobile_app", "Mobile App"
        GPS_TRACKER = "gps_tracker", "GPS Tracker"
        ISOBUS = "isobus", "ISOBUS"
        MANUFACTURER_API = "manufacturer_api", "Manufacturer API"

    work_order = models.ForeignKey(
        WorkOrder, on_delete=models.CASCADE, related_name="sessions"
    )
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="work_sessions")
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_sessions",
    )

    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    start_meter_hours = models.DecimalField(
        max_digits=9, decimal_places=1, null=True, blank=True
    )
    end_meter_hours = models.DecimalField(
        max_digits=9, decimal_places=1, null=True, blank=True
    )

    area_completed_ha = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Hectares completed in this session",
    )
    fuel_used_l = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Fuel consumed in litres",
    )
    distance_km = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Distance travelled in kilometres",
    )

    track_geometry = gis_models.MultiLineStringField(
        null=True,
        blank=True,
        srid=4326,
        help_text="GPS track recorded during this session (WGS84)",
    )

    source = models.CharField(
        max_length=20,
        choices=DataSource.choices,
        default=DataSource.MANUAL,
    )
    external_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text="ID in the source system (e.g. ISOBUS task file ID)",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["started_at"]
        indexes = [
            models.Index(fields=["work_order", "started_at"]),
            models.Index(fields=["asset", "started_at"]),
        ]

    def __str__(self) -> str:
        return f"Session #{self.pk} — {self.asset} on {self.started_at.date()}"

    def clean(self) -> None:
        if self.area_completed_ha is not None and self.area_completed_ha < 0:
            raise ValidationError(
                {"area_completed_ha": "Completed area cannot be negative."}
            )


# =============================================================================
# 7. MAINTENANCE & INSPECTIONS
# =============================================================================


class MaintenanceRecord(PublicIdModel):
    """Service / repair log entry for an asset."""

    class MaintenanceType(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled Service"
        REPAIR = "repair", "Repair"
        EMERGENCY = "emergency", "Emergency Repair"
        INSPECTION = "inspection", "Inspection"
        OTHER = "other", "Other"

    asset = models.ForeignKey(
        Asset, on_delete=models.CASCADE, related_name="maintenance_records"
    )
    maintenance_type = models.CharField(
        max_length=20, choices=MaintenanceType.choices
    )

    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    meter_hours_at_service = models.DecimalField(
        max_digits=9, decimal_places=1, null=True, blank=True
    )

    cost = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    currency = models.CharField(max_length=3, default="UZS")

    description = models.TextField(blank=True)
    performed_by = models.CharField(
        max_length=255,
        blank=True,
        help_text="Technician name or service centre",
    )
    next_service_hours = models.DecimalField(
        max_digits=9,
        decimal_places=1,
        null=True,
        blank=True,
        help_text="Schedule next service at this meter reading",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return (
            f"{self.get_maintenance_type_display()} — "
            f"{self.asset} on {self.started_at.date()}"
        )


class Inspection(PublicIdModel):
    """Pre-rental, post-rental, or routine equipment inspection."""

    class InspectionType(models.TextChoices):
        PRE_RENTAL = "pre_rental", "Pre-Rental"
        POST_RENTAL = "post_rental", "Post-Rental"
        ROUTINE = "routine", "Routine"

    class ConditionStatus(models.TextChoices):
        GOOD = "good", "Good"
        FAIR = "fair", "Fair"
        POOR = "poor", "Poor"
        DAMAGED = "damaged", "Damaged"

    asset = models.ForeignKey(
        Asset, on_delete=models.CASCADE, related_name="inspections"
    )
    booking = models.ForeignKey(
        Booking,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspections",
    )
    inspection_type = models.CharField(
        max_length=20, choices=InspectionType.choices
    )
    inspected_at = models.DateTimeField()
    inspector = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspections_performed",
    )
    condition_status = models.CharField(
        max_length=20, choices=ConditionStatus.choices
    )
    checklist = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Structured checklist, e.g. "
            "{'tyres': 'ok', 'lights': 'damaged', 'hydraulics': 'ok'}"
        ),
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-inspected_at"]

    def __str__(self) -> str:
        return (
            f"{self.get_inspection_type_display()} — "
            f"{self.asset} on {self.inspected_at.date()}"
        )


class FaultReport(PublicIdModel):
    """Equipment fault or damage report logged by an operator or inspector."""

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_REVIEW = "in_review", "In Review"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    asset = models.ForeignKey(
        Asset, on_delete=models.CASCADE, related_name="fault_reports"
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fault_reports",
    )
    reported_at = models.DateTimeField(auto_now_add=True)
    severity = models.CharField(max_length=10, choices=Severity.choices)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    description = models.TextField()
    resolution_notes = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-reported_at"]

    def __str__(self) -> str:
        return f"Fault #{self.pk} [{self.severity}] — {self.asset}"


# =============================================================================
# 8. DOCUMENTS — Generic file attachments
# =============================================================================


class Document(PublicIdModel):
    """
    File attachment that can be linked to any model via generic FK.
    Used for contracts, registrations, insurance, inspection photos,
    maintenance docs, operator licences, etc.
    """

    class DocumentType(models.TextChoices):
        RENTAL_CONTRACT = "rental_contract", "Rental Contract"
        EQUIPMENT_REGISTRATION = "equipment_registration", "Equipment Registration"
        INSURANCE = "insurance", "Insurance"
        INSPECTION_PHOTO = "inspection_photo", "Inspection Photo"
        MAINTENANCE_DOC = "maintenance_doc", "Maintenance Document"
        OPERATOR_LICENSE = "operator_license", "Operator License"
        OTHER = "other", "Other"

    organization = models.ForeignKey(
        "users.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="documents",
    )

    # Attach to any model: Asset, Booking, MaintenanceRecord, Inspection, …
    content_type = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    related_object = GenericForeignKey("content_type", "object_id")

    file = models.FileField(upload_to="documents/%Y/%m/")
    document_type = models.CharField(max_length=30, choices=DocumentType.choices)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_documents",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_document_type_display()} ({self.uploaded_at.date()})"


# =============================================================================
# 9. EVENTS & EXTERNAL REFERENCES
# =============================================================================


class AssetEvent(PublicIdModel):
    """
    Lightweight lifecycle event log for significant asset transitions.
    This is NOT for high-frequency GPS/sensor data — use a time-series
    store (TimescaleDB, partitioned table) for that when required.
    """

    class EventType(models.TextChoices):
        CHECKED_OUT = "checked_out", "Checked Out"
        DELIVERED = "delivered", "Delivered"
        WORK_STARTED = "work_started", "Work Started"
        WORK_COMPLETED = "work_completed", "Work Completed"
        FUEL_ADDED = "fuel_added", "Fuel Added"
        FAULT_DETECTED = "fault_detected", "Fault Detected"
        MAINTENANCE_STARTED = "maintenance_started", "Maintenance Started"
        MAINTENANCE_COMPLETED = "maintenance_completed", "Maintenance Completed"
        RETURNED = "returned", "Returned"

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(
        max_length=30, choices=EventType.choices, db_index=True
    )
    occurred_at = models.DateTimeField(db_index=True)
    location = gis_models.PointField(null=True, blank=True, srid=4326)
    booking = models.ForeignKey(
        Booking,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    work_order = models.ForeignKey(
        WorkOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Event-specific data (meter hours, fuel quantity, etc.)",
    )

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["asset", "occurred_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.asset} — {self.event_type} @ {self.occurred_at}"


class ExternalReference(PublicIdModel):
    """
    Maps any internal object to an ID in an external system.

    Keeps the core data model clean and decoupled from vendor standards.
    When integrating John Deere / CLAAS / ADAPT / ISOBUS, create rows here
    instead of polluting core models with vendor-specific fields.
    """

    class ExternalSystem(models.TextChoices):
        JOHN_DEERE = "john_deere", "John Deere Operations Center"
        CLAAS = "claas", "CLAAS"
        CNH = "cnh", "CNH / Case IH / New Holland"
        ADAPT = "adapt", "ADAPT"
        ISOBUS = "isobus", "ISOBUS / ISOXML"
        GPS_TRACKER = "gps_tracker", "GPS Tracker"
        OTHER = "other", "Other"

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()

    external_system = models.CharField(
        max_length=30, choices=ExternalSystem.choices
    )
    external_id = models.CharField(max_length=255)
    external_version = models.CharField(max_length=64, blank=True)
    raw_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Last raw payload received from the external system",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [
            ("content_type", "object_id", "external_system", "external_id")
        ]
        indexes = [
            models.Index(fields=["external_system", "external_id"]),
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.external_system}:{self.external_id}"
