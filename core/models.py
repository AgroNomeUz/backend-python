"""
core/models.py
Shared abstract model bases used by all apps.
"""

import uuid

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class PublicIdModel(models.Model):
    """
    Adds a non-primary UUID exposed to API clients instead of the integer PK.

    Integer PKs stay internal (cheap joins, ordered index); `public_id` is the
    only identifier that leaves the backend. Look up API-supplied ids with
    `Model.objects.get(public_id=...)`, never by `pk`.
    """

    public_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
    )

    class Meta:
        abstract = True


class ActivityLog(PublicIdModel):
    """
    Append-only record of who changed what inside an organization.

    Every write endpoint that mutates org-owned data must log here, so an
    organization can always answer "who added / edited / removed this?".
    The target is a generic FK, so the same table covers assets today and
    bookings, pricing, farms, … later.

    Rows are never updated or deleted; `target_repr` snapshots the object's
    label at the time of the action so history survives the object itself.
    """

    class Action(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        DELETED = "deleted", "Deleted"
        STATUS_CHANGED = "status_changed", "Status Changed"

    organization = models.ForeignKey(
        "users.Organization",
        on_delete=models.CASCADE,
        related_name="activity_logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity_logs",
        help_text="User who performed the action; null once the account is deleted",
    )
    action = models.CharField(max_length=20, choices=Action.choices, db_index=True)

    content_type = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    target = GenericForeignKey("content_type", "object_id")
    target_repr = models.CharField(
        max_length=255,
        blank=True,
        help_text="str() of the target when the action happened",
    )

    changes = models.JSONField(
        default=dict,
        blank=True,
        help_text='Field-level diff: {"field": {"from": ..., "to": ...}}',
    )
    context = models.JSONField(
        default=dict,
        blank=True,
        help_text="Request metadata (ip, user agent, endpoint, …)",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Activity Log"
        verbose_name_plural = "Activity Logs"
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self) -> str:
        who = self.actor.username if self.actor else "system"
        return f"{who} {self.action} {self.target_repr or self.content_type}"

    @classmethod
    def record(
        cls,
        *,
        organization,
        actor,
        action: str,
        target=None,
        changes: dict | None = None,
        context: dict | None = None,
    ) -> "ActivityLog":
        """Write one audit row. Call from the view/service layer, not signals."""
        content_type = (
            ContentType.objects.get_for_model(target.__class__) if target else None
        )
        return cls.objects.create(
            organization=organization,
            actor=actor,
            action=action,
            content_type=content_type,
            object_id=target.pk if target else None,
            target_repr=str(target)[:255] if target else "",
            changes=changes or {},
            context=context or {},
        )
