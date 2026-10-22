"""
deals/models.py
The profile-facing history/review pipeline: a completed transaction between
two organizations, and the review the customer may leave on it.

Sourced from `Inquiry` for both rent and sale (seller-profile-proposal.md
§8.3) — **not** derived from `equipment.Booking`, which has no `listing` or
`inquiry` FK and books `Asset` rows across possibly several listings in one
agreement, so there is no clean way to fold it into a one-listing-one-deal
shape. If `/rentals` is ever built out for operational fulfillment, the
dependency should run the other way: an optional `Booking.deal` FK set from
a confirmed `Deal`, not the reverse.

Org-owned per §0.1 (`provider_organization`/`customer_organization`, both
copied from the inquiry at creation time — never client-supplied), and
attributed per §0.3 (`created_by`, plus every write logs to `ActivityLog`
from the view layer).
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from core.models import PublicIdModel
from inquiries.models import Inquiry
from listings.models import Listing
from users.models import Organization


class Deal(PublicIdModel):
    """One completed (or in-progress) transaction between two organizations."""

    class DealType(models.TextChoices):
        RENT = "rent", "Rent"
        SALE = "sale", "Sale"

    class Status(models.TextChoices):
        PENDING_CONFIRMATION = "pending_confirmation", "Pending confirmation"
        COMPLETED = "completed", "Completed"
        DECLINED = "declined", "Declined"

    # SET_NULL, not PROTECT: a listing later archived or otherwise removed
    # must not block the deal history that already references it — the same
    # choice `Inquiry.listing` would make if it weren't PROTECT for a
    # different reason (an inquiry outlives the offer going off the market).
    # Deal history is meant to outlive the listing too, but here the listing
    # itself may need to go, so the FK gives way instead.
    listing = models.ForeignKey(
        Listing, on_delete=models.SET_NULL, null=True, blank=True, related_name="deals"
    )
    # One inquiry becomes at most one deal (`deal_one_per_inquiry` below).
    # SET_NULL rather than PROTECT: deleting the inquiry (there is no such
    # endpoint today, but nothing should stop existing either) must not take
    # the deal history down with it.
    inquiry = models.ForeignKey(
        Inquiry, on_delete=models.SET_NULL, null=True, blank=True, related_name="deals"
    )

    # Both copied from the inquiry at creation time, exactly once, and never
    # from the request body (§0.1) — the same denormalisation
    # `Inquiry.provider_organization` does off `Listing.organization`.
    provider_organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="deals_as_provider"
    )
    customer_organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="deals_as_customer"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deals_created",
        help_text="The provider-side member who turned the inquiry into a deal",
    )

    deal_type = models.CharField(max_length=10, choices=DealType.choices)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.PENDING_CONFIRMATION,
        db_index=True,
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Deal"
        verbose_name_plural = "Deals"
        indexes = [
            # The profile's deal-history read: one org's completed deals,
            # newest first.
            models.Index(
                fields=["provider_organization", "-completed_at"],
                name="deal_provider_completed_idx",
            ),
            models.Index(
                fields=["customer_organization", "status"],
                name="deal_customer_status_idx",
            ),
        ]
        constraints = [
            # An organization cannot be both sides of its own transaction —
            # same shape as `inquiry_not_to_own_org`, and for the same reason:
            # a deal's two organization FKs are trusted inputs, not a client
            # claim, but the backstop belongs in the database regardless.
            models.CheckConstraint(
                condition=~Q(customer_organization=F("provider_organization")),
                name="deal_not_own_org",
            ),
            # One inquiry becomes at most one deal. Partial, because
            # `inquiry` is nullable (a deal survives its inquiry being
            # cleared) and NULL values never collide under a unique index.
            models.UniqueConstraint(
                fields=["inquiry"],
                condition=Q(inquiry__isnull=False),
                name="deal_one_per_inquiry",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_deal_type_display()} deal {self.customer_organization} → {self.provider_organization}"

    def clean(self) -> None:
        """
        Mirror of the invariant every write path enforces by construction
        (both organizations are copied off the inquiry, never supplied by a
        caller): a readable message beats a silent inconsistency reaching the
        admin.
        """
        if self.listing_id and self.provider_organization_id:
            if self.listing.organization_id != self.provider_organization_id:
                raise ValidationError(
                    "A deal's provider must be the organization that owns the listing."
                )
        if self.inquiry_id and self.provider_organization_id:
            if self.inquiry.provider_organization_id != self.provider_organization_id:
                raise ValidationError(
                    "A deal's provider must match the inquiry it was created from."
                )
            if self.inquiry.customer_organization_id != self.customer_organization_id:
                raise ValidationError(
                    "A deal's customer must match the inquiry it was created from."
                )


class Review(PublicIdModel):
    """One review, left by the customer organization on a completed deal."""

    deal = models.OneToOneField(Deal, on_delete=models.CASCADE, related_name="review")
    # Denormalised from `deal.customer_organization` — the reviewer is always
    # the customer side, never client-supplied (§0.1).
    author_organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="reviews_written"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviews_created",
    )
    rating = models.SmallIntegerField()
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Review"
        verbose_name_plural = "Reviews"
        constraints = [
            models.CheckConstraint(
                condition=Q(rating__gte=1) & Q(rating__lte=5),
                name="review_rating_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.rating}★ on {self.deal_id}"

    def clean(self) -> None:
        if self.deal_id and self.author_organization_id:
            if self.deal.customer_organization_id != self.author_organization_id:
                raise ValidationError(
                    "Only the customer organization on the deal may review it."
                )
