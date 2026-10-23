from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import InternalError, connection, models
from django.core.exceptions import ValidationError

from core.models import PublicIdModel
from users.models import Organization

from .units import AREA_UNIT_CHOICES, HECTARE, MAX_AREA_HECTARES, validate_area_unit


# =============================================================================
# 3. FARMS & FIELDS
# =============================================================================
#
# ADAPT-aligned: Farm > Field > FieldBoundary / CropZone, with Season as the
# time scope. Quantities are a value column plus a UCUM `<name>_unit` column.
#
# Every org-owned table here carries `organization` and a `(id, organization)`
# unique constraint so its children can reference the pair with a composite
# foreign key. Those foreign keys are added in the migration with RunSQL, like
# `inquiry_listing_same_org_fk` in inquiries/0001: they keep a denormalised
# `organization` honest in the one place a view cannot forget.


def geometry_area_hectares(geometry) -> Decimal:
    """
    Area of a WGS84 geometry in hectares, measured on the spheroid.

    Done with PostGIS `ST_Area(geography)`, not `geometry.area`: that one is in
    square degrees, which means nothing for a field.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ST_Area(ST_GeomFromEWKT(%s)::geography)",
                [f"SRID=4326;{geometry.wkt}"],
            )
            square_metres = Decimal(str(cursor.fetchone()[0]))
    except InternalError:
        # PostGIS cannot measure e.g. an edge spanning 180 degrees of longitude.
        raise ValidationError("The geometry's area cannot be measured.")
    hectares = (square_metres / Decimal("10000")).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    if hectares >= MAX_AREA_HECTARES:
        raise ValidationError("The geometry's area is too large.")
    return hectares


class FarmCluster(PublicIdModel):
    """A named group of farms inside one organization."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="farm_clusters"
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="farmcluster_org_name_unique",
            ),
            # Lets Farm reference (cluster, organization) as a composite FK.
            models.UniqueConstraint(
                fields=["id", "organization"],
                name="farmcluster_id_org_unique",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Farm(PublicIdModel):
    """A farm owned or managed by a customer organization."""

    # Named `organization` like every other org-owned model — `owner` read as
    # a User FK at a glance, which it never was.
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="farms"
    )
    # Composite FK (cluster_id, organization_id) -> FarmCluster(id,
    # organization_id) in the migration: a farm cannot join another org's
    # cluster. SET_NULL on the plain FK is mirrored by the API, which clears
    # the column before deleting a cluster (a composite FK cannot SET NULL
    # just one of its two columns).
    cluster = models.ForeignKey(
        FarmCluster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="farms",
    )
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=512, blank=True)
    region = models.ForeignKey(
        "users.Region",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="farms",
    )
    location = gis_models.PointField(
        null=True,
        blank=True,
        srid=4326,
        help_text="Approximate centre point of the farm (WGS84)",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="farm_org_name_unique",
            ),
            models.UniqueConstraint(
                fields=["id", "organization"],
                name="farm_id_org_unique",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Field(PublicIdModel):
    """A single arable parcel belonging to a farm."""

    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="fields")
    # Copy of `farm.organization`, so org-scoped queries are one filter. The
    # composite FK (farm_id, organization_id) -> Farm(id, organization_id)
    # in the migration keeps the copy in step.
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="fields"
    )
    name = models.CharField(max_length=255)
    soato = models.CharField(max_length=20, blank=True)
    area = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    area_unit = models.CharField(
        max_length=8,
        choices=AREA_UNIT_CHOICES,
        default=HECTARE,
        validators=[validate_area_unit],
        help_text="UCUM code of `area`",
    )
    soil_type = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["farm", "name"]
        constraints = [
            models.UniqueConstraint(fields=["farm", "name"], name="field_farm_name_unique"),
            models.UniqueConstraint(
                fields=["id", "organization"], name="field_id_org_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(area__isnull=True) | models.Q(area__gt=0),
                name="field_area_positive",
                violation_error_message="Field area must be positive.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.farm.name} / {self.name}"

    def clean(self) -> None:
        if self.area is not None and self.area <= 0:
            raise ValidationError({"area": "Field area must be positive."})
        if self.farm_id and self.organization_id != self.farm.organization_id:
            raise ValidationError(
                {"farm": "The farm belongs to a different organization."}
            )


class FieldBoundary(PublicIdModel):
    """
    One boundary of a field over a range of dates (ADAPT Boundary).

    History is append-only through the API: a new boundary closes the open one
    (`valid_to`) and inserts another. At most one boundary per field is open.
    """

    class Source(models.TextChoices):
        DRAWN = "drawn", "Drawn"
        GPS = "gps", "GPS"
        IMPORTED = "imported", "Imported"

    field = models.ForeignKey(
        Field, on_delete=models.CASCADE, related_name="boundaries"
    )
    geometry = gis_models.MultiPolygonField(srid=4326)
    # Computed from `geometry` in save(), in hectares.
    area = models.DecimalField(max_digits=14, decimal_places=4, editable=False)
    area_unit = models.CharField(
        max_length=8,
        choices=AREA_UNIT_CHOICES,
        default=HECTARE,
        validators=[validate_area_unit],
        editable=False,
    )
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.DRAWN
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-valid_from", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_to__gte=models.F("valid_from")),
                name="fieldboundary_valid_range",
                violation_error_message="valid_to cannot be before valid_from.",
            ),
            models.UniqueConstraint(
                fields=["field"],
                condition=models.Q(valid_to__isnull=True),
                name="fieldboundary_one_open_per_field",
                violation_error_message="This field already has an open boundary.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.field} boundary from {self.valid_from}"

    def save(self, *args, **kwargs):
        self.area = geometry_area_hectares(self.geometry)
        self.area_unit = HECTARE
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = {*update_fields, "area", "area_unit"}
        super().save(*args, **kwargs)


class Season(PublicIdModel):
    """A named time scope for crop zones, e.g. "2026"."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="seasons"
    )
    name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ["-start_date", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="season_org_name_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="season_end_after_start",
                violation_error_message="end_date cannot be before start_date.",
            ),
            models.UniqueConstraint(
                fields=["id", "organization"], name="season_id_org_unique"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class CropZone(PublicIdModel):
    """A crop on a field in a season (ADAPT CropZone)."""

    field = models.ForeignKey(
        Field, on_delete=models.CASCADE, related_name="crop_zones"
    )
    # RESTRICT, not PROTECT: a season that still has zones cannot be deleted
    # (API: 409, catch RestrictedError), but deleting the whole organization
    # may still cascade through it, which PROTECT would refuse.
    season = models.ForeignKey(
        Season, on_delete=models.RESTRICT, related_name="crop_zones"
    )
    # Composite FKs (field_id, organization_id) -> Field and (season_id,
    # organization_id) -> Season in the migration: a zone can never mix a
    # field and a season of different organizations.
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="crop_zones"
    )
    crop_code = models.CharField(
        max_length=10, db_index=True, help_text="EPPO code, uppercase"
    )
    crop_name = models.CharField(max_length=255, help_text="Local display label")
    geometry = gis_models.MultiPolygonField(null=True, blank=True, srid=4326)
    area = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    area_unit = models.CharField(
        max_length=8,
        choices=AREA_UNIT_CHOICES,
        default=HECTARE,
        validators=[validate_area_unit],
    )
    planting_date = models.DateField(null=True, blank=True)
    expected_harvest_date = models.DateField(null=True, blank=True)
    harvest_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-season__start_date", "field", "crop_code", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(planting_date__isnull=True)
                | models.Q(expected_harvest_date__isnull=True)
                | models.Q(expected_harvest_date__gte=models.F("planting_date")),
                name="cropzone_harvest_after_planting",
                violation_error_message="expected_harvest_date cannot be before planting_date.",
            ),
            models.CheckConstraint(
                condition=models.Q(area__isnull=True) | models.Q(area__gt=0),
                name="cropzone_area_positive",
                violation_error_message="Crop zone area must be positive.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.field} — {self.crop_name} ({self.season})"

    def clean(self) -> None:
        if self.field_id and self.season_id:
            if self.field.organization_id != self.season.organization_id:
                raise ValidationError(
                    {"season": "The season belongs to a different organization."}
                )
