"""
users/test_iam.py
Role presets (users/presets.py), the `/iam` read endpoints, and the extra
member fields the console's user list shows.
"""

from django.test import TestCase

from api.auth import create_access_token
from core.models import ActivityLog
from users.models import VIEW_PERMISSIONS, OrgPermission, Organization, User

P = OrgPermission


def auth(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}


class IamTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner")
        cls.org = Organization.objects.create(name="Org", owner=cls.owner)
        cls.owner.organization = cls.org
        cls.owner.save(update_fields=["organization"])

        def member(name, perms, **kw):
            return User.objects.create_user(
                username=name, organization=cls.org, permissions=sorted(perms), **kw
            )

        cls.fleet = member("fleet", [P.MANAGE_EQUIPMENT, *VIEW_PERMISSIONS])
        cls.sales = member(
            "sales", [P.MANAGE_EQUIPMENT, P.MANAGE_INQUIRIES, *VIEW_PERMISSIONS]
        )
        cls.viewer = member("viewer", VIEW_PERMISSIONS)
        cls.blind = member("blind", [])
        cls.custom = member("custom", [P.MANAGE_USERS])
        cls.gone = member(
            "gone", [P.MANAGE_EQUIPMENT, *VIEW_PERMISSIONS], is_active=False
        )

        other_owner = User.objects.create_user(username="other")
        other = Organization.objects.create(name="Other", owner=other_owner)
        User.objects.create_user(username="x", organization=other, permissions=[])

    def get(self, path, user=None):
        return self.client.get(f"/api/v1/{path}", **auth(user or self.viewer))

    def test_permission_catalogue_is_grouped_by_service(self):
        rows = self.get("iam/permissions").json()
        by_code = {r["code"]: r for r in rows}
        self.assertEqual(set(by_code), set(P.values))
        self.assertEqual(by_code["equipment.manage"]["service"], "equipment")
        self.assertEqual(by_code["users.manage"]["service_label"], "IAM")

    def test_presets_count_exact_matches_among_active_staff_only(self):
        rows = {r["id"]: r for r in self.get("iam/presets").json()}
        self.assertEqual(rows["fleet_manager"]["member_count"], 1)  # `gone` is inactive
        self.assertEqual(rows["sales"]["member_count"], 1)
        self.assertEqual(rows["viewer"]["member_count"], 1)  # other org not counted
        self.assertEqual(rows["org_admin"]["member_count"], 0)  # owner never counted

    def test_member_rows_carry_the_matched_preset(self):
        items = {m["username"]: m for m in self.get("members?limit=50").json()["items"]}
        self.assertEqual(items["owner"]["preset"], "owner")
        self.assertEqual(items["fleet"]["preset"], "fleet_manager")
        self.assertEqual(items["sales"]["preset"], "sales")
        self.assertIsNone(items["custom"]["preset"])

    def test_preset_filter(self):
        def names(q):
            return {m["username"] for m in self.get(f"members?preset={q}").json()["items"]}

        self.assertEqual(names("owner"), {"owner"})
        self.assertEqual(names("fleet_manager"), {"fleet", "gone"})
        self.assertEqual(names("custom"), {"custom", "blind"})
        self.assertEqual(self.get("members?preset=nope").status_code, 400)

    def test_last_activity_is_the_newest_row_the_member_acted_in(self):
        ActivityLog.record(
            organization=self.org, actor=self.sales,
            action=ActivityLog.Action.UPDATED, target=self.sales,
        )
        items = {m["username"]: m for m in self.get("members").json()["items"]}
        self.assertIsNotNone(items["sales"]["last_activity_at"])
        self.assertIsNone(items["fleet"]["last_activity_at"])
        self.assertEqual(items["fleet"]["last_login_method"], "")
