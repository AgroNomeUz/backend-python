"""
api/tests.py
Phone-first authentication: the OTP lifecycle, the limits that make a
six-digit secret safe, the disclosure rules that keep the code out of
everyone else's hands, and the one public path that creates an account.

A test learns the passcode by patching the generator, **not** by reading it
out of a response or a log — that is the whole point of `OtpDisclosureTests`
below, and a helper that depended on either channel would quietly stop the
suite from noticing if the leak came back.
"""

from datetime import timedelta
from itertools import count
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from api.auth import create_access_token, create_signup_token
from api.models import PhoneOtp, RefreshToken
from api.sms import mask_phone
from core.models import ActivityLog
from users.models import OrgPermission, Organization, Region, User

PHONE = "+998901234567"

# Escape hatches are refused in production; most tests exercise the ordinary
# path, so they pin the environment rather than inheriting the developer's.
DEVELOPMENT = override_settings(ENVIRONMENT="development")


def auth(user: User) -> dict:
    """Request kwargs carrying a bearer token for `user`."""
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}


class AuthTestCase(TestCase):
    """Shared client helpers — every test here posts JSON to `/api/v1/auth/…`."""

    def setUp(self):
        super().setUp()
        # Deterministic, distinct codes, learned at the source. Nothing in
        # the suite depends on the passcode being readable from outside.
        self.issued_codes = count(100000)
        self.last_code = None
        patcher = patch("api.otp.get_random_string", side_effect=self._next_code)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _next_code(self, *args, **kwargs) -> str:
        self.last_code = str(next(self.issued_codes))
        return self.last_code

    def post(self, path: str, body: dict, **extra):
        return self.client.post(
            f"/api/v1/auth/{path}",
            data=body,
            content_type="application/json",
            **extra,
        )

    def request_code(self, phone: str = PHONE, **extra) -> str:
        """Ask for a code and return the one that was generated."""
        response = self.post("otp/request", {"phone": phone}, **extra)
        self.assertEqual(response.status_code, 200, response.content)
        return self.last_code

    def make_org(self, *, phone: str | None = None, **kwargs) -> User:
        """An existing organization and its owner."""
        owner = User.objects.create_user(
            username=kwargs.pop("username", "owner"),
            phone=phone,
            **kwargs,
        )
        org = Organization.objects.create(name="Existing Org", owner=owner)
        owner.organization = org
        owner.save(update_fields=["organization"])
        return owner


@DEVELOPMENT
class OtpRequestTests(AuthTestCase):
    def test_sends_a_code_and_reports_no_account(self):
        response = self.post("otp/request", {"phone": PHONE})

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertFalse(body["account_exists"])
        self.assertEqual(body["retry_after"], 60)
        self.assertEqual(PhoneOtp.objects.filter(phone=PHONE).count(), 1)
        # Requesting a code must never create the account (§0.2).
        self.assertFalse(User.objects.filter(phone=PHONE).exists())

    def test_code_is_stored_hashed(self):
        code = self.request_code()
        otp = PhoneOtp.objects.get(phone=PHONE)

        self.assertNotIn(code, otp.code_hash)
        self.assertTrue(otp.code_hash.startswith("pbkdf2_"))

    def test_account_exists_for_a_known_number(self):
        self.make_org(phone=PHONE)

        response = self.post("otp/request", {"phone": PHONE})

        self.assertTrue(response.json()["account_exists"])

    def test_deactivated_account_still_counts_as_existing(self):
        """
        Otherwise the app sends them to create an organization, which then
        fails on a phone number that is already taken.
        """
        owner = self.make_org(phone=PHONE)
        User.objects.filter(pk=owner.pk).update(is_active=False)

        self.assertTrue(self.post("otp/request", {"phone": PHONE}).json()["account_exists"])

    def test_phone_is_normalized_before_anything_else(self):
        """`+998 90 123-45-67` and `+998901234567` are one number, one bucket."""
        self.post("otp/request", {"phone": "+998 90 123-45-67"})

        self.assertEqual(PhoneOtp.objects.filter(phone=PHONE).count(), 1)
        # Same number, so the cooldown below applies to the formatted form too.
        self.assertEqual(self.post("otp/request", {"phone": PHONE}).status_code, 429)

    def test_blank_phone_is_rejected(self):
        self.assertEqual(self.post("otp/request", {"phone": "  "}).status_code, 400)

    def test_resend_is_refused_until_the_cooldown_passes(self):
        self.request_code()

        response = self.post("otp/request", {"phone": PHONE})

        self.assertEqual(response.status_code, 429)
        self.assertLessEqual(response.json()["retry_after"], 60)
        self.assertEqual(PhoneOtp.objects.count(), 1)

    def test_resend_is_allowed_once_the_cooldown_expires(self):
        self.request_code()
        PhoneOtp.objects.update(created_at=timezone.now() - timedelta(seconds=61))

        self.assertEqual(self.post("otp/request", {"phone": PHONE}).status_code, 200)
        self.assertEqual(PhoneOtp.objects.count(), 2)

    @override_settings(OTP_MAX_REQUESTS_PER_PHONE_PER_HOUR=2)
    def test_hourly_cap_per_phone(self):
        for _ in range(2):
            self.post("otp/request", {"phone": PHONE})
            # Step past the cooldown so the *hourly* cap is what bites.
            PhoneOtp.objects.update(created_at=timezone.now() - timedelta(seconds=61))

        response = self.post("otp/request", {"phone": PHONE})

        self.assertEqual(response.status_code, 429)
        self.assertIn("this number", response.json()["detail"])
        self.assertEqual(PhoneOtp.objects.count(), 2)

    @override_settings(OTP_MAX_REQUESTS_PER_IP_PER_HOUR=2)
    def test_hourly_cap_per_ip_spans_different_numbers(self):
        """A number-per-request attacker is still one address."""
        for index in range(2):
            self.post("otp/request", {"phone": f"+99890000000{index}"})

        response = self.post("otp/request", {"phone": "+998900000009"})

        self.assertEqual(response.status_code, 429)
        self.assertIn("this address", response.json()["detail"])

    def test_forged_forwarded_for_does_not_break_the_request(self):
        """`X-Forwarded-For` is client-controlled; `PhoneOtp.ip` is an inet."""
        response = self.post(
            "otp/request", {"phone": PHONE}, HTTP_X_FORWARDED_FOR="not-an-ip"
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(PhoneOtp.objects.get().ip)

    @override_settings(OTP_TEST_PHONES=[PHONE], OTP_DEV_CODE="424242")
    def test_whitelisted_test_number_gets_the_fixed_dev_code(self):
        response = self.post("otp/request", {"phone": PHONE})

        # Disclosed deliberately: the code is a constant the operator chose,
        # and listing a number is their statement that they control it.
        self.assertEqual(response.json()["debug_code"], "424242")
        self.assertIsNone(self.last_code, "a whitelisted number skips the generator")

        verify = self.post("otp/verify", {"phone": PHONE, "code": "424242"})
        self.assertEqual(verify.json()["status"], "no_account")


@DEVELOPMENT
class OtpDisclosureTests(AuthTestCase):
    """
    Where a passcode is allowed to appear.

    A live code is a bearer credential: whoever reads it can post it to the
    public `/auth/otp/verify` and walk away with that person's tokens. So it
    may reach exactly one place — the phone it was sent to — with two
    deliberate, non-production exceptions, both tested here.
    """

    def log_of_one_request(self, phone: str = PHONE) -> str:
        with self.assertLogs("api.sms", level="WARNING") as captured:
            self.post("otp/request", {"phone": phone})
        return "\n".join(captured.output)

    def test_response_never_carries_a_live_code(self):
        """Not even with the echo switch on — that one writes to the log."""
        with override_settings(OTP_ECHO_CODES=True):
            response = self.post("otp/request", {"phone": PHONE})

        self.assertIsNone(response.json()["debug_code"])

    def test_a_registered_users_code_is_not_handed_to_the_caller(self):
        """
        The takeover this closes: request a code for someone else's number,
        read it out of the response, exchange it for their tokens.
        """
        self.make_org(phone=PHONE)

        response = self.post("otp/request", {"phone": PHONE})

        self.assertIsNone(response.json()["debug_code"])

    def test_the_gateway_log_carries_neither_the_code_nor_the_number(self):
        output = self.log_of_one_request()

        self.assertNotIn(self.last_code, output)
        self.assertNotIn(PHONE, output)
        self.assertIn(mask_phone(PHONE), output)

    def test_codes_are_not_logged_by_default(self):
        output = self.log_of_one_request()

        self.assertNotIn(self.last_code, output)

    @override_settings(OTP_ECHO_CODES=True)
    def test_echo_switch_writes_the_code_to_the_log(self):
        """The one supported way to sign in as a number you don't own."""
        output = self.log_of_one_request()

        self.assertIn(self.last_code, output)
        self.assertIn("never be set in production", output)

    @override_settings(OTP_ECHO_CODES=True, ENVIRONMENT="production")
    def test_echo_switch_is_refused_in_production(self):
        output = self.log_of_one_request()

        self.assertNotIn(self.last_code, output)

    @override_settings(
        OTP_TEST_PHONES=[PHONE], OTP_DEV_CODE="424242", ENVIRONMENT="production"
    )
    def test_test_phone_whitelist_is_ignored_in_production(self):
        response = self.post("otp/request", {"phone": PHONE})

        self.assertIsNone(response.json()["debug_code"])
        # And the predictable code isn't the one that was issued.
        verify = self.post("otp/verify", {"phone": PHONE, "code": "424242"})
        self.assertEqual(verify.status_code, 400)

    def test_masking_leaves_a_number_unreadable(self):
        masked = mask_phone(PHONE)

        self.assertEqual(masked, "+998*******67")
        self.assertEqual(len(masked), len(PHONE))
        self.assertEqual(mask_phone("12345"), "*****")


@DEVELOPMENT
class OtpVerifyTests(AuthTestCase):
    def test_unknown_number_gets_a_signup_token_and_no_account(self):
        code = self.request_code()

        response = self.post("otp/verify", {"phone": PHONE, "code": code})

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["status"], "no_account")
        self.assertTrue(body["signup_token"])
        self.assertIsNone(body["access_token"])
        self.assertFalse(User.objects.filter(phone=PHONE).exists())

    def test_known_number_is_logged_in(self):
        owner = self.make_org(phone=PHONE, full_name="Ali Valiyev")
        code = self.request_code()

        response = self.post("otp/verify", {"phone": PHONE, "code": code})

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["status"], "authenticated")
        self.assertIsNone(body["signup_token"])
        self.assertEqual(body["user"]["full_name"], "Ali Valiyev")
        # The owner reads back every permission code (§0.2).
        self.assertEqual(body["user"]["permissions"], list(OrgPermission.values))
        self.assertTrue(body["user"]["is_owner"])
        self.assertTrue(
            RefreshToken.objects.filter(user=owner, is_revoked=False).exists()
        )

    def test_issued_access_token_works(self):
        self.make_org(phone=PHONE)
        code = self.request_code()
        token = self.post("otp/verify", {"phone": PHONE, "code": code}).json()

        me = self.client.get(
            "/api/v1/members/me",
            HTTP_AUTHORIZATION=f"Bearer {token['access_token']}",
        )

        self.assertEqual(me.status_code, 200, me.content)
        self.assertEqual(me.json()["phone"], PHONE)

    def test_deactivated_account_cannot_log_in(self):
        owner = self.make_org(phone=PHONE)
        User.objects.filter(pk=owner.pk).update(is_active=False)
        code = self.request_code()

        response = self.post("otp/verify", {"phone": PHONE, "code": code})

        self.assertEqual(response.status_code, 403)

    def test_wrong_code_is_counted(self):
        self.request_code()

        response = self.post("otp/verify", {"phone": PHONE, "code": "999999"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Invalid code")
        self.assertEqual(PhoneOtp.objects.get().attempts, 1)

    @override_settings(OTP_MAX_ATTEMPTS=3)
    def test_code_is_retired_after_too_many_guesses(self):
        code = self.request_code()
        for _ in range(3):
            self.post("otp/verify", {"phone": PHONE, "code": "000001"})

        # Even the right code is now worthless — the guesser can't stumble on it.
        response = self.post("otp/verify", {"phone": PHONE, "code": code})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Too many incorrect attempts", response.json()["detail"])

    def test_expired_code_is_refused(self):
        code = self.request_code()
        PhoneOtp.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        response = self.post("otp/verify", {"phone": PHONE, "code": code})

        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.json()["detail"])

    def test_code_is_burnt_on_success(self):
        self.make_org(phone=PHONE)
        code = self.request_code()
        self.post("otp/verify", {"phone": PHONE, "code": code})

        response = self.post("otp/verify", {"phone": PHONE, "code": code})

        self.assertEqual(response.status_code, 400)

    def test_only_the_newest_code_is_accepted(self):
        """Pressing "resend" retires the code that didn't arrive."""
        first = self.request_code()
        PhoneOtp.objects.update(created_at=timezone.now() - timedelta(seconds=61))
        second = self.request_code()

        self.assertEqual(
            self.post("otp/verify", {"phone": PHONE, "code": first}).status_code, 400
        )
        self.assertEqual(
            self.post("otp/verify", {"phone": PHONE, "code": second}).status_code, 200
        )

    def test_verify_without_a_request_is_refused(self):
        response = self.post("otp/verify", {"phone": PHONE, "code": "123456"})

        self.assertEqual(response.status_code, 400)


@DEVELOPMENT
class OrgSignupTests(AuthTestCase):
    def signup_token(self, phone: str = PHONE) -> str:
        code = self.request_code(phone)
        response = self.post("otp/verify", {"phone": phone, "code": code})
        return response.json()["signup_token"]

    def signup(self, token: str, **overrides):
        body = {
            "signup_token": token,
            "full_name": "Ali Valiyev",
            "organization_name": "Agro Servis",
            **overrides,
        }
        return self.post("org/signup", body)

    def test_creates_the_organization_and_its_admin(self):
        region = Region.objects.create(name="Samarqand", code="UZ-SA")

        response = self.signup(self.signup_token(), region_id=str(region.public_id))

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["access_token"])

        user = User.objects.get(phone=PHONE)
        org = Organization.objects.get(owner=user)
        self.assertEqual(user.full_name, "Ali Valiyev")
        self.assertEqual(user.organization_id, org.pk)
        self.assertEqual(org.name, "Agro Servis")
        self.assertEqual(org.region_id, region.pk)
        self.assertEqual(org.entity_type, Organization.EntityType.INDIVIDUAL)
        self.assertFalse(org.is_verified)
        # OTP is the only credential this account has.
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.must_change_password)
        # Owner ⇒ every permission code, without any being stored (§0.2).
        self.assertEqual(user.permissions, [])
        self.assertEqual(body["user"]["permissions"], list(OrgPermission.values))

    def test_org_creation_is_audited(self):
        self.signup(self.signup_token())

        entry = ActivityLog.objects.get()
        self.assertEqual(entry.action, ActivityLog.Action.CREATED)
        self.assertEqual(entry.actor, User.objects.get(phone=PHONE))
        self.assertEqual(entry.changes["name"]["to"], "Agro Servis")
        self.assertEqual(entry.context["path"], "/api/v1/auth/org/signup")
        # No region was given (§6b, fix.txt) — `diff` only reports keys whose
        # value changed from `before.get(key)`, which is `None` for a fresh
        # row, and an unset FK snapshots to `None` too, so the key is absent
        # rather than present-and-null.
        self.assertNotIn("region", entry.changes)

    def test_org_creation_is_audited_with_the_region_when_one_is_given(self):
        """
        §6b — `ORG_AUDIT_FIELDS` gained `"region"` when it moved to
        `users/profile.py`, so it is shared with this creation row too.
        `to_jsonable` renders the FK through `str()`, i.e. `Region.__str__`,
        which is `"Name (CODE)"` — what a human reading history wants, not
        the public id.
        """
        region = Region.objects.create(name="Samarqand", code="UZ-SA")

        self.signup(self.signup_token(), region_id=str(region.public_id))

        entry = ActivityLog.objects.get()
        self.assertEqual(entry.changes["region"]["to"], "Samarqand (UZ-SA)")

    def test_signup_token_is_single_use(self):
        token = self.signup_token()
        self.assertEqual(self.signup(token).status_code, 200)

        second = self.signup(token, organization_name="Second Org")

        self.assertEqual(second.status_code, 401)
        self.assertEqual(Organization.objects.count(), 1)

    def test_a_forged_token_is_refused(self):
        self.assertEqual(self.signup("not-a-jwt").status_code, 401)

    def test_an_access_token_is_not_a_signup_token(self):
        """The `type` claim is what keeps the token families apart."""
        owner = self.make_org(phone="+998900000001")

        response = self.signup(create_access_token(owner.public_id))

        self.assertEqual(response.status_code, 401)

    def test_a_token_pointing_at_an_unverified_otp_is_refused(self):
        """Minting the JWT is not the proof — the burnt `PhoneOtp` row is."""
        self.request_code()
        otp = PhoneOtp.objects.get()

        response = self.signup(create_signup_token(PHONE, otp.public_id))

        self.assertEqual(response.status_code, 401)
        self.assertFalse(User.objects.filter(phone=PHONE).exists())

    def test_unknown_region_rolls_back_and_leaves_the_token_usable(self):
        token = self.signup_token()

        failed = self.signup(token, region_id="00000000-0000-0000-0000-000000000000")

        self.assertEqual(failed.status_code, 400)
        self.assertFalse(User.objects.filter(phone=PHONE).exists())
        # The token was not spent by the failed attempt.
        self.assertEqual(self.signup(token).status_code, 200)

    def test_organization_name_is_required(self):
        response = self.signup(self.signup_token(), organization_name="   ")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Organization.objects.exists())

    def test_legal_entity_can_be_declared_at_signup(self):
        response = self.signup(self.signup_token(), entity_type="legal_entity")

        self.assertEqual(response.status_code, 200, response.content)
        org = Organization.objects.get()
        self.assertEqual(org.entity_type, Organization.EntityType.LEGAL_ENTITY)
        # Declaring is not verifying — that needs ONEID (§0b).
        self.assertFalse(org.is_verified)

    def test_unknown_entity_type_is_refused(self):
        response = self.signup(self.signup_token(), entity_type="cooperative")

        self.assertEqual(response.status_code, 400)

    def test_public_password_signup_is_gone(self):
        """§0.2 — the only public account-creating endpoint is org/signup."""
        response = self.post(
            "signup",
            {"username": "x", "email": "x@example.com", "password": "sekret123"},
        )

        self.assertEqual(response.status_code, 404)


class LoginTests(AuthTestCase):
    """Email + password stays, and now answers to a phone number too (§0.4)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="alisher",
            email="Alisher@Example.com",
            password="sekret-password-1",
            phone=PHONE,
        )
        org = Organization.objects.create(name="Org", owner=cls.user)
        cls.user.organization = org
        cls.user.save(update_fields=["organization"])

    def login(self, username: str, password: str = "sekret-password-1"):
        return self.post("login", {"username": username, "password": password})

    def test_phone_is_accepted_as_an_identifier(self):
        response = self.login(PHONE)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["user"]["username"], "alisher")

    def test_formatted_phone_is_accepted(self):
        self.assertEqual(self.login("+998 90 123 45 67").status_code, 200)

    def test_email_and_username_still_work(self):
        self.assertEqual(self.login("alisher@example.com").status_code, 200)
        self.assertEqual(self.login("alisher").status_code, 200)

    def test_wrong_password_is_401(self):
        self.assertEqual(self.login(PHONE, "wrong").status_code, 401)

    def test_unknown_phone_is_401(self):
        self.assertEqual(self.login("+998900000000").status_code, 401)


class LogoutTests(AuthTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="alisher", password="sekret-password-1", phone=PHONE
        )
        org = Organization.objects.create(name="Org", owner=cls.user)
        cls.user.organization = org
        cls.user.save(update_fields=["organization"])

    def session(self) -> str:
        response = self.post(
            "login", {"username": "alisher", "password": "sekret-password-1"}
        )
        return response.json()["refresh_token"]

    def test_revokes_the_presented_token(self):
        refresh = self.session()

        response = self.post("logout", {"refresh_token": refresh}, **auth(self.user))

        self.assertEqual(response.status_code, 204)
        self.assertTrue(RefreshToken.objects.get(token=refresh).is_revoked)
        # The session can no longer be extended.
        self.assertEqual(
            self.post("token/refresh", {"refresh_token": refresh}).status_code, 401
        )

    def test_leaves_other_sessions_alone(self):
        first, second = self.session(), self.session()

        self.post("logout", {"refresh_token": first}, **auth(self.user))

        self.assertFalse(RefreshToken.objects.get(token=second).is_revoked)

    def test_cannot_revoke_someone_elses_session(self):
        refresh = self.session()
        stranger = User.objects.create_user(username="stranger")

        response = self.post(
            "logout", {"refresh_token": refresh}, **auth(stranger)
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(RefreshToken.objects.get(token=refresh).is_revoked)

    def test_requires_authentication(self):
        response = self.post("logout", {"refresh_token": self.session()})

        self.assertEqual(response.status_code, 401)
