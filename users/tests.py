"""
users/tests.py
Identity fields: phone normalization and its global uniqueness, the single
`full_name` field, and the organization entity type.

The member endpoints are org-scoped and permission-gated, so most tests go
through the API with a real bearer token rather than calling the view — the
guards are as much the subject as the field is.
"""

from django.test import TestCase

from api.auth import create_access_token
from core.models import ActivityLog
from users.models import OrgPermission, Organization, User
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
