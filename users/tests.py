"""
users/tests.py
Identity fields: phone normalization and its global uniqueness, the single
`full_name` field, and the organization entity type. Then the self-service
half — `/users/me`, the verified phone change, and the organization profile.

The member endpoints are org-scoped and permission-gated, so most tests go
through the API with a real bearer token rather than calling the view — the
guards are as much the subject as the field is.
"""

from itertools import count
from unittest.mock import patch

from django.test import TestCase

from api.auth import create_access_token
from api.models import PhoneOtp
from core.models import ActivityLog
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


class ProfileTestCase(TestCase):
    """
    An organization with three people in it: the owner, an admin holding
    `users.manage`, and a member holding nothing.

    The member is the interesting one — every self-service endpoint here has
    to work for an account with no permission codes at all, which is exactly
    what `PATCH /members/{id}` cannot do.
    """

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(name="Samarqand", code="UZ-SA")
        cls.owner = User.objects.create_user(
            username="owner", email="owner@example.com", phone="+998900000001"
        )
        cls.org = Organization.objects.create(
            name="AgroFarm", owner=cls.owner, region=cls.region
        )
        cls.owner.organization = cls.org
        cls.owner.save(update_fields=["organization"])

        cls.admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            organization=cls.org,
            permissions=[OrgPermission.MANAGE_USERS],
        )
        cls.member = User.objects.create_user(
            username="member",
            email="member@example.com",
            phone="+998900000002",
            full_name="Alisher",
            organization=cls.org,
        )

    def patch_me(self, user, **body):
        return self.client.patch(
            "/api/v1/users/me",
            data=body,
            content_type="application/json",
            **auth(user),
        )


class CurrentUserReadTests(ProfileTestCase):
    """`GET /users/me` — item 4: profile and organization in one call."""

    def test_returns_the_nested_organization(self):
        response = self.client.get("/api/v1/users/me", **auth(self.member))

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["id"], str(self.member.public_id))
        self.assertEqual(body["full_name"], "Alisher")

        org = body["organization"]
        self.assertEqual(org["id"], str(self.org.public_id))
        self.assertEqual(org["name"], "AgroFarm")
        self.assertEqual(org["region"]["code"], "UZ-SA")
        self.assertEqual(org["entity_type"], "individual")
        self.assertFalse(org["is_verified"])

    def test_plain_member_is_not_an_owner_and_holds_nothing(self):
        body = self.client.get("/api/v1/users/me", **auth(self.member)).json()

        self.assertFalse(body["is_owner"])
        self.assertEqual(body["permissions"], [])

    def test_owner_reads_back_every_permission_code(self):
        body = self.client.get("/api/v1/users/me", **auth(self.owner)).json()

        self.assertTrue(body["is_owner"])
        self.assertEqual(body["permissions"], list(OrgPermission.values))

    def test_matches_what_login_returned(self):
        """
        The frontend caches the login response and refreshes it from here, so
        a difference between the two shows up as a field that silently
        reverts.
        """
        self.member.set_password("s3cret-pass-phrase")
        self.member.save(update_fields=["password"])

        login = self.client.post(
            "/api/v1/auth/login",
            data={"username": "member", "password": "s3cret-pass-phrase"},
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200, login.content)

        me = self.client.get("/api/v1/users/me", **auth(self.member))
        self.assertEqual(login.json()["user"], me.json())

    def test_requires_authentication(self):
        self.assertEqual(self.client.get("/api/v1/users/me").status_code, 401)


class CurrentUserUpdateTests(ProfileTestCase):
    """`PATCH /users/me` — item 1: the permission-free self-edit path."""

    def test_member_without_users_manage_can_edit_themselves(self):
        self.assertFalse(self.member.has_org_perm(OrgPermission.MANAGE_USERS))

        response = self.patch_me(
            self.member, full_name="Alisher Valiyev", telegram="alisher"
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.member.refresh_from_db()
        self.assertEqual(self.member.full_name, "Alisher Valiyev")
        self.assertEqual(self.member.telegram, "alisher")

    def test_the_same_member_still_cannot_use_the_admin_endpoint(self):
        """
        The 403 that sent the frontend here in the first place. It is correct
        and stays — `/members/{id}` is for administering other people.
        """
        response = self.client.patch(
            f"/api/v1/members/{self.member.public_id}",
            data={"full_name": "Via the admin route"},
            content_type="application/json",
            **auth(self.member),
        )

        self.assertEqual(response.status_code, 403)
        self.member.refresh_from_db()
        self.assertEqual(self.member.full_name, "Alisher")

    def test_the_edit_is_audited(self):
        """§0.3 — a self-edit is still a write against the organization."""
        self.patch_me(self.member, full_name="Alisher Valiyev")

        entry = ActivityLog.objects.filter(
            organization=self.org, action=ActivityLog.Action.UPDATED
        ).latest("created_at")

        self.assertEqual(entry.actor_id, self.member.pk)
        self.assertEqual(entry.changes["full_name"]["from"], "Alisher")
        self.assertEqual(entry.changes["full_name"]["to"], "Alisher Valiyev")

    def test_a_no_op_edit_writes_no_audit_row(self):
        before = ActivityLog.objects.count()
        response = self.patch_me(self.member, full_name="Alisher")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ActivityLog.objects.count(), before)

    def test_privileged_fields_are_not_reachable(self):
        """
        `permissions`, `is_active` and `phone` are absent from `SelfUpdateIn`,
        so sending them is ignored rather than applied.
        """
        response = self.patch_me(
            self.member,
            full_name="Alisher Valiyev",
            permissions=[OrgPermission.MANAGE_USERS.value],
            is_active=False,
            phone="+998909998877",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.member.refresh_from_db()
        self.assertEqual(self.member.full_name, "Alisher Valiyev")
        self.assertEqual(self.member.permissions, [])
        self.assertTrue(self.member.is_active)
        self.assertEqual(self.member.phone, "+998900000002")

    def test_email_is_changed_and_lowercased(self):
        response = self.patch_me(self.member, email="Alisher@Example.COM")

        self.assertEqual(response.status_code, 200, response.content)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "alisher@example.com")

    def test_a_taken_email_is_refused_case_insensitively(self):
        """The `user_email_ci_unique` constraint, surfaced as a 400."""
        response = self.patch_me(self.member, email="OWNER@example.com")

        self.assertEqual(response.status_code, 400)
        self.assertIn("Email already registered", response.json()["detail"])
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, "member@example.com")

    def test_an_empty_body_changes_nothing(self):
        response = self.patch_me(self.member)

        self.assertEqual(response.status_code, 200, response.content)
        self.member.refresh_from_db()
        self.assertEqual(self.member.full_name, "Alisher")


class PhoneChangeTests(ProfileTestCase):
    """
    `POST /users/me/phone` — item 2: a phone change costs a code sent to the
    new number.

    The code is learned by patching the generator, never read out of a
    response — the same rule `api/tests.py` follows, and the reason
    `OtpDisclosureTests` exists.
    """

    NEW_PHONE = "+998905554433"

    def setUp(self):
        super().setUp()
        self.issued_codes = count(100000)
        self.last_code = None
        patcher = patch("api.otp.get_random_string", side_effect=self._next_code)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _next_code(self, *args, **kwargs) -> str:
        self.last_code = str(next(self.issued_codes))
        return self.last_code

    def start(self, phone=NEW_PHONE, user=None):
        return self.client.post(
            "/api/v1/users/me/phone",
            data={"phone": phone},
            content_type="application/json",
            **auth(user or self.member),
        )

    def confirm(self, code, phone=NEW_PHONE, user=None):
        return self.client.post(
            "/api/v1/users/me/phone/verify",
            data={"phone": phone, "code": code},
            content_type="application/json",
            **auth(user or self.member),
        )

    def test_the_code_goes_to_the_new_number(self):
        response = self.start()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"retry_after": 60})
        self.assertEqual(PhoneOtp.objects.filter(phone=self.NEW_PHONE).count(), 1)
        # Nothing is written until the code comes back.
        self.member.refresh_from_db()
        self.assertEqual(self.member.phone, "+998900000002")

    def test_the_swap_happens_on_a_correct_code(self):
        code = None
        self.start("+998 90 555-44-33")
        code = self.last_code

        response = self.confirm(code)

        self.assertEqual(response.status_code, 200, response.content)
        self.member.refresh_from_db()
        self.assertEqual(self.member.phone, self.NEW_PHONE)
        self.assertEqual(response.json()["phone"], self.NEW_PHONE)

    def test_the_swap_is_audited(self):
        self.start()
        self.confirm(self.last_code)

        entry = ActivityLog.objects.filter(
            organization=self.org, action=ActivityLog.Action.UPDATED
        ).latest("created_at")

        self.assertEqual(entry.actor_id, self.member.pk)
        self.assertEqual(entry.changes["phone"]["from"], "+998900000002")
        self.assertEqual(entry.changes["phone"]["to"], self.NEW_PHONE)

    def test_a_wrong_code_leaves_the_number_alone(self):
        self.start()

        response = self.confirm("000000")

        self.assertEqual(response.status_code, 400)
        self.member.refresh_from_db()
        self.assertEqual(self.member.phone, "+998900000002")

    def test_confirming_without_starting_is_refused(self):
        response = self.confirm("123456")

        self.assertEqual(response.status_code, 400)
        self.member.refresh_from_db()
        self.assertEqual(self.member.phone, "+998900000002")

    def test_a_number_someone_else_holds_is_refused_before_any_code(self):
        response = self.start(self.owner.phone)

        self.assertEqual(response.status_code, 400)
        self.assertIn("Phone number already registered", response.json()["detail"])
        self.assertFalse(PhoneOtp.objects.filter(phone=self.owner.phone).exists())

    def test_your_own_number_is_refused(self):
        response = self.start(self.member.phone)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PhoneOtp.objects.exists())

    def test_a_blank_number_is_refused(self):
        response = self.start("   ")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PhoneOtp.objects.exists())

    def test_resending_inside_the_cooldown_is_429_with_a_countdown(self):
        self.start()

        response = self.start()

        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertLessEqual(body["retry_after"], 60)
        self.assertIn("detail", body)

    def test_a_code_cannot_be_spent_twice(self):
        self.start()
        code = self.last_code
        self.assertEqual(self.confirm(code).status_code, 200)

        # Back to a third number, replaying the burnt code.
        self.assertEqual(self.confirm(code, phone="+998907776655").status_code, 400)
        self.member.refresh_from_db()
        self.assertEqual(self.member.phone, self.NEW_PHONE)

    def test_requires_authentication(self):
        response = self.client.post(
            "/api/v1/users/me/phone",
            data={"phone": self.NEW_PHONE},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)


class OwnerPhoneProtectionTests(ProfileTestCase):
    """
    `PATCH /members/{id}` must not let staff move the owner's number.

    Phone is the OTP login identifier, so an admin who could rewrite it could
    point it at a handset they hold and log in as the owner — the escalation
    that `reset-password` and `deactivate` are already owner-protected against.
    """

    def patch_member(self, target, **body):
        return self.client.patch(
            f"/api/v1/members/{target.public_id}",
            data=body,
            content_type="application/json",
            **auth(self.admin),
        )

    def test_staff_cannot_change_the_owners_phone(self):
        response = self.patch_member(self.owner, phone="+998909998877")

        self.assertEqual(response.status_code, 403)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.phone, "+998900000001")

    def test_staff_can_still_change_an_ordinary_members_phone(self):
        response = self.patch_member(self.member, phone="+998 90 999 88 77")

        self.assertEqual(response.status_code, 200, response.content)
        self.member.refresh_from_db()
        self.assertEqual(self.member.phone, "+998909998877")

    def test_the_owners_other_fields_are_still_editable(self):
        """The guard is on the credential, not on the owner's whole record."""
        response = self.patch_member(self.owner, full_name="Bosh Direktor")

        self.assertEqual(response.status_code, 200, response.content)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.full_name, "Bosh Direktor")


class OrganizationProfileTests(ProfileTestCase):
    """`GET` / `PATCH /org` — item 3."""

    def patch_org(self, user, **body):
        return self.client.patch(
            "/api/v1/org",
            data=body,
            content_type="application/json",
            **auth(user),
        )

    def test_any_member_can_read_it(self):
        response = self.client.get("/api/v1/org", **auth(self.member))

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["id"], str(self.org.public_id))
        self.assertEqual(body["name"], "AgroFarm")
        self.assertEqual(body["region"]["code"], "UZ-SA")
        self.assertEqual(body["entity_type"], "individual")
        self.assertFalse(body["is_verified"])
        self.assertIn("created_at", body)

    def test_member_count_covers_the_whole_roster(self):
        body = self.client.get("/api/v1/org", **auth(self.member)).json()

        self.assertEqual(body["member_count"], 3)

    def test_member_count_ignores_other_organizations(self):
        stranger = User.objects.create_user(username="stranger")
        Organization.objects.create(name="Elsewhere", owner=stranger)

        body = self.client.get("/api/v1/org", **auth(self.member)).json()

        self.assertEqual(body["member_count"], 3)

    def test_editing_requires_users_manage(self):
        response = self.patch_org(self.member, name="Renamed")

        self.assertEqual(response.status_code, 403)
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, "AgroFarm")

    def test_an_admin_can_edit_the_profile(self):
        response = self.patch_org(
            self.admin,
            name="AgroFarm LLC",
            address="Registon 1",
            tax_number="123456789",
            email="info@agrofarm.uz",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, "AgroFarm LLC")
        self.assertEqual(self.org.address, "Registon 1")
        self.assertEqual(self.org.tax_number, "123456789")
        self.assertEqual(self.org.email, "info@agrofarm.uz")

    def test_the_edit_is_audited(self):
        self.patch_org(self.admin, name="AgroFarm LLC")

        entry = ActivityLog.objects.filter(
            organization=self.org, action=ActivityLog.Action.UPDATED
        ).latest("created_at")

        self.assertEqual(entry.actor_id, self.admin.pk)
        self.assertEqual(entry.changes["name"]["from"], "AgroFarm")
        self.assertEqual(entry.changes["name"]["to"], "AgroFarm LLC")

    def test_the_region_can_be_moved_and_the_move_is_readable_in_history(self):
        other = Region.objects.create(name="Buxoro", code="UZ-BU")

        response = self.patch_org(self.admin, region_id=str(other.public_id))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["region"]["code"], "UZ-BU")
        self.org.refresh_from_db()
        self.assertEqual(self.org.region_id, other.pk)

        entry = ActivityLog.objects.filter(
            organization=self.org, action=ActivityLog.Action.UPDATED
        ).latest("created_at")
        self.assertEqual(entry.changes["region"]["from"], "Samarqand (UZ-SA)")
        self.assertEqual(entry.changes["region"]["to"], "Buxoro (UZ-BU)")

    def test_an_unknown_region_is_refused(self):
        response = self.patch_org(
            self.admin,
            name="AgroFarm LLC",
            region_id="00000000-0000-0000-0000-000000000000",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown region", response.json()["detail"])
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, "AgroFarm")

    def test_is_verified_cannot_be_written(self):
        """It drives the verified badge on public listings (§2)."""
        self.org.is_verified = True
        self.org.save(update_fields=["is_verified"])

        response = self.patch_org(self.admin, name="AgroFarm LLC", is_verified=False)

        self.assertEqual(response.status_code, 200, response.content)
        self.org.refresh_from_db()
        self.assertTrue(self.org.is_verified)
        self.assertEqual(self.org.name, "AgroFarm LLC")

    def test_entity_type_cannot_be_written(self):
        """Only ONEID verification promotes an org to a legal entity (§2b)."""
        response = self.patch_org(self.admin, entity_type="legal_entity")

        self.assertEqual(response.status_code, 200, response.content)
        self.org.refresh_from_db()
        self.assertEqual(self.org.entity_type, Organization.EntityType.INDIVIDUAL)

    def test_requires_authentication(self):
        self.assertEqual(self.client.get("/api/v1/org").status_code, 401)
