"""
users/test_view_permissions.py
`*.view` permissions: reading an organization's private data is gated, per
area, the way writing already was.

A request for a random id answers 403 for a member without the view code and
404 for one with it, so no fixtures are needed to prove the gate sits before
the lookup. The areas' own test modules cover what the data looks like.
"""

import uuid
from importlib import import_module

from django.apps import apps
from django.test import TestCase

from api.auth import create_access_token
from users.models import VIEW_PERMISSIONS, OrgPermission, Organization, User

P = OrgPermission
MISSING = uuid.uuid4()


def auth(user: User) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}


# area view code -> GETs on private data in that area
GATED = {
    P.VIEW_EQUIPMENT: [
        "assets",
        f"assets/{MISSING}",
        f"assets/{MISSING}/activity",
        "listings/my",
    ],
    P.VIEW_USERS: [
        "members",
        f"members/{MISSING}",
        f"members/{MISSING}/activity",
        "iam/permissions",
        "iam/presets",
    ],
    P.VIEW_INQUIRIES: [
        "inquiries/received",
        "inquiries/sent",
        f"inquiries/{MISSING}",
        f"inquiries/{MISSING}/messages",
    ],
    P.VIEW_FARMS: [
        "farms",
        f"farms/{MISSING}",
        f"farms/{MISSING}/activity",
        f"farms/{MISSING}/external-references",
        "farm-clusters",
        f"farm-clusters/{MISSING}",
        "seasons",
        f"seasons/{MISSING}",
        "fields",
        f"fields/{MISSING}",
        f"fields/{MISSING}/boundaries",
        f"fields/{MISSING}/activity",
        "crop-zones",
        f"crop-zones/{MISSING}",
        f"crop-zones/{MISSING}/activity",
    ],
    P.VIEW_ACTIVITY: ["activity"],
}

MANAGE_FOR = {
    P.VIEW_EQUIPMENT: P.MANAGE_EQUIPMENT,
    P.VIEW_USERS: P.MANAGE_USERS,
    P.VIEW_INQUIRIES: P.MANAGE_INQUIRIES,
    P.VIEW_FARMS: P.MANAGE_FARMS,
}


class ViewPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="vp-owner")
        cls.org = Organization.objects.create(name="Org", owner=cls.owner)
        cls.owner.organization = cls.org
        cls.owner.save(update_fields=["organization"])

    def member(self, *codes) -> User:
        return User.objects.create_user(
            username=f"vp-{uuid.uuid4().hex[:8]}",
            organization=self.org,
            permissions=sorted(c.value for c in codes),
        )

    def status(self, path, user) -> int:
        return self.client.get(f"/api/v1/{path}", **auth(user)).status_code

    def test_every_view_code_has_gated_endpoints(self):
        self.assertEqual(set(GATED), {P(code) for code in VIEW_PERMISSIONS})

    def test_a_member_with_no_codes_sees_nothing_private(self):
        blind = self.member()
        for paths in GATED.values():
            for path in paths:
                self.assertEqual(self.status(path, blind), 403, path)

    def test_the_view_code_opens_its_own_area_only(self):
        for code, paths in GATED.items():
            member = self.member(code)
            for other, other_paths in GATED.items():
                for path in other_paths:
                    expected = 404 if other == code else 403
                    got = self.status(path, member)
                    if got == 200:  # list endpoints answer 200, not 404
                        got = 404
                    self.assertEqual(got, expected, (code.value, path))

    def test_manage_implies_view_in_its_area(self):
        for view, manage in MANAGE_FOR.items():
            member = self.member(manage)
            for path in GATED[view]:
                self.assertNotEqual(self.status(path, member), 403, (manage, path))

    def test_manage_does_not_imply_activity_view(self):
        everything = self.member(*MANAGE_FOR.values())
        self.assertEqual(self.status("activity", everything), 403)

    def test_the_owner_sees_everything(self):
        for paths in GATED.values():
            for path in paths:
                self.assertNotEqual(self.status(path, self.owner), 403, path)

    def test_what_stays_open(self):
        blind = self.member()
        self.assertEqual(self.status("members/me", blind), 200)
        self.assertEqual(self.status("users/me", blind), 200)
        self.assertEqual(self.status("org", blind), 200)
        self.assertEqual(self.status("favorites", blind), 200)
        self.assertEqual(self.status("catalog/categories", blind), 200)
        self.assertEqual(self.status("listings", blind), 200)

    def test_effective_permissions_include_the_implied_views(self):
        member = self.member(P.MANAGE_FARMS)
        me = self.client.get("/api/v1/users/me", **auth(member)).json()
        self.assertEqual(me["permissions"], ["farms.manage", "farms.view"])
        row = self.client.get("/api/v1/members/me", **auth(member)).json()
        self.assertEqual(row["permissions"], ["farms.manage", "farms.view"])

    def test_the_permission_catalogue_lists_the_view_codes(self):
        reader = self.member(P.VIEW_USERS)
        codes = {r["code"] for r in self.client.get("/api/v1/iam/permissions", **auth(reader)).json()}
        self.assertTrue(set(VIEW_PERMISSIONS) <= codes)

    def test_a_draft_listing_is_hidden_from_a_member_without_equipment_view(self):
        from equipment.models import Asset, EquipmentCategory, EquipmentModel, Manufacturer
        from listings.models import Listing

        model = EquipmentModel.objects.create(
            manufacturer=Manufacturer.objects.create(name="MTZ"),
            category=EquipmentCategory.objects.create(name="Tractor", slug="tractor"),
            name="82.1",
        )
        asset = Asset.objects.create(organization=self.org, equipment_model=model)
        draft = Listing.objects.create(
            organization=self.org,
            asset=asset,
            created_by=self.owner,
            listing_type=Listing.ListingType.RENT,
            status=Listing.Status.DRAFT,
            title="Draft",
            price="1.00",
        )
        url = f"/api/v1/listings/{draft.public_id}"
        self.assertEqual(self.client.get(url, **auth(self.member())).status_code, 404)
        self.assertEqual(
            self.client.get(url, **auth(self.member(P.VIEW_EQUIPMENT))).status_code, 200
        )

    def test_the_dashboard_blanks_what_the_caller_may_not_see(self):
        def stats(*codes):
            return self.client.get("/api/v1/stats/owner", **auth(self.member(*codes))).json()

        blind = stats()
        for key in (
            "active_listings",
            "listings_by_status",
            "views",
            "unread_inquiries",
            "inquiries_received",
            "inquiries_sent",
            "members",
        ):
            self.assertIsNone(blind[key], key)

        self.assertIsNotNone(stats(P.VIEW_EQUIPMENT)["active_listings"])
        self.assertIsNone(stats(P.VIEW_EQUIPMENT)["unread_inquiries"])
        self.assertIsNotNone(stats(P.VIEW_INQUIRIES)["unread_inquiries"])
        self.assertIsNotNone(stats(P.VIEW_USERS)["members"])

    def test_the_backfill_gives_existing_staff_the_view_codes_but_not_owners(self):
        migration = import_module("users.migrations.0013_view_permissions")
        old = self.member(P.MANAGE_EQUIPMENT)
        User.objects.filter(pk=old.pk).update(permissions=["equipment.manage"])
        migration.grant_views_to_existing_members(apps, None)
        old.refresh_from_db()
        self.assertEqual(
            old.permissions, sorted(["equipment.manage", *VIEW_PERMISSIONS])
        )
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.permissions, [])
