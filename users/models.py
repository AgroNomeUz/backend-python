from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models.functions import Lower
from django.utils.text import slugify

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
    MANAGE_INQUIRIES = "inquiries.manage", "Manage inquiries"


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

    # Primary login identifier. Nullable rather than blank-able: `unique`
    # treats every "" as equal, so blank would allow exactly one phoneless
    # account. NULLs don't collide, and accounts created before phone-first
    # auth (or by `createsuperuser`) legitimately have none.
    phone = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        unique=True,
        db_index=True,
        help_text="E.164, e.g. +998901234567. The default login identifier.",
    )

    # One field, not first_name/last_name: Uzbek names don't split reliably
    # into given/family halves, and every screen renders the whole thing.
    # AbstractUser's two fields stay for Django admin compatibility.
    full_name = models.CharField(max_length=255, blank=True)

    telegram = models.CharField(
        max_length=64,
        blank=True,
        help_text="Telegram handle without the @, for future notifications",
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

    def get_full_name(self) -> str:
        """
        Prefer the single `full_name` field, falling back to Django's
        first/last pair for accounts predating it.

        Overridden rather than shadowed so the admin, password-reset emails
        and anything else calling Django's own API see the same name the API
        returns.
        """
        if self.full_name:
            return self.full_name
        return super().get_full_name()

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
    """
    Geographic / administrative region used to locate organizations.

    Global reference data, admin-curated and read-only over the API — every
    organization picks from the same 14 administrative units of Uzbekistan
    (`python manage.py seed_regions`).

    Three identifiers, each for a different consumer:
      * `code`  — ISO 3166-2:UZ without the country prefix ("AN"), what the
        public filters take.
      * `slug`  — the URL key for `/regions/{slug}`, derived from the name.
      * `soato` — the national administrative classifier, which is what Uzbek
        systems (and any district/city data added later) actually key on.
    """

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=15, unique=True)

    # Derived from `name` in save() when left blank, so every region has a URL
    # key without callers having to supply one. Blank-able rather than
    # required for the same reason it is filled in automatically: `unique`
    # treats every "" as equal, so a blank left in the database would allow
    # exactly one slug-less region.
    slug = models.SlugField(max_length=64, unique=True, blank=True)

    # SOATO — Uzbekistan's national classifier of administrative-territorial
    # objects. Regions are four digits (Andijan 1703); districts and cities
    # extend the same code to seven, which is why this is a CharField sized
    # for the longer form rather than an integer.
    #
    # Nullable rather than blank-able, exactly as `User.phone` is: `unique`
    # would treat every "" as the same value, so blank would permit only one
    # region without a code. Every seeded region has one; a region created ad
    # hoc gets NULL, and NULLs don't collide.
    soato = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        unique=True,
        db_index=True,
        help_text="SOATO administrative code, e.g. 1703 for Andijan region",
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Region"
        verbose_name_plural = "Regions"

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._derive_slug()
        super().save(*args, **kwargs)

    def _derive_slug(self) -> str:
        """
        A unique slug from the region's name.

        Two regions can legitimately share a name stem ("Tashkent Region" and
        "Tashkent City" don't, but a seed in another language could), so a
        collision appends a counter rather than raising — a slug is a URL key,
        not something a user chose.
        """
        base = slugify(self.name)[:60] or "region"
        candidate = base
        taken = Region.objects.exclude(pk=self.pk)
        counter = 2
        while taken.filter(slug=candidate).exists():
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate


class Organization(PublicIdModel):
    """
    A B2B legal entity (company, farm, cooperative, etc.).

    Created automatically when the first user of the organization signs up.
    That user becomes the `owner`. Additional staff are linked via
    `User.organization` FK.

    Sole traders are not a separate concept: they are an organization with
    one member and `entity_type = INDIVIDUAL`. Everything the app owns hangs
    off an Organization, so B2C and P2P are emulated rather than modelled.
    """

    class EntityType(models.TextChoices):
        INDIVIDUAL = "individual", "Individual"
        LEGAL_ENTITY = "legal_entity", "Legal entity"

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

    entity_type = models.CharField(
        max_length=20,
        choices=EntityType.choices,
        default=EntityType.INDIVIDUAL,
        db_index=True,
        help_text=(
            "Promoted to 'legal_entity' by ONEID verification, which is what "
            "fills the legal fields above."
        ),
    )

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
