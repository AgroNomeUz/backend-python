from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Custom user model. Extend here instead of the default Django user.

    Every user belongs to an Organization (via `organization` FK).
    The user who creates the org is its owner (`owned_organization` reverse).
    Use `user.is_organization_owner` to check ownership without an extra query.
    """

    organization = models.ForeignKey(
        "Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="members",
    )

    @property
    def is_organization_owner(self) -> bool:
        """True if this user is the owner of an organization."""
        return hasattr(self, "owned_organization")

    class Meta(AbstractUser.Meta):
        swappable = "AUTH_USER_MODEL"


class Region(models.Model):
    """Geographic / administrative region used to locate organizations."""

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=15, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Region"
        verbose_name_plural = "Regions"

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class Organization(models.Model):
    """
    A B2B legal entity (company, farm, cooperative, etc.).

    Created automatically when the first user of the organization signs up.
    That user becomes the `owner`. Additional staff are linked via
    `User.organization` FK.
    """

    name = models.CharField(max_length=255, blank=True)
    address = models.CharField(max_length=255, blank=True)
    region = models.ForeignKey(
        Region,
        on_delete=models.PROTECT,
        related_name="organizations",
        null=True,
        blank=True,
    )
    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owned_organization",
    )

    # Optional B2B contact / legal fields
    tax_number = models.CharField(
        max_length=32,
        blank=True,
        help_text="Tax / INN registration number",
    )
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)

    # Audit timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Organization"
        verbose_name_plural = "Organizations"

    def __str__(self) -> str:
        return self.name
