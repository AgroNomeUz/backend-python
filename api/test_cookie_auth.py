"""
api/test_cookie_auth.py
The opt-in cookie transport for the refresh token (api/cookies.py), and the
sign-in stamp the auth endpoints now leave on the user.
"""

from django.test import TestCase, override_settings

from api.auth import create_access_token
from api.models import RefreshToken
from users.models import Organization, User

COOKIE = "agro_refresh"
CONSOLE = "https://console.agronome.uz"
COOKIE_MODE = {"HTTP_X_AUTH_TRANSPORT": "cookie"}


@override_settings(CORS_ALLOWED_ORIGINS=[CONSOLE], AUTH_REFRESH_COOKIE_DOMAIN=".agronome.uz")
class CookieTransportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="owner", password="a-long-password-1", phone="+998901112233"
        )
        org = Organization.objects.create(name="Org", owner=self.user)
        self.user.organization = org
        self.user.save(update_fields=["organization"])

    def post(self, path, body=None, **extra):
        return self.client.post(
            f"/api/v1/auth/{path}",
            data=body if body is not None else {},
            content_type="application/json",
            **extra,
        )

    def login(self, **extra):
        return self.post(
            "login", {"username": "owner", "password": "a-long-password-1"}, **extra
        )

    # ── issuing ──────────────────────────────────────────────────────────────

    def test_without_the_header_nothing_changes(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["refresh_token"])
        self.assertNotIn(COOKIE, response.cookies)

    def test_cookie_mode_sets_an_httponly_cookie_and_hides_the_token(self):
        response = self.login(**COOKIE_MODE)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["refresh_token"])
        self.assertTrue(body["access_token"])

        cookie = response.cookies[COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["domain"], ".agronome.uz")
        self.assertEqual(cookie["path"], "/api/v1/auth/")
        self.assertTrue(RefreshToken.objects.filter(token=cookie.value).exists())

    # ── refreshing ───────────────────────────────────────────────────────────

    def test_refresh_reads_the_cookie_and_rotates_it(self):
        old = self.login(**COOKIE_MODE).cookies[COOKIE].value

        response = self.post("token/refresh", HTTP_ORIGIN=CONSOLE, **COOKIE_MODE)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()["refresh_token"])
        new = response.cookies[COOKIE].value
        self.assertNotEqual(new, old)
        self.assertTrue(RefreshToken.objects.get(token=old).is_revoked)

    def test_cookie_is_ignored_without_the_header(self):
        self.login(**COOKIE_MODE)
        response = self.post("token/refresh")
        self.assertEqual(response.status_code, 401)

    def test_refresh_from_an_untrusted_origin_is_refused(self):
        self.login(**COOKIE_MODE)
        response = self.post(
            "token/refresh", HTTP_ORIGIN="https://evil.example", **COOKIE_MODE
        )
        self.assertEqual(response.status_code, 403)

    def test_a_revoked_cookie_is_cleared(self):
        token = self.login(**COOKIE_MODE).cookies[COOKIE].value
        RefreshToken.objects.filter(token=token).update(is_revoked=True)

        response = self.post("token/refresh", **COOKIE_MODE)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.cookies[COOKIE].value, "")

    def test_body_token_still_works(self):
        token = self.login().json()["refresh_token"]
        response = self.post("token/refresh", {"refresh_token": token})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["refresh_token"])

    def test_refresh_for_a_deactivated_user_is_refused(self):
        token = self.login().json()["refresh_token"]
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        response = self.post("token/refresh", {"refresh_token": token})
        self.assertEqual(response.status_code, 401)

    # ── logging out ──────────────────────────────────────────────────────────

    def test_logout_revokes_and_clears_the_cookie(self):
        response = self.login(**COOKIE_MODE)
        token = response.cookies[COOKIE].value
        access = response.json()["access_token"]

        response = self.post(
            "logout", HTTP_AUTHORIZATION=f"Bearer {access}", **COOKIE_MODE
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.cookies[COOKIE].value, "")
        self.assertTrue(RefreshToken.objects.get(token=token).is_revoked)


class SignInStampTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", password="a-long-password-1")

    def test_password_login_stamps_last_login(self):
        self.client.post(
            "/api/v1/auth/login",
            {"username": "u", "password": "a-long-password-1"},
            content_type="application/json",
        )
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login)
        self.assertEqual(self.user.last_login_method, "password")

    def test_refresh_is_not_a_sign_in(self):
        token = self.client.post(
            "/api/v1/auth/login",
            {"username": "u", "password": "a-long-password-1"},
            content_type="application/json",
        ).json()["refresh_token"]
        self.user.refresh_from_db()
        first = self.user.last_login

        self.client.post(
            "/api/v1/auth/token/refresh",
            {"refresh_token": token},
            content_type="application/json",
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_login, first)


def bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}
