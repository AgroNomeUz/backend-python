"""
inquiries/models.py
First contact between a renter and the organization that owns a machine.

Under §0.1 an inquiry is **organization → organization**, sent by a named
member: `customer_organization` is who is asking, `provider_organization` is
who is being asked, and `created_by` is the person who actually typed it. That
is the same separation `equipment.Booking` already draws between
`customer_organization` and `created_by`, and it is what makes an inbox usable
by a team rather than by one account.

`Inquiry` is deliberately not a `Booking`. A booking is a commercial agreement
with dates, items, money and a validated status machine; an inquiry is a
message with an optional date range attached, and it has to stay cheap enough
that someone sends five of them before choosing. `Booking` keeps `/rentals`.

The listing is an FK rather than a copied title (§0.5): `Listing` is
soft-deleted to `archived` and never removed, so an inquiry outlives the offer
going off the market, and the machine going in for maintenance changes nothing
about a conversation already in progress.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.models import PublicIdModel
from listings.models import Listing
from users.models import Organization


class Inquiry(PublicIdModel):
    """One organization asking another about a listing."""

    listing = models.ForeignKey(
        Listing, on_delete=models.PROTECT, related_name="inquiries"
    )

    # Denormalised from `listing.organization`, so the inbox is one filter
    # rather than a join — the same move `Listing.organization` makes off its
    # asset, and kept honest the same way, by a composite foreign key added in
    # the initial migration.
    provider_organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="inquiries_received",
        help_text="The organization that owns the listing",
    )
    customer_organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="inquiries_sent",
        help_text="The organization asking",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inquiries_created",
        help_text="Member who sent it; null once the account is deleted",
    )

    message = models.TextField()

    # Optional: a sale inquiry has no date range, and a renter often asks
    # about availability before having dates in mind.
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    # `read` on the wire is derived from this, so there is one source of truth
    # for both the flag and "when".
    read_at = models.DateTimeField(null=True, blank=True)
    read_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inquiries_handled",
        help_text=(
            "Member who picked it up. In a shared inbox 'read' means nothing "
            "without a name against it."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Inquiry"
        verbose_name_plural = "Inquiries"
        indexes = [
            # The inbox and the sent folder, each newest first.
            models.Index(
                fields=["provider_organization", "-created_at"],
                name="inquiry_inbox_idx",
            ),
            models.Index(
                fields=["customer_organization", "-created_at"],
                name="inquiry_sent_idx",
            ),
            # The unread badge, and the count /stats/owner will want (§7).
            # Partial, because unread is the small half of a growing table.
            models.Index(
                fields=["provider_organization"],
                condition=Q(read_at__isnull=True),
                name="inquiry_unread_idx",
            ),
        ]
        constraints = [
            # An organization contacting itself is not a conversation, and it
            # would put its own listings in its own inbox. The endpoint checks
            # first and answers 400 with a reason; this is the backstop, in the
            # database for the same reason §0.5's invariants are.
            models.CheckConstraint(
                condition=~Q(customer_organization=F("provider_organization")),
                name="inquiry_not_to_own_org",
                violation_error_message=(
                    "You cannot send an inquiry to your own organization."
                ),
            ),
            # Only meaningful when both halves are present — either may be
            # omitted, and a one-sided range is a legitimate "from the 3rd
            # onwards".
            models.CheckConstraint(
                condition=(
                    Q(start_date__isnull=True)
                    | Q(end_date__isnull=True)
                    | Q(end_date__gte=F("start_date"))
                ),
                name="inquiry_dates_ordered",
                violation_error_message="The end date cannot precede the start date.",
            ),
        ]
        # Not a constraint: "read_by is set if and only if read_at is". It
        # reads like the equivalence `listing_total_price_unit_iff_sale`
        # states, but `read_by` is SET_NULL — hard-deleting a user would null
        # the column while `read_at` stood, and the CHECK would then fail the
        # *delete* rather than the write that broke the rule. The endpoint sets
        # both in one transaction and nothing else writes them.

    def __str__(self) -> str:
        return f"{self.customer_organization} → {self.listing}"

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def clean(self) -> None:
        """
        Mirror of the composite foreign key, for the admin and anything else
        calling full_clean(): the database rejects a mismatched pair outright,
        but a readable message beats an IntegrityError.
        """
        if self.listing_id and self.provider_organization_id:
            if self.listing.organization_id != self.provider_organization_id:
                raise ValidationError(
                    "An inquiry's provider must be the organization that owns "
                    "the listing."
                )


class InquiryMessage(PublicIdModel):
    """
    One turn in the conversation an `Inquiry` opened.

    An inquiry has two parties, so "who sent this" cannot get the same
    composite-foreign-key treatment `provider_organization` does above: that
    pattern vouches for exactly one `(child, organization)` pair, and a
    message's sender is legitimately either one. Storing which *side* sent it
    instead makes "the sender is a party to this inquiry" true by
    construction — `sender_organization` below simply reads it off the
    inquiry, so there is no denormalised organization column to keep in step
    and nothing for a view to get wrong.

    `Inquiry.message` is untouched by this model: `POST /inquiries` writes the
    opening `InquiryMessage` alongside it, in the same transaction, so a
    thread always has at least one turn — but the column stays the one
    everything already reading `Inquiry.message` has always used.
    """

    class Side(models.TextChoices):
        RENTER = "renter", "Renter"
        PROVIDER = "provider", "Provider"

    inquiry = models.ForeignKey(
        Inquiry, on_delete=models.CASCADE, related_name="messages"
    )
    sender_side = models.CharField(max_length=8, choices=Side.choices)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inquiry_messages_created",
        help_text="Member who sent it; null once the account is deleted",
    )
    body = models.TextField()

    # `default=`, not `auto_now_add=`: the opening message is stamped with the
    # inquiry's own `created_at` (see `_apply_create_inquiry`), and
    # `auto_now_add` would silently overwrite that on every insert, including
    # the migration that backfills one for every inquiry that predates this
    # model.
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        # `id` breaks the tie: pagination slices this ordering, and a
        # backfilled opener shares its `created_at` with the inquiry itself.
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["inquiry", "created_at"], name="inquiry_thread_idx"),
        ]
        # Not a constraint: "every inquiry has at least one message". A CHECK
        # cannot span rows, and a trigger would fail the *delete* of the
        # inquiry rather than the write that left it empty — the same
        # reasoning that keeps "read_by is set iff read_at is" out of
        # `Inquiry.Meta` above. It holds because `_apply_create_inquiry`
        # writes the opener in the same transaction as the inquiry, and the
        # migration that introduces this model backfills one for every row
        # that predates it.

    def __str__(self) -> str:
        return f"{self.sender_organization} on {self.inquiry}"

    @property
    def sender_organization(self) -> Organization:
        if self.sender_side == self.Side.PROVIDER:
            return self.inquiry.provider_organization
        return self.inquiry.customer_organization
