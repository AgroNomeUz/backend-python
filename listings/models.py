"""
listings/models.py
The marketplace layer: an *offer* to rent or sell a machine.

A `Listing` is deliberately not an extended `Asset`. An asset is a machine the
organization owns and tracks (serial number, meter hours, maintenance); a
listing is one way that machine is currently being offered. Keeping them apart
settles a collision the single-model design could not express:

    Asset.operational_status   machine state       available / rented / under_maintenance / …
    Listing.status             publication state   draft / active / paused / archived

The public feed is the intersection of the two — `status=active` **and**
`asset.operational_status=available` — so a tractor going in for repair stops
being rentable without having to look "unpublished", and one asset can carry
several offers (for rent *and* for sale) or be relisted after archiving.

Everything spec-like — brand, model, year, power, category — is read through
`asset.equipment_model` and never copied. The one thing that does live here is
`price`: the advertised price is a property of the offer, it has to be an
indexed column for `min_price`/`max_price`/`sort=price_asc` to work, and a
price on an org-owned row cannot leak another organization's pricing the way
resolving `PricingRule` at read time could.

Org-owned per §0.1 (`organization` is denormalised off the asset so scoping is
one filter), attributed per §0.3 (`created_by`).
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models

from core.models import PublicIdModel
from equipment.models import Asset
from users.models import Organization, Region

# Extensions accepted on an upload. Enforced again by content sniffing in the
# endpoint — the extension is whatever the client chose to call the file.
IMAGE_EXTENSIONS = ["jpg", "jpeg", "png", "webp"]


class Listing(PublicIdModel):
    """One organization's offer to rent out or sell one of its machines."""

    class ListingType(models.TextChoices):
        RENT = "rent", "For rent"
        SALE = "sale", "For sale"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        ARCHIVED = "archived", "Archived"

    class PriceUnit(models.TextChoices):
        HOUR = "hour", "Per hour"
        DAY = "day", "Per day"
        HECTARE = "hectare", "Per hectare"
        SHIFT = "shift", "Per shift"
        OPERATION = "operation", "Per operation"
        KM = "km", "Per kilometre"
        # Sales only — see the listing_total_price_unit_iff_sale constraint.
        TOTAL = "total", "Total price"

    # Denormalised from `asset.organization` so every org-scoped query is one
    # filter rather than a join. The composite foreign key added in the
    # initial migration keeps the two in step at the database level.
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="listings"
    )
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="listings")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="listings_created",
        help_text="Member who published it; null once the account is deleted",
    )

    listing_type = models.CharField(
        max_length=20, choices=ListingType.choices, db_index=True
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    availability = models.CharField(
        max_length=255,
        blank=True,
        help_text="Free text, as the frontend sends it — e.g. 'weekdays, March–October'",
    )

    price = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(0)]
    )
    currency = models.CharField(max_length=3, default="UZS")
    price_unit = models.CharField(max_length=20, choices=PriceUnit.choices)

    has_operator = models.BooleanField(default=False)
    has_delivery = models.BooleanField(default=False)

    # Where the machine is offered, which is not necessarily where the
    # organization is registered. Defaults from the org at creation.
    region = models.ForeignKey(
        Region,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="listings",
    )
    district = models.CharField(max_length=120, blank=True)

    published_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="First time this listing went active; never cleared afterwards",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Listing"
        verbose_name_plural = "Listings"
        indexes = [
            # Public feed: active listings, newest first.
            models.Index(fields=["status", "-created_at"], name="listing_status_created_idx"),
            # sort=price_asc / price_desc and the min_price/max_price filters.
            models.Index(fields=["status", "price"], name="listing_status_price_idx"),
            # GET /listings/my, filtered by status.
            models.Index(fields=["organization", "status"], name="listing_org_status_idx"),
        ]
        constraints = [
            # Without this the same tractor appears twice in the feed. Partial
            # so a draft, a paused and any number of archived listings for the
            # same machine are all still allowed.
            models.UniqueConstraint(
                fields=["asset", "listing_type"],
                condition=models.Q(status="active"),
                name="listing_one_active_per_asset_type",
                violation_error_message=(
                    "This machine already has an active listing of that type."
                ),
            ),
            # Rental prices are time- or area-based; a total price only means
            # anything for a sale. Stated as an equivalence, so neither half
            # can drift: "total" implies sale *and* sale implies "total".
            models.CheckConstraint(
                condition=(
                    models.Q(listing_type="sale", price_unit="total")
                    | (~models.Q(listing_type="sale") & ~models.Q(price_unit="total"))
                ),
                name="listing_total_price_unit_iff_sale",
                violation_error_message=(
                    "price_unit 'total' is for sale listings, and a sale must "
                    "be priced as a total."
                ),
            ),
            # `price` carries MinValueValidator(0), but a validator only runs
            # under full_clean(), which no write path calls — so until this
            # constraint existed a negative price saved happily and then sorted
            # to the top of the public feed under `sort=price_asc`. Stated in
            # the database for the same reason as the two above: it is the only
            # place a view cannot forget it.
            models.CheckConstraint(
                condition=models.Q(price__gte=0),
                name="listing_price_non_negative",
                violation_error_message="A price cannot be negative.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.get_listing_type_display()})"

    def clean(self) -> None:
        """
        Mirror of the composite foreign key, for the admin and for anything
        calling full_clean(): the database rejects a cross-org listing outright,
        but a readable message beats an IntegrityError.
        """
        if self.asset_id and self.organization_id:
            if self.asset.organization_id != self.organization_id:
                raise ValidationError(
                    "A listing must belong to the same organization as its asset."
                )


def listing_image_path(instance, filename: str) -> str:
    return f"listings/{instance.listing.public_id}/{filename}"


class ListingImage(PublicIdModel):
    """
    A marketing photo of an offer.

    Attached to the listing rather than the asset on purpose: these are photos
    taken to sell a particular offer, and an asset relisted later deserves new
    ones. Stored on local disk for now; responses always carry an absolute URL
    so moving to object storage is not a contract change (§0b).
    """

    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name="images"
    )
    file = models.FileField(
        upload_to=listing_image_path,
        validators=[FileExtensionValidator(IMAGE_EXTENSIONS)],
    )
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_primary = models.BooleanField(
        default=False, help_text="The card image. At most one per listing."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "pk"]
        verbose_name = "Listing image"
        verbose_name_plural = "Listing images"
        constraints = [
            models.UniqueConstraint(
                fields=["listing"],
                condition=models.Q(is_primary=True),
                name="listing_one_primary_image",
                violation_error_message="A listing can only have one primary image.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.listing_id} / {self.file.name}"
