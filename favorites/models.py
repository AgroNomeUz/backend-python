"""
favorites/models.py
A saved listing — the smallest row in the system, and the one with the most
contested ownership.

§0.1 says every domain object carries an owning organization, and §6 asks
whether a favorite is really the organization's or the member's. It is both,
and the row says so: `organization` owns it, `user` is whose shortlist it is
on. Scoping it to the pair costs one column and gives two views of the same
table — the member's own saved listings by default, and the team's shared
shortlist under `?scope=organization` — where either alone would have needed
a migration to become the other.

`Favorite` is deliberately not a soft-deleted or audited-in-place row like
`Listing`: unsaving is a real delete. What survives is the `ActivityLog`
entry the endpoint writes, which is where "who saved this, and when did it
come off the list?" is answered.
"""

from django.conf import settings
from django.db import models

from core.models import PublicIdModel
from listings.models import Listing
from users.models import Organization


class Favorite(PublicIdModel):
    """One listing saved by one member of one organization."""

    listing = models.ForeignKey(
        Listing,
        # CASCADE, where `Inquiry.listing` is PROTECT: an inquiry is a
        # conversation that has to outlive the offer, a favorite is a
        # bookmark. Nothing is lost by it vanishing, and PROTECT here would
        # mean a stranger's saved list could block a listing being removed.
        on_delete=models.CASCADE,
        related_name="favorites",
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="favorites",
        help_text="The organization whose shared shortlist this is on",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        # Not SET_NULL, unlike `Inquiry.created_by`: there the member is
        # attribution on a row that stands without them, here they are half
        # of what the row *is*. A favorite belonging to nobody is not a
        # favorite, and `user` is part of the unique constraint, so it could
        # not be nullable even if we wanted it to be.
        on_delete=models.CASCADE,
        related_name="favorites",
        help_text="Member who saved it",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The member's own list, newest first — the default view.
            models.Index(fields=["user", "-created_at"], name="favorite_user_idx"),
            # The team's shared shortlist, newest first — ?scope=organization.
            models.Index(
                fields=["organization", "-created_at"], name="favorite_org_idx"
            ),
        ]
        constraints = [
            # What makes PUT idempotent in the database rather than only in
            # the view: two tabs clicking the save button at the same moment
            # cannot produce two rows.
            #
            # `organization` is in the key, not just `(user, listing)`,
            # because every query here is org-scoped — a member who moves to
            # another organization starts with an empty shortlist there
            # rather than dragging the old team's picks across, and can save
            # the same listing again without colliding with a row neither
            # view will ever show them.
            models.UniqueConstraint(
                fields=["organization", "user", "listing"],
                name="favorite_unique_per_member",
            ),
        ]
        # Not a constraint: "`organization` is the user's own organization".
        # It is the same denormalisation `Inquiry.provider_organization`
        # makes, and it would want the same composite foreign key —
        # `(user_id, organization_id)` → `users_user (id, organization_id)`.
        # But `User.organization` is SET_NULL, so removing an organization
        # nulls the column on the user while the copy on this row stands, and
        # the constraint would then fail the *delete*. The endpoint reads the
        # organization from the token (`caller_organization`) and never from
        # the body, and nothing else writes this table.

    def __str__(self) -> str:
        return f"{self.user} ♥ {self.listing}"
