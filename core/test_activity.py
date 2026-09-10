"""
core/test_activity.py
`GET /activity`, and the two per-object histories that share its queryset (§8).

The filters are the point of this module, and what they are worth is decided
by how they *fail*. A filter the server cannot make sense of — an unknown
action, a `target_id` with no `target_type` — has to be a 400, because an
empty page would read as "nothing ever happened", which is the one answer an
audit log must never give wrongly. A filter that is well-formed but matches
nothing — another organization's actor, an id that was never here — has to be
an empty page, because a 404 would confirm what §0.1 says must not be
confirmed. Most of what follows is one or the other of those two cases.

The rows are produced by calling the real endpoints wherever possible. A
fixture that wrote `ActivityLog` rows by hand would still pass if `/listings`
stopped logging, and §0.3's whole claim is that it does not.
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from api.auth import create_access_token
from core.models import ActivityLog
from equipment.models import Asset, EquipmentCategory, EquipmentModel, Manufacturer
from listings.models import Listing
from users.models import OrgPermission, Organization, Region, User

ACTIVITY = "/api/v1/activity"


def auth(user: User) -> dict:
    """Request kwargs carrying a bearer token for `user`."""
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}


class ActivityTestCase(TestCase):
    """
    One organization with three people in it, and a rival with its own history.

    Three, because the interesting questions all need more than two: the
    filter has to tell the owner's rows from a colleague's, and the access
    rule has to be checked by somebody holding no permission codes at all.
    """

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(
            name="Andijan Activity", code="ANA", soato="1706"
        )

        cls.owner = User.objects.create_user(
            username="activity-owner", password="x", full_name="Alisher Karimov"
        )
        cls.org = Organization.objects.create(
            name="Activity Org", owner=cls.owner, region=cls.region
        )
        cls.owner.organization = cls.org
        cls.owner.save(update_fields=["organization"])

        cls.manager = User.objects.create_user(
            username="activity-manager",
            password="x",
            organization=cls.org,
            full_name="Dilnoza Rahimova",
            permissions=[OrgPermission.MANAGE_EQUIPMENT, OrgPermission.MANAGE_USERS],
        )
        # No codes, and no `full_name` either — one account proving both that
        # reads are ungated (§0.2) and that `actor_name` falls back.
        cls.plain = User.objects.create_user(
            username="activity-plain", password="x", organization=cls.org
        )

        cls.rival_owner = User.objects.create_user(
            username="activity-rival", password="x", full_name="Rival Owner"
        )
        cls.rival_org = Organization.objects.create(
            name="Rival Activity Org", owner=cls.rival_owner, region=cls.region
        )
        cls.rival_owner.organization = cls.rival_org
        cls.rival_owner.save(update_fields=["organization"])

        manufacturer = Manufacturer.objects.create(name="MTZ")
        category = EquipmentCategory.objects.create(name="Tractor", slug="tractor")
        cls.equipment_model = EquipmentModel.objects.create(
            manufacturer=manufacturer, category=category, name="82.1"
        )
        cls.asset = Asset.objects.create(
            organization=cls.org, equipment_model=cls.equipment_model
        )
        cls.rival_asset = Asset.objects.create(
            organization=cls.rival_org, equipment_model=cls.equipment_model
        )

    def make_listing(self, user=None, **overrides):
        """Publish a listing through the API, so a real audit row is written."""
        payload = {
            "asset_id": str(self.asset.public_id),
            "listing_type": "rent",
            "status": "active",
            "title": "MTZ-82 tractor with operator",
            "price": "400000.00",
            "price_unit": "day",
        }
        payload.update(overrides)
        response = self.client.post(
            "/api/v1/listings",
            data=payload,
            content_type="application/json",
            **auth(user or self.owner),
        )
        self.assertEqual(response.status_code, 201, response.content)
        return Listing.objects.get(public_id=response.json()["id"])

    def rows(self, user=None, **params):
        response = self.client.get(ACTIVITY, params, **auth(user or self.plain))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["items"]


class ActivityAccessTests(ActivityTestCase):
    """Who may read the history — the question §8 left open."""

    def test_anonymous_is_401(self):
        self.assertEqual(self.client.get(ACTIVITY).status_code, 401)

    def test_an_account_with_no_organization_is_403(self):
        stray = User.objects.create_user(username="activity-stray", password="x")
        self.assertEqual(self.client.get(ACTIVITY, **auth(stray)).status_code, 403)

    def test_a_member_with_no_codes_reads_the_whole_org_history(self):
        """
        The decision §8 asked for: the whole organization, not just your own
        rows. Reads are ungated (§0.2), and a member seeing that a colleague
        archived the listing they published is the feature, not a leak.
        """
        self.make_listing(user=self.owner)
        actors = {row["actor_username"] for row in self.rows(self.plain)}
        self.assertEqual(actors, {"activity-owner"})

    def test_another_organizations_history_is_invisible(self):
        self.client.post(
            "/api/v1/listings",
            data={
                "asset_id": str(self.rival_asset.public_id),
                "listing_type": "rent",
                "status": "active",
                "title": "Rival tractor",
                "price": "1.00",
                "price_unit": "day",
            },
            content_type="application/json",
            **auth(self.rival_owner),
        )
        self.assertEqual(self.rows(self.plain), [])
        self.assertEqual(len(self.rows(self.rival_owner)), 1)


class ActivityPayloadTests(ActivityTestCase):
    """What one row says."""

    def test_a_listing_write_is_logged_with_its_diff(self):
        listing = self.make_listing()
        row = self.rows()[0]
        self.assertEqual(row["action"], "created")
        self.assertEqual(row["target_type"], "listing")
        self.assertEqual(row["target_id"], str(listing.public_id))
        self.assertEqual(row["target_repr"], str(listing))
        self.assertEqual(row["changes"]["title"]["to"], listing.title)

    def test_actor_name_prefers_the_persons_name(self):
        self.make_listing(user=self.owner)
        row = self.rows()[0]
        self.assertEqual(row["actor_name"], "Alisher Karimov")
        self.assertEqual(row["actor_username"], "activity-owner")

    def test_actor_name_falls_back_to_the_username(self):
        ActivityLog.record(
            organization=self.org,
            actor=self.plain,
            action=ActivityLog.Action.UPDATED,
            target=self.asset,
        )
        self.assertEqual(self.rows()[0]["actor_name"], "activity-plain")

    def test_a_row_outlives_its_target(self):
        """
        `target_repr` is the whole reason the log survives a deletion — and
        `target_id` goes null rather than pointing at nothing.
        """
        listing = self.make_listing()
        label = str(listing)
        listing.delete()

        row = next(r for r in self.rows() if r["target_type"] == "listing")
        self.assertEqual(row["target_repr"], label)
        self.assertIsNone(row["target_id"])

    def test_a_page_of_mixed_target_types_serialises(self):
        """
        The generic FK is prefetched, not lazily loaded: in an async endpoint
        a lazy relation is a 500 at serialisation time, not an N+1.
        """
        self.make_listing()
        self.client.post(
            "/api/v1/members",
            data={"email": "new-hire@example.com", "full_name": "New Hire"},
            content_type="application/json",
            **auth(self.manager),
        )
        types = {row["target_type"] for row in self.rows()}
        self.assertEqual(types, {"listing", "user"})


class ActivityFilterTests(ActivityTestCase):
    """The five filters, and how each one fails."""

    def setUp(self):
        super().setUp()
        self.listing = self.make_listing(user=self.owner)
        self.client.patch(
            f"/api/v1/listings/{self.listing.public_id}",
            data={"status": "paused"},
            content_type="application/json",
            **auth(self.manager),
        )

    def test_action(self):
        self.assertEqual(len(self.rows(action="created")), 1)
        self.assertEqual(len(self.rows(action="status_changed")), 1)

    def test_an_unknown_action_is_a_400_not_an_empty_page(self):
        response = self.client.get(ACTIVITY, {"action": "deleted-ish"}, **auth(self.plain))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown action", response.json()["detail"])

    def test_target_type(self):
        self.assertEqual(len(self.rows(target_type="listing")), 2)
        self.assertEqual(self.rows(target_type="asset"), [])

    def test_an_unknown_target_type_is_a_400(self):
        response = self.client.get(
            ACTIVITY, {"target_type": "spaceship"}, **auth(self.plain)
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown target_type", response.json()["detail"])

    def test_target_id_gives_one_objects_history(self):
        other = self.make_listing(
            listing_type="sale", price_unit="total", title="Also for sale"
        )
        rows = self.rows(target_type="listing", target_id=str(self.listing.public_id))
        self.assertEqual(len(rows), 2)
        self.assertNotIn(
            str(other.public_id), {row["target_id"] for row in rows}
        )

    def test_target_id_without_target_type_is_a_400(self):
        response = self.client.get(
            ACTIVITY, {"target_id": str(self.listing.public_id)}, **auth(self.plain)
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("target_id needs target_type", response.json()["detail"])

    def test_an_unknown_target_id_is_an_empty_page(self):
        rows = self.rows(
            target_type="listing", target_id="00000000-0000-4000-8000-000000000000"
        )
        self.assertEqual(rows, [])

    def test_another_orgs_target_id_is_indistinguishable_from_a_missing_one(self):
        """
        §0.1: a wrong id must not confirm that the object exists. Both answers
        here are the same empty page.
        """
        rival_listing = Listing.objects.create(
            organization=self.rival_org,
            asset=self.rival_asset,
            created_by=self.rival_owner,
            listing_type=Listing.ListingType.RENT,
            status=Listing.Status.ACTIVE,
            title="Rival listing",
            price=Decimal("1.00"),
            price_unit=Listing.PriceUnit.DAY,
        )
        self.assertEqual(
            self.rows(target_type="listing", target_id=str(rival_listing.public_id)),
            self.rows(
                target_type="listing",
                target_id="00000000-0000-4000-8000-000000000000",
            ),
        )

    def test_actor(self):
        rows = self.rows(actor=str(self.manager.public_id))
        self.assertEqual({row["actor_username"] for row in rows}, {"activity-manager"})
        self.assertEqual(len(rows), 1)

    def test_an_actor_from_another_organization_is_an_empty_page(self):
        self.assertEqual(self.rows(actor=str(self.rival_owner.public_id)), [])

    def test_the_filters_combine(self):
        rows = self.rows(
            action="created",
            target_type="listing",
            actor=str(self.owner.public_id),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "created")


class ActivityDateFilterTests(ActivityTestCase):
    """
    The date range, which is the filter most likely to be off by a day.

    `created_at` is `auto_now_add`, so the ages are backdated with an UPDATE —
    the only way to get a row that claims to be from last week.
    """

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        for age in (0, 1, 5):
            log = ActivityLog.record(
                organization=self.org,
                actor=self.owner,
                action=ActivityLog.Action.UPDATED,
                target=self.asset,
                changes={"age": {"from": None, "to": age}},
            )
            ActivityLog.objects.filter(pk=log.pk).update(
                created_at=timezone.now() - timedelta(days=age)
            )

    def ages(self, **params):
        return sorted(row["changes"]["age"]["to"] for row in self.rows(**params))

    def test_date_from_is_inclusive_of_its_own_day(self):
        self.assertEqual(self.ages(date_from=str(self.today - timedelta(days=1))), [0, 1])

    def test_date_to_is_inclusive_of_its_own_day(self):
        """
        The end of the day, not midnight at the start of it — anything else
        silently drops everything that happened on the day the user asked for.
        """
        self.assertEqual(self.ages(date_to=str(self.today)), [0, 1, 5])
        self.assertEqual(self.ages(date_to=str(self.today - timedelta(days=1))), [1, 5])

    def test_a_range_takes_both_ends(self):
        self.assertEqual(
            self.ages(
                date_from=str(self.today - timedelta(days=5)),
                date_to=str(self.today - timedelta(days=1)),
            ),
            [1, 5],
        )

    def test_a_backwards_range_is_a_400(self):
        response = self.client.get(
            ACTIVITY,
            {"date_from": str(self.today), "date_to": str(self.today - timedelta(days=2))},
            **auth(self.plain),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("date_from is after date_to", response.json()["detail"])

    def test_a_future_range_is_empty_rather_than_an_error(self):
        self.assertEqual(self.ages(date_from=str(self.today + timedelta(days=1))), [])


class PerObjectHistoryTests(ActivityTestCase):
    """`/assets/{id}/activity` and `/members/{id}/activity`."""

    def test_an_assets_history_still_answers_after_the_move(self):
        self.client.patch(
            f"/api/v1/assets/{self.asset.public_id}",
            data={"notes": "Serviced"},
            content_type="application/json",
            **auth(self.manager),
        )
        response = self.client.get(
            f"/api/v1/assets/{self.asset.public_id}/activity", **auth(self.plain)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 1)

    def test_another_orgs_asset_is_a_404_there_but_an_empty_page_here(self):
        """
        Both are right, and the difference is what the second endpoint is for:
        `/assets/{id}` knows the caller is claiming the machine is theirs.
        """
        detail = self.client.get(
            f"/api/v1/assets/{self.rival_asset.public_id}/activity", **auth(self.plain)
        )
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(
            self.rows(target_type="asset", target_id=str(self.rival_asset.public_id)),
            [],
        )

    def member_rows(self, member, caller=None):
        response = self.client.get(
            f"/api/v1/members/{member.public_id}/activity", **auth(caller or self.plain)
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["items"]

    def test_a_members_history_is_what_they_did(self):
        self.make_listing(user=self.owner)
        self.make_listing(
            user=self.manager,
            listing_type="sale",
            price_unit="total",
            title="Owner's other offer",
        )

        self.assertEqual(
            {row["actor_username"] for row in self.member_rows(self.manager)},
            {"activity-manager"},
        )
        self.assertEqual(len(self.member_rows(self.owner)), 1)

    def test_it_is_not_what_was_done_to_them(self):
        """
        The two readings are different endpoints, and the fixture makes them
        disagree: the manager *created* an account, and that same row is what
        was *done to* the new hire.
        """
        response = self.client.post(
            "/api/v1/members",
            data={"email": "hire@example.com", "full_name": "New Hire"},
            content_type="application/json",
            **auth(self.manager),
        )
        self.assertEqual(response.status_code, 201, response.content)
        hire = User.objects.get(email="hire@example.com")

        self.assertEqual(len(self.member_rows(self.manager)), 1)
        self.assertEqual(self.member_rows(hire), [])

        done_to_them = self.rows(target_type="user", target_id=str(hire.public_id))
        self.assertEqual(len(done_to_them), 1)
        self.assertEqual(done_to_them[0]["actor_username"], "activity-manager")

    def test_a_member_of_another_organization_is_a_404(self):
        response = self.client.get(
            f"/api/v1/members/{self.rival_owner.public_id}/activity",
            **auth(self.plain),
        )
        self.assertEqual(response.status_code, 404)
