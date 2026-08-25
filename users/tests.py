"""
users/tests.py
Identity fields: phone normalization and its global uniqueness, the single
`full_name` field, and the organization entity type. Plus the region
reference data — its three identifiers and the public endpoints that read
them.

The member endpoints are org-scoped and permission-gated, so most tests go
through the API with a real bearer token rather than calling the view — the
guards are as much the subject as the field is.
"""

from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from api.auth import create_access_token
from core.models import ActivityLog
from equipment.models import Asset, EquipmentCategory, EquipmentModel, Manufacturer
from listings.models import Listing
from users.management.commands.seed_regions import REGIONS
from users.models import OrgPermission, Organization, Region, User
from users.services import normalize_phone


def auth(user: User) -> dict:
    """Request kwargs carrying a bearer token for `user`."""
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}


class NormalizePhoneTests(TestCase):
    def test_strips_formatting_but_keeps_plus(self):
        self.assertEqual(normalize_phone("+998 90 123-45-67"), "+998901234567")
        self.assertEqual(normalize_phone("(90) 123 45 67"), "901234567")

    def test_empty_becomes_none(self):
        # The column is NULL-not-blank: "" would collide under `unique`.
        for value in ("", "   ", None, "----"):
            self.assertIsNone(normalize_phone(value), repr(value))


class UserNameTests(TestCase):
    def test_full_name_is_preferred(self):
        user = User.objects.create_user(
            username="a", first_name="Ali", last_name="Valiyev", full_name="Ali Valiyev"
        )
        self.assertEqual(user.get_full_name(), "Ali Valiyev")

    def test_falls_back_to_django_name_pair(self):
        """Accounts predating `full_name` must not render nameless."""
        user = User.objects.create_user(username="b", first_name="Ali", last_name="V")
        self.assertEqual(user.get_full_name(), "Ali V")


class OrganizationEntityTypeTests(TestCase):
    def test_defaults_to_individual(self):
        owner = User.objects.create_user(username="c")
        org = Organization.objects.create(name="Solo", owner=owner)
        self.assertEqual(org.entity_type, Organization.EntityType.INDIVIDUAL)
        self.assertFalse(org.is_verified)


class MemberPhoneTests(TestCase):
    """`POST`/`PATCH /members` must reject a duplicate phone with a 400."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="admin", email="admin@example.com", password="x"
        )
        cls.org = Organization.objects.create(name="Org", owner=cls.admin)
        cls.admin.organization = cls.org
        cls.admin.save(update_fields=["organization"])

        # A second organization: phones are unique across the whole install,
        # not per org, so this one's number must still block a reuse.
        cls.other_admin = User.objects.create_user(
            username="other", email="other@example.com", phone="+998901112233"
        )
        cls.other_org = Organization.objects.create(name="Other", owner=cls.other_admin)
        cls.other_admin.organization = cls.other_org
        cls.other_admin.save(update_fields=["organization"])

    def create_member(self, **body):
        payload = {"email": "new@example.com", **body}
        return self.client.post(
            "/api/v1/members",
            data=payload,
            content_type="application/json",
            **auth(self.admin),
        )

    def test_phone_is_stored_normalized(self):
        response = self.create_member(phone="+998 90 555-44-33", full_name="Yangi Xodim")
        self.assertEqual(response.status_code, 201, response.content)

        member = User.objects.get(email="new@example.com")
        self.assertEqual(member.phone, "+998905554433")
        self.assertEqual(member.full_name, "Yangi Xodim")
        self.assertEqual(member.organization_id, self.org.pk)

    def test_duplicate_phone_is_rejected_with_400(self):
        """
        The unique column would raise IntegrityError and surface as a 500;
        the view checks first so the client gets a usable error.
        """
        response = self.create_member(phone="+998 90 111 22 33")

        self.assertEqual(response.status_code, 400)
        self.assertIn("Phone number already registered", response.json()["detail"])
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_blank_phone_does_not_collide(self):
        """Two phoneless members are legal — NULLs don't collide, "" would."""
        first = self.create_member(email="one@example.com")
        second = self.create_member(email="two@example.com")

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertIsNone(User.objects.get(email="one@example.com").phone)
        self.assertIsNone(User.objects.get(email="two@example.com").phone)

    def test_patch_to_a_taken_phone_is_rejected(self):
        self.create_member(phone="+998905554433")
        member = User.objects.get(email="new@example.com")

        response = self.client.patch(
            f"/api/v1/members/{member.public_id}",
            data={"phone": "+998901112233"},
            content_type="application/json",
            **auth(self.admin),
        )

        self.assertEqual(response.status_code, 400)
        member.refresh_from_db()
        self.assertEqual(member.phone, "+998905554433")

    def test_patch_keeping_own_phone_is_allowed(self):
        """The uniqueness check must exclude the member being edited."""
        self.create_member(phone="+998905554433")
        member = User.objects.get(email="new@example.com")

        response = self.client.patch(
            f"/api/v1/members/{member.public_id}",
            data={"phone": "+998 90 555 44 33", "full_name": "Yangilandi"},
            content_type="application/json",
            **auth(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        member.refresh_from_db()
        self.assertEqual(member.phone, "+998905554433")
        self.assertEqual(member.full_name, "Yangilandi")

    def test_member_creation_is_audited_with_the_new_fields(self):
        """§0.3 — every write is attributed, and the diff covers what changed."""
        self.create_member(phone="+998905554433", full_name="Yangi Xodim")

        entry = ActivityLog.objects.filter(
            organization=self.org, action=ActivityLog.Action.CREATED
        ).latest("created_at")

        self.assertEqual(entry.actor_id, self.admin.pk)
        self.assertEqual(entry.changes["phone"]["to"], "+998905554433")
        self.assertEqual(entry.changes["full_name"]["to"], "Yangi Xodim")

    def test_member_without_users_manage_cannot_create(self):
        """Reads are open to any member; writes need the code."""
        plain = User.objects.create_user(
            username="plain", email="plain@example.com", organization=self.org
        )
        self.assertFalse(plain.has_org_perm(OrgPermission.MANAGE_USERS))

        response = self.client.post(
            "/api/v1/members",
            data={"email": "blocked@example.com"},
            content_type="application/json",
            **auth(plain),
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(email="blocked@example.com").exists())


class RegionIdentifierTests(TestCase):
    """
    A region carries three identifiers because three different things key off
    it: `code` for the existing filters, `slug` for the URL, and `soato` for
    the national classifier.
    """

    def test_slug_is_derived_from_the_name(self):
        region = Region.objects.create(name="Tashkent Region", code="TK1")
        self.assertEqual(region.slug, "tashkent-region")

    def test_an_explicit_slug_is_left_alone(self):
        region = Region.objects.create(name="Andijan", code="AN1", slug="custom")
        self.assertEqual(region.slug, "custom")

    def test_a_colliding_name_gets_a_suffix_rather_than_an_error(self):
        first = Region.objects.create(name="Tashkent", code="T1")
        second = Region.objects.create(name="Tashkent", code="T2")
        self.assertEqual(first.slug, "tashkent")
        self.assertEqual(second.slug, "tashkent-2")

    def test_soato_is_null_not_blank_when_absent(self):
        """
        The same reason `User.phone` is nullable: `unique` treats every "" as
        the same value, so two code-less regions would collide.
        """
        first = Region.objects.create(name="One", code="O1")
        second = Region.objects.create(name="Two", code="O2")
        self.assertIsNone(first.soato)
        self.assertIsNone(second.soato)


class SeedRegionsTests(TestCase):
    def test_seeding_is_idempotent_and_fills_soato(self):
        call_command("seed_regions", stdout=StringIO())
        call_command("seed_regions", stdout=StringIO())

        self.assertEqual(Region.objects.count(), len(REGIONS))
        andijan = Region.objects.get(code="AN")
        self.assertEqual(andijan.soato, "1703")
        self.assertEqual(andijan.slug, "andijan")
        # Every seeded region gets all three identifiers.
        self.assertFalse(Region.objects.filter(soato__isnull=True).exists())
        self.assertFalse(Region.objects.filter(slug="").exists())


class RegionEndpointTests(TestCase):
    """
    `GET /regions` and `/regions/{slug}` — public, unpaginated, and counting
    **active listings**, unlike the frozen `/public/regions` which still
    counts available assets.
    """

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(name="Andijan", code="AN", soato="1703")
        cls.empty_region = Region.objects.create(name="Navoiy", code="NW", soato="1712")

        owner = User.objects.create_user(username="region-owner")
        org = Organization.objects.create(name="Region Org", owner=owner, region=cls.region)

        manufacturer = Manufacturer.objects.create(name="MTZ")
        tractors = EquipmentCategory.objects.create(name="Tractor", slug="tractor")
        combines = EquipmentCategory.objects.create(name="Combine", slug="combine")

        def listing(category, count):
            model = EquipmentModel.objects.create(
                manufacturer=manufacturer, category=category, name=f"{category.slug}-model"
            )
            for _ in range(count):
                asset = Asset.objects.create(organization=org, equipment_model=model)
                Listing.objects.create(
                    organization=org,
                    asset=asset,
                    region=cls.region,
                    listing_type=Listing.ListingType.RENT,
                    status=Listing.Status.ACTIVE,
                    title="offer",
                    price=Decimal("1"),
                    price_unit=Listing.PriceUnit.DAY,
                )

        listing(tractors, 3)
        listing(combines, 1)

    def test_list_needs_no_token_and_carries_all_three_identifiers(self):
        response = self.client.get("/api/v1/regions")
        self.assertEqual(response.status_code, 200)
        by_code = {region["code"]: region for region in response.json()}
        self.assertEqual(by_code["AN"]["slug"], "andijan")
        self.assertEqual(by_code["AN"]["soato"], "1703")

    def test_listing_count_and_popular_equipment(self):
        by_code = {r["code"]: r for r in self.client.get("/api/v1/regions").json()}
        self.assertEqual(by_code["AN"]["listing_count"], 4)
        # Busiest category first.
        self.assertEqual(
            by_code["AN"]["popular_equipment"],
            [{"type": "tractor", "count": 3}, {"type": "combine", "count": 1}],
        )

    def test_a_region_with_no_listings_still_appears(self):
        by_code = {r["code"]: r for r in self.client.get("/api/v1/regions").json()}
        self.assertEqual(by_code["NW"]["listing_count"], 0)
        self.assertEqual(by_code["NW"]["popular_equipment"], [])

    def test_detail_by_slug(self):
        response = self.client.get("/api/v1/regions/andijan")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["listing_count"], 4)

    def test_unknown_slug_is_a_404(self):
        self.assertEqual(self.client.get("/api/v1/regions/atlantis").status_code, 404)

    def test_only_published_listings_are_counted(self):
        Listing.objects.update(status=Listing.Status.DRAFT)
        by_code = {r["code"]: r for r in self.client.get("/api/v1/regions").json()}
        self.assertEqual(by_code["AN"]["listing_count"], 0)
