"""Input that used to reach Postgres and come back as a 500."""

from core.test_support import auth

from .tests import ProfileTestCase


class BadInputTests(ProfileTestCase):
    def get(self, path, query):
        return self.client.get(f"{path}?{query}", **auth(self.member))

    def test_non_finite_numbers_are_422(self):
        for path in ("/api/v1/public/listings", "/api/v1/listings", "/api/v1/catalog/equipment-models"):
            for query in ("min_power_kw=inf", "max_power_kw=nan"):
                with self.subTest(path=path, query=query):
                    self.assertEqual(self.get(path, query).status_code, 422)
        for query in ("min_price=inf", "max_price=nan"):
            with self.subTest(query=query):
                for path in ("/api/v1/listings", "/api/v1/market/offers"):
                    self.assertEqual(self.get(path, query).status_code, 422)

    def test_nul_in_a_query_string_is_400(self):
        self.assertEqual(self.get("/api/v1/public/listings", "search=a%00b").status_code, 400)

    def test_offset_beyond_bigint_is_400(self):
        response = self.get("/api/v1/public/listings", "offset=99999999999999999999")
        self.assertEqual(response.status_code, 400)

    def patch(self, path, user, body):
        return self.client.patch(
            path, data=body, content_type="application/json", **auth(user)
        )

    def test_nul_in_a_json_string_is_400(self):
        self.assertEqual(self.patch("/api/v1/org", self.owner, {"name": "x\u0000y"}).status_code, 400)

    def test_org_phone_that_normalizes_to_nothing_is_400(self):
        self.assertEqual(self.patch("/api/v1/org", self.owner, {"phone": "abc"}).status_code, 400)

    def test_null_on_a_required_member_field_is_400(self):
        for name in ("full_name", "telegram", "is_active"):
            with self.subTest(name=name):
                response = self.patch(
                    f"/api/v1/members/{self.member.public_id}", self.admin, {name: None}
                )
                self.assertEqual(response.status_code, 400)
