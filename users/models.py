from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models.functions import Lower

from core.models import PublicIdModel


class OrgPermission(models.TextChoices):
    """
    What a member is allowed to do inside their own organization.

    Deliberately coarse: one code per manageable area, not per endpoint.
    Reads are open to every member of the org — a permission only ever gates
    writes. The owner implicitly holds every code (see `User.has_org_perm`),
    so an org always has at least one account that can grant permissions.

    Add a code here when a new write-capable area ships; existing members
    simply won't have it until someone grants it.
    """

    MANAGE_EQUIPMENT = "equipment.manage", "Manage equipment"
    MANAGE_USERS = "users.manage", "Manage users"


class User(PublicIdModel, AbstractUser):
    """
    Custom user model. Extend here instead of the default Django user.

    Every user belongs to an Organization (via `organization` FK).
    The user who creates the org is its owner (`owned_organization` reverse).
    Use `user.is_organization_owner` to check ownership without an extra query.

    Staff accounts are created by an org member holding
    `OrgPermission.MANAGE_USERS`; they get a one-time password and carry
    `must_change_password` until they set their own.
    """

    organization = models.ForeignKey(
        "Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="members",
    )

    permissions = ArrayField(
        models.CharField(max_length=32, choices=OrgPermission.choices),
        default=list,
        blank=True,
        help_text=(
            "Permission codes granted inside the user's own organization. "
            "Ignored for the owner, who implicitly holds all of them."
        ),
    )

    must_change_password = models.BooleanField(
        default=False,
        help_text=(
            "Set when the account is created or its password is reset by an "
            "admin. Cleared once the user sets a password themselves."
        ),
    )

    @property
    def is_organization_owner(self) -> bool:
        """True if this user is the owner of an organization."""
        return hasattr(self, "owned_organization")

    @property
    def org_permissions(self) -> list[str]:
        """
        Effective permission codes — what the API actually checks against.

        Owners are not stored with permissions; they are handed the full set
        here so callers never need to special-case ownership themselves.
        """
        if self.is_organization_owner:
            return list(OrgPermission.values)
        return sorted(self.permissions or [])

    def has_org_perm(self, code: str) -> bool:
        """True if this user may perform `code` inside their organization."""
        if self.is_organization_owner:
            return True
        return code in (self.permissions or [])

    class Meta(AbstractUser.Meta):
        swappable = "AUTH_USER_MODEL"
        constraints = [
            # Login accepts an email in place of a username, and staff accounts
            # are addressed by email alone — so an email may identify at most
            # one account. Case-insensitive, and blank is exempt because
            # accounts made with `createsuperuser` may have no email at all.
            models.UniqueConstraint(
                Lower("email"),
                condition=~models.Q(email=""),
                name="user_email_ci_unique",
            ),
        ]


class Region(PublicIdModel):
    """Geographic / administrative region used to locate organizations."""

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=15, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Region"
        verbose_name_plural = "Regions"

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class Organization(PublicIdModel):
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

    # Set by staff after checking the company's documents. Drives the badge on
    # public listings, so never let an endpoint write it.
    is_verified = models.BooleanField(default=False, db_index=True)

    # Audit timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Organization"
        verbose_name_plural = "Organizations"

    def __str__(self) -> str:
        return self.name
