from django.core.exceptions import ValidationError
from django.db import models

from core.models import PublicIdModel
from users.models import Organization
from django.contrib.gis.db import models as gis_models


# =============================================================================
# 3. FARMS & FIELDS
# =============================================================================


class Farm(PublicIdModel):
    """A farm owned or managed by a customer user."""

    owner = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="farms"
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

    def __str__(self) -> str:
        return self.name


class Field(PublicIdModel):
    """A single arable parcel belonging to a farm."""

    farm = models.ForeignKey(Farm, on_delete=models.CASCADE, related_name="fields")
    name = models.CharField(max_length=255)
    boundary = gis_models.MultiPolygonField(
        null=True,
        blank=True,
        srid=4326,
        help_text="Field boundary as WGS84 multipolygon",
    )
    area_ha = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Area in hectares (can be computed from boundary)",
    )
    soil_type = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["farm", "name"]
        unique_together = [("farm", "name")]

    def __str__(self) -> str:
        return f"{self.farm.name} / {self.name}"

    def clean(self) -> None:
        if self.area_ha is not None and self.area_ha <= 0:
            raise ValidationError({"area_ha": "Field area must be positive."})


class CropSeason(PublicIdModel):
    """Crop planted on a field during a season year."""

    field = models.ForeignKey(Field, on_delete=models.CASCADE, related_name="crop_seasons")
    crop_type = models.CharField(max_length=100)
    season_year = models.PositiveSmallIntegerField()
    planting_date = models.DateField(null=True, blank=True)
    expected_harvest_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-season_year", "field"]

    def __str__(self) -> str:
        return f"{self.field} — {self.crop_type} ({self.season_year})"

