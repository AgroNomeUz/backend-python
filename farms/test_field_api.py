"""
farms/test_field_api.py
Fields, field boundaries and crop zones over the API.

Built on `FarmsTestCase`: two organizations, each with an owner, a member who
holds no permissions and a manager who holds `farms.manage`. The negative case
(the other org's ids, a member without the permission) is checked as often as
the happy path. The constraint tests write through the ORM, as inquiries/tests.py
does, so a view that forgot its guard could not make them pass.
"""

from datetime import date, timedelta

from django.db import IntegrityError, transaction

from core.models import ActivityLog
from equipment.models import ExternalReference
from farms.models import CropZone, Farm, Field, FieldBoundary, Season

from .test_support import FarmsTestCase, auth, multipolygon_geojson, polygon_geojson

FIELDS = "/api/v1/fields"
ZONES = "/api/v1/crop-zones"


class FieldApiCase(FarmsTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.a_farm = Farm.objects.create(organization=cls.a_org, name="A farm")
        cls.b_farm = Farm.objects.create(organization=cls.b_org, name="B farm")
        cls.a_season = Season.objects.create(
            organization=cls.a_org,
            name="2026",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        cls.b_season = Season.objects.create(
            organization=cls.b_org,
            name="2026",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )

    def make_field(self, farm=None, name="North", org=None, **extra):
        farm = farm or self.a_farm
        return Field.objects.create(
            farm=farm, organization=farm.organization, name=name, **extra
        )

    def make_zone(self, field, season=None, **extra):
        season = season or self.a_season
        values = {"crop_code": "TRZAX", "crop_name": "Wheat"}
        values.update(extra)
        return CropZone.objects.create(
            organization=field.organization, field=field, season=season, **values
        )

    def get(self, path, user=None, **params):
        return self.client.get(path, params, **auth(user or self.a_owner))

    def post(self, path, body, user=None):
        return self.client.post(
            path, body, content_type="application/json", **auth(user or self.a_owner)
        )

    def patch(self, path, body, user=None):
        return self.client.patch(
            path, body, content_type="application/json", **auth(user or self.a_owner)
        )

    def delete(self, path, user=None):
        return self.client.delete(path, **auth(user or self.a_owner))

    def activity_count(self, org=None):
        return ActivityLog.objects.filter(organization=org or self.a_org).count()


class FieldTests(FieldApiCase):
    def test_create_minimal_field(self):
        response = self.post(
            FIELDS, {"farm_id": str(self.a_farm.public_id), "name": "North"}
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["farm"]["name"], "A farm")
        self.assertEqual(body["farm"]["id"], str(self.a_farm.public_id))
        self.assertIsNone(body["area"])
        self.assertIsNone(body["current_boundary"])
        self.assertEqual(body["external_references"], [])
        field = Field.objects.get(public_id=body["id"])
        self.assertEqual(field.organization, self.a_org)
        log = ActivityLog.objects.get(organization=self.a_org)
        self.assertEqual(log.action, ActivityLog.Action.CREATED)
        self.assertEqual(log.actor, self.a_owner)

    def test_create_with_area_and_initial_boundary(self):
        response = self.post(
            FIELDS,
            {
                "farm_id": str(self.a_farm.public_id),
                "name": "North",
                "area": {"value": "12.5", "unit": "har"},
                "boundary": polygon_geojson(),
            },
            user=self.a_manager,
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["area"], {"value": "12.5000", "unit": "har"})
        current = body["current_boundary"]
        self.assertEqual(current["geometry"]["type"], "MultiPolygon")
        self.assertEqual(current["source"], "drawn")
        self.assertIsNone(current["valid_to"])
        self.assertEqual(current["valid_from"], date.today().isoformat())
        self.assertEqual(current["area"]["unit"], "har")
        # A 0.001 degree square at 41N is about 0.93 ha.
        self.assertTrue(0.8 < float(current["area"]["value"]) < 1.1)
        field = Field.objects.get(public_id=body["id"])
        boundary = field.boundaries.get()
        self.assertEqual(boundary.created_by, self.a_manager)
        # One audit row, and it mentions the boundary.
        log = ActivityLog.objects.get(organization=self.a_org)
        self.assertEqual(log.changes["boundary"]["to"]["id"], str(boundary.public_id))

    def test_area_is_not_derived_from_the_boundary(self):
        response = self.post(
            FIELDS,
            {
                "farm_id": str(self.a_farm.public_id),
                "name": "North",
                "boundary": polygon_geojson(),
            },
        )
        self.assertIsNone(response.json()["area"])
        self.assertIsNotNone(response.json()["current_boundary"])

    def test_create_in_other_orgs_farm_is_404(self):
        response = self.post(
            FIELDS, {"farm_id": str(self.b_farm.public_id), "name": "North"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Field.objects.count(), 0)
        self.assertEqual(self.activity_count(), 0)

    def test_create_requires_the_permission(self):
        response = self.post(
            FIELDS,
            {"farm_id": str(self.a_farm.public_id), "name": "North"},
            user=self.a_member,
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Manage farms", response.json()["detail"])
        self.assertEqual(Field.objects.count(), 0)

    def test_duplicate_name_in_a_farm_is_409(self):
        self.make_field(name="North")
        response = self.post(
            FIELDS, {"farm_id": str(self.a_farm.public_id), "name": "North"}
        )
        self.assertEqual(response.status_code, 409)
        # Same name on another farm of the org is fine.
        other = Farm.objects.create(organization=self.a_org, name="Second")
        response = self.post(
            FIELDS, {"farm_id": str(other.public_id), "name": "North"}
        )
        self.assertEqual(response.status_code, 201)

    def test_bad_area_is_rejected(self):
        for area in (
            {"value": "0", "unit": "har"},
            {"value": "-1", "unit": "har"},
            {"value": "5", "unit": "acre"},
        ):
            response = self.post(
                FIELDS,
                {"farm_id": str(self.a_farm.public_id), "name": "N", "area": area},
            )
            self.assertEqual(response.status_code, 422, area)

    def test_list_filters_and_pagination(self):
        other_farm = Farm.objects.create(organization=self.a_org, name="Second")
        self.make_field(name="North")
        self.make_field(name="South")
        self.make_field(farm=other_farm, name="East")
        self.make_field(farm=self.b_farm, name="Foreign")

        body = self.get(FIELDS).json()
        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["items"]), 3)
        self.assertEqual(
            {item["name"] for item in body["items"]}, {"North", "South", "East"}
        )

        page = self.get(FIELDS, limit=2, offset=0).json()
        self.assertEqual(page["count"], 3)
        self.assertEqual(len(page["items"]), 2)

        by_farm = self.get(FIELDS, farm_id=str(other_farm.public_id)).json()
        self.assertEqual([i["name"] for i in by_farm["items"]], ["East"])
        searched = self.get(FIELDS, search="sou").json()
        self.assertEqual([i["name"] for i in searched["items"]], ["South"])

    def test_list_filters_by_cluster(self):
        from farms.models import FarmCluster

        cluster = FarmCluster.objects.create(organization=self.a_org, name="C")
        other_farm = Farm.objects.create(
            organization=self.a_org, name="Second", cluster=cluster
        )
        self.make_field(name="North")
        self.make_field(farm=other_farm, name="East")
        body = self.get(FIELDS, cluster_id=str(cluster.public_id)).json()
        self.assertEqual([i["name"] for i in body["items"]], ["East"])

    def test_list_shows_the_current_boundary(self):
        field = self.make_field()
        FieldBoundary.objects.create(
            field=field,
            geometry=_multi(),
            valid_from=date(2025, 1, 1),
            valid_to=date(2025, 12, 31),
        )
        FieldBoundary.objects.create(
            field=field, geometry=_multi(lon=69.1), valid_from=date(2026, 1, 1)
        )
        item = self.get(FIELDS).json()["items"][0]
        self.assertEqual(item["current_boundary"]["valid_from"], "2026-01-01")

    def test_detail_and_other_org_detail(self):
        field = self.make_field()
        response = self.get(f"{FIELDS}/{field.public_id}", user=self.a_member)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "North")
        # The other org gets a 404, not a 403.
        self.assertEqual(
            self.get(f"{FIELDS}/{field.public_id}", user=self.b_owner).status_code, 404
        )

    def test_reads_are_open_to_members_without_the_permission(self):
        field = self.make_field()
        self.assertEqual(self.get(FIELDS, user=self.a_member).status_code, 200)
        self.assertEqual(
            self.get(f"{FIELDS}/{field.public_id}/boundaries", user=self.a_member).status_code,
            200,
        )
        self.assertEqual(
            self.get(f"{FIELDS}/{field.public_id}/activity", user=self.a_member).status_code,
            200,
        )

    def test_patch_updates_and_audits(self):
        field = self.make_field()
        response = self.patch(
            f"{FIELDS}/{field.public_id}",
            {"name": "Renamed", "area": {"value": "3", "unit": "m2"}, "soil_type": "loam"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["area"], {"value": "3.0000", "unit": "m2"})
        field.refresh_from_db()
        self.assertEqual(field.name, "Renamed")
        log = ActivityLog.objects.get(organization=self.a_org)
        self.assertEqual(log.action, ActivityLog.Action.UPDATED)
        self.assertEqual(log.changes["name"], {"from": "North", "to": "Renamed"})
        self.assertIn("area_unit", log.changes)

    def test_patch_null_area_clears_it(self):
        field = self.make_field(area="5")
        response = self.patch(f"{FIELDS}/{field.public_id}", {"area": None})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["area"])

    def test_noop_patch_writes_no_audit_row(self):
        field = self.make_field(name="North")
        self.assertEqual(self.patch(f"{FIELDS}/{field.public_id}", {}).status_code, 200)
        self.assertEqual(
            self.patch(f"{FIELDS}/{field.public_id}", {"name": "North"}).status_code, 200
        )
        self.assertEqual(self.activity_count(), 0)

    def test_patch_null_name_is_400(self):
        field = self.make_field()
        response = self.patch(f"{FIELDS}/{field.public_id}", {"name": None})
        self.assertEqual(response.status_code, 400)

    def test_patch_to_a_taken_name_is_409(self):
        self.make_field(name="South")
        field = self.make_field(name="North")
        response = self.patch(f"{FIELDS}/{field.public_id}", {"name": "South"})
        self.assertEqual(response.status_code, 409)
        field.refresh_from_db()
        self.assertEqual(field.name, "North")
        self.assertEqual(self.activity_count(), 0)

    def test_patch_permission_and_other_org(self):
        field = self.make_field()
        denied = self.patch(f"{FIELDS}/{field.public_id}", {"name": "x"}, user=self.a_member)
        self.assertEqual(denied.status_code, 403)
        self.assertIn("Manage farms", denied.json()["detail"])
        foreign = self.patch(f"{FIELDS}/{field.public_id}", {"name": "x"}, user=self.b_owner)
        self.assertEqual(foreign.status_code, 404)

    def test_delete_field(self):
        field = self.make_field()
        FieldBoundary.objects.create(
            field=field, geometry=_multi(), valid_from=date(2026, 1, 1)
        )
        response = self.delete(f"{FIELDS}/{field.public_id}")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Field.objects.exists())
        self.assertFalse(FieldBoundary.objects.exists())
        log = ActivityLog.objects.get(organization=self.a_org)
        self.assertEqual(log.action, ActivityLog.Action.DELETED)

    def test_delete_with_crop_zones_is_409(self):
        field = self.make_field()
        self.make_zone(field)
        response = self.delete(f"{FIELDS}/{field.public_id}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("crop zones", response.json()["detail"])
        self.assertTrue(Field.objects.filter(pk=field.pk).exists())
        self.assertEqual(self.activity_count(), 0)

    def test_delete_permission_and_other_org(self):
        field = self.make_field()
        self.assertEqual(
            self.delete(f"{FIELDS}/{field.public_id}", user=self.a_member).status_code, 403
        )
        self.assertEqual(
            self.delete(f"{FIELDS}/{field.public_id}", user=self.b_owner).status_code, 404
        )
        self.assertTrue(Field.objects.exists())

    def test_activity_endpoint(self):
        field = self.make_field()
        self.patch(f"{FIELDS}/{field.public_id}", {"name": "Renamed"})
        body = self.get(f"{FIELDS}/{field.public_id}/activity").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"][0]["action"], "updated")
        self.assertEqual(
            self.get(f"{FIELDS}/{field.public_id}/activity", user=self.b_owner).status_code,
            404,
        )

    def test_field_constraints_via_the_orm(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_field(name="Zero", area="0")
        # A field cannot carry a different organization than its farm.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Field.objects.create(
                farm=self.a_farm, organization=self.b_org, name="Mixed"
            )


class BoundaryTests(FieldApiCase):
    def url(self, field):
        return f"{FIELDS}/{field.public_id}/boundaries"

    def test_first_boundary(self):
        field = self.make_field()
        response = self.post(
            self.url(field),
            {"geometry": polygon_geojson(), "valid_from": "2026-03-01", "source": "gps"},
            user=self.a_manager,
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["geometry"]["type"], "MultiPolygon")
        self.assertEqual(body["source"], "gps")
        self.assertEqual(body["area"]["unit"], "har")
        self.assertEqual(body["created_by_id"], str(self.a_manager.public_id))
        log = ActivityLog.objects.get(organization=self.a_org)
        self.assertEqual(log.action, ActivityLog.Action.UPDATED)
        self.assertIsNone(log.changes["boundary"]["from"])
        self.assertEqual(log.changes["boundary"]["to"]["id"], body["id"])
        self.assertEqual(log.changes["boundary"]["to"]["area"], float(body["area"]["value"]))

    def test_multipolygon_is_accepted(self):
        field = self.make_field()
        response = self.post(self.url(field), {"geometry": multipolygon_geojson()})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["valid_from"], date.today().isoformat())

    def test_area_is_computed_in_hectares(self):
        field = self.make_field()
        # 0.01 degrees is about 1.11 km north-south and 0.84 km east-west at 41N
        # (cos 41 = 0.755): roughly 93 ha.
        response = self.post(
            self.url(field), {"geometry": polygon_geojson(size=0.01)}
        )
        value = float(response.json()["area"]["value"])
        self.assertTrue(85 < value < 100, value)
        self.assertEqual(response.json()["area"]["unit"], "har")

    def test_second_boundary_closes_the_first(self):
        field = self.make_field()
        first = self.post(
            self.url(field),
            {"geometry": polygon_geojson(), "valid_from": "2025-01-01"},
        ).json()
        second = self.post(
            self.url(field),
            {"geometry": polygon_geojson(size=0.002), "valid_from": "2026-01-01"},
        )
        self.assertEqual(second.status_code, 201, second.content)

        old = FieldBoundary.objects.get(public_id=first["id"])
        self.assertEqual(old.valid_to, date(2025, 12, 31))
        self.assertEqual(
            FieldBoundary.objects.filter(field=field, valid_to__isnull=True).count(), 1
        )
        # Audited: from the old boundary to the new one.
        log = ActivityLog.objects.filter(organization=self.a_org).first()
        self.assertEqual(log.changes["boundary"]["from"]["id"], first["id"])
        self.assertEqual(log.changes["boundary"]["to"]["id"], second.json()["id"])

        # The field reports the new one as current.
        detail = self.get(f"{FIELDS}/{field.public_id}").json()
        self.assertEqual(detail["current_boundary"]["id"], second.json()["id"])

    def test_history_is_newest_first(self):
        field = self.make_field()
        for day in ("2024-01-01", "2025-01-01", "2026-01-01"):
            self.post(self.url(field), {"geometry": polygon_geojson(), "valid_from": day})
        body = self.get(self.url(field)).json()
        self.assertEqual(body["count"], 3)
        self.assertEqual(
            [b["valid_from"] for b in body["items"]],
            ["2026-01-01", "2025-01-01", "2024-01-01"],
        )
        self.assertEqual(
            [b["valid_to"] for b in body["items"]],
            [None, "2025-12-31", "2024-12-31"],
        )

    def test_same_day_replacement_keeps_the_range_valid(self):
        field = self.make_field()
        self.post(self.url(field), {"geometry": polygon_geojson(), "valid_from": "2026-01-01"})
        response = self.post(
            self.url(field), {"geometry": polygon_geojson(size=0.002), "valid_from": "2026-01-01"}
        )
        self.assertEqual(response.status_code, 201, response.content)
        old = FieldBoundary.objects.get(field=field, valid_to__isnull=False)
        self.assertEqual(old.valid_to, date(2026, 1, 1))

    def test_valid_from_before_the_open_boundary_is_400(self):
        field = self.make_field()
        self.post(self.url(field), {"geometry": polygon_geojson(), "valid_from": "2026-01-01"})
        before = self.activity_count()
        response = self.post(
            self.url(field), {"geometry": polygon_geojson(), "valid_from": "2025-06-01"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(field.boundaries.count(), 1)
        self.assertIsNone(field.boundaries.get().valid_to)
        self.assertEqual(self.activity_count(), before)

    def test_overlap_with_an_earlier_closed_range_is_400(self):
        field = self.make_field()
        # Only closed boundaries remain (the data model allows it).
        FieldBoundary.objects.create(
            field=field,
            geometry=_multi(),
            valid_from=date(2025, 1, 1),
            valid_to=date(2025, 12, 31),
        )
        response = self.post(
            self.url(field), {"geometry": polygon_geojson(), "valid_from": "2025-06-01"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("overlap", response.json()["detail"])
        ok = self.post(
            self.url(field), {"geometry": polygon_geojson(), "valid_from": "2026-01-01"}
        )
        self.assertEqual(ok.status_code, 201)

    def test_bad_geometry_is_400(self):
        field = self.make_field()
        bowtie = {
            "type": "Polygon",
            "coordinates": [[[69, 41], [69.01, 41.01], [69.01, 41], [69, 41.01], [69, 41]]],
        }
        cases = {
            "self-intersecting": bowtie,
            "empty": {"type": "Polygon", "coordinates": []},
            "out of range": polygon_geojson(lon=500, lat=41),
            "point": {"type": "Point", "coordinates": [69, 41]},
            "junk": {"type": "Polygon", "coordinates": "nope"},
            "not geojson": {"hello": "world"},
        }
        for label, geometry in cases.items():
            response = self.post(self.url(field), {"geometry": geometry})
            self.assertEqual(response.status_code, 400, (label, response.content))
        self.assertEqual(field.boundaries.count(), 0)
        self.assertEqual(self.activity_count(), 0)

    def test_bad_geometry_on_field_create_creates_nothing(self):
        response = self.post(
            FIELDS,
            {
                "farm_id": str(self.a_farm.public_id),
                "name": "North",
                "boundary": {"type": "Polygon", "coordinates": []},
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Field.objects.exists())

    def test_unknown_source_is_422(self):
        field = self.make_field()
        response = self.post(
            self.url(field), {"geometry": polygon_geojson(), "source": "satellite"}
        )
        self.assertEqual(response.status_code, 422)

    def test_other_orgs_field_is_404(self):
        foreign = self.make_field(farm=self.b_farm)
        self.assertEqual(self.get(self.url(foreign)).status_code, 404)
        self.assertEqual(
            self.post(self.url(foreign), {"geometry": polygon_geojson()}).status_code, 404
        )
        self.assertFalse(FieldBoundary.objects.exists())

    def test_post_needs_the_permission(self):
        field = self.make_field()
        response = self.post(
            self.url(field), {"geometry": polygon_geojson()}, user=self.a_member
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Manage farms", response.json()["detail"])

    def test_one_open_boundary_per_field_in_the_database(self):
        field = self.make_field()
        FieldBoundary.objects.create(
            field=field, geometry=_multi(), valid_from=date(2026, 1, 1)
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            FieldBoundary.objects.create(
                field=field, geometry=_multi(), valid_from=date(2026, 2, 1)
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            FieldBoundary.objects.create(
                field=field,
                geometry=_multi(),
                valid_from=date(2026, 2, 1),
                valid_to=date(2026, 1, 1) - timedelta(days=1),
            )


class CropZoneTests(FieldApiCase):
    def body(self, **overrides):
        values = {
            "field_id": str(self.field.public_id),
            "season_id": str(self.a_season.public_id),
            "crop_code": "trzax",
            "crop_name": "Wheat",
        }
        values.update(overrides)
        return values

    def setUp(self):
        self.field = self.make_field()

    def test_create(self):
        response = self.post(
            ZONES,
            self.body(
                geometry=polygon_geojson(),
                area={"value": "4.5", "unit": "har"},
                planting_date="2026-03-01",
                expected_harvest_date="2026-07-01",
            ),
            user=self.a_manager,
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["crop_code"], "TRZAX")
        self.assertEqual(body["field"]["name"], "North")
        self.assertEqual(body["farm"]["name"], "A farm")
        self.assertEqual(body["season"]["name"], "2026")
        self.assertEqual(body["area"], {"value": "4.5000", "unit": "har"})
        self.assertEqual(body["geometry"]["type"], "MultiPolygon")
        self.assertEqual(body["external_references"], [])
        zone = CropZone.objects.get(public_id=body["id"])
        self.assertEqual(zone.organization, self.a_org)
        log = ActivityLog.objects.get(organization=self.a_org)
        self.assertEqual(log.action, ActivityLog.Action.CREATED)
        self.assertEqual(log.changes["crop_code"]["to"], "TRZAX")

    def test_create_minimal(self):
        response = self.post(ZONES, self.body())
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.json()["geometry"])
        self.assertIsNone(response.json()["area"])

    def test_field_and_season_of_another_org_are_404(self):
        foreign_field = self.make_field(farm=self.b_farm)
        for overrides in (
            {"field_id": str(foreign_field.public_id)},
            {"season_id": str(self.b_season.public_id)},
        ):
            response = self.post(ZONES, self.body(**overrides))
            self.assertEqual(response.status_code, 404, overrides)
        self.assertFalse(CropZone.objects.exists())
        self.assertEqual(self.activity_count(), 0)

    def test_create_needs_the_permission(self):
        response = self.post(ZONES, self.body(), user=self.a_member)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Manage farms", response.json()["detail"])

    def test_bad_input_is_400_not_500(self):
        cases = [
            self.body(planting_date="2026-06-01", expected_harvest_date="2026-05-01"),
            self.body(crop_code="   "),
            self.body(geometry={"type": "Polygon", "coordinates": []}),
        ]
        for body in cases:
            response = self.post(ZONES, body)
            self.assertEqual(response.status_code, 400, (body, response.content))
        self.assertEqual(
            self.post(ZONES, self.body(crop_code="X" * 11)).status_code, 422
        )
        self.assertEqual(
            self.post(ZONES, self.body(area={"value": "0", "unit": "har"})).status_code,
            422,
        )
        self.assertFalse(CropZone.objects.exists())

    def test_list_filters(self):
        other_farm = Farm.objects.create(organization=self.a_org, name="Second")
        other_field = self.make_field(farm=other_farm, name="East")
        other_season = Season.objects.create(
            organization=self.a_org,
            name="2027",
            start_date=date(2027, 1, 1),
            end_date=date(2027, 12, 31),
        )
        self.make_zone(self.field)
        self.make_zone(other_field, crop_code="HORVX", crop_name="Barley")
        self.make_zone(self.field, season=other_season, crop_code="ZEAMX", crop_name="Maize")
        foreign = self.make_field(farm=self.b_farm)
        self.make_zone(foreign, season=self.b_season)

        everything = self.get(ZONES).json()
        self.assertEqual(everything["count"], 3)
        self.assertEqual(len(everything["items"]), 3)

        def codes(**params):
            return sorted(i["crop_code"] for i in self.get(ZONES, **params).json()["items"])

        self.assertEqual(codes(field_id=str(self.field.public_id)), ["TRZAX", "ZEAMX"])
        self.assertEqual(codes(farm_id=str(other_farm.public_id)), ["HORVX"])
        self.assertEqual(codes(season_id=str(other_season.public_id)), ["ZEAMX"])
        self.assertEqual(codes(crop_code="horvx"), ["HORVX"])
        page = self.get(ZONES, limit=1).json()
        self.assertEqual((page["count"], len(page["items"])), (3, 1))

    def test_detail_and_other_org(self):
        zone = self.make_zone(self.field)
        response = self.get(f"{ZONES}/{zone.public_id}", user=self.a_member)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["crop_name"], "Wheat")
        self.assertEqual(
            self.get(f"{ZONES}/{zone.public_id}", user=self.b_owner).status_code, 404
        )

    def test_patch(self):
        zone = self.make_zone(self.field)
        response = self.patch(
            f"{ZONES}/{zone.public_id}",
            {
                "crop_code": "horvx",
                "crop_name": "Barley",
                "area": {"value": "2", "unit": "har"},
                "geometry": multipolygon_geojson(),
                "harvest_date": "2026-08-01",
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["crop_code"], "HORVX")
        self.assertEqual(body["geometry"]["type"], "MultiPolygon")
        log = ActivityLog.objects.get(organization=self.a_org)
        self.assertEqual(log.action, ActivityLog.Action.UPDATED)
        self.assertEqual(log.changes["crop_name"], {"from": "Wheat", "to": "Barley"})
        self.assertIn("geometry", log.changes)
        # Clearing the geometry again.
        cleared = self.patch(f"{ZONES}/{zone.public_id}", {"geometry": None})
        self.assertIsNone(cleared.json()["geometry"])

    def test_noop_patch_writes_no_audit_row(self):
        zone = self.make_zone(self.field)
        self.patch(f"{ZONES}/{zone.public_id}", {})
        self.patch(f"{ZONES}/{zone.public_id}", {"crop_code": "trzax", "crop_name": "Wheat"})
        self.assertEqual(self.activity_count(), 0)

    def test_patch_moves_to_another_own_field_but_not_a_foreign_one(self):
        zone = self.make_zone(self.field)
        other = self.make_field(name="South")
        moved = self.patch(f"{ZONES}/{zone.public_id}", {"field_id": str(other.public_id)})
        self.assertEqual(moved.json()["field"]["name"], "South")
        foreign = self.make_field(farm=self.b_farm)
        denied = self.patch(
            f"{ZONES}/{zone.public_id}", {"field_id": str(foreign.public_id)}
        )
        self.assertEqual(denied.status_code, 404)
        seasoned = self.patch(
            f"{ZONES}/{zone.public_id}", {"season_id": str(self.b_season.public_id)}
        )
        self.assertEqual(seasoned.status_code, 404)

    def test_patch_dates_violating_the_check_is_400(self):
        zone = self.make_zone(self.field, planting_date=date(2026, 5, 1))
        response = self.patch(
            f"{ZONES}/{zone.public_id}", {"expected_harvest_date": "2026-04-01"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.activity_count(), 0)

    def test_patch_permission_and_other_org(self):
        zone = self.make_zone(self.field)
        denied = self.patch(f"{ZONES}/{zone.public_id}", {"notes": "x"}, user=self.a_member)
        self.assertEqual(denied.status_code, 403)
        self.assertIn("Manage farms", denied.json()["detail"])
        foreign = self.patch(f"{ZONES}/{zone.public_id}", {"notes": "x"}, user=self.b_owner)
        self.assertEqual(foreign.status_code, 404)

    def test_delete(self):
        zone = self.make_zone(self.field)
        self.assertEqual(self.delete(f"{ZONES}/{zone.public_id}").status_code, 204)
        self.assertFalse(CropZone.objects.exists())
        log = ActivityLog.objects.get(organization=self.a_org)
        self.assertEqual(log.action, ActivityLog.Action.DELETED)
        # Now the field can go.
        self.assertEqual(self.delete(f"{FIELDS}/{self.field.public_id}").status_code, 204)

    def test_delete_permission_and_other_org(self):
        zone = self.make_zone(self.field)
        self.assertEqual(
            self.delete(f"{ZONES}/{zone.public_id}", user=self.a_member).status_code, 403
        )
        self.assertEqual(
            self.delete(f"{ZONES}/{zone.public_id}", user=self.b_owner).status_code, 404
        )
        self.assertTrue(CropZone.objects.exists())

    def test_activity_endpoint(self):
        zone = self.make_zone(self.field)
        self.patch(f"{ZONES}/{zone.public_id}", {"notes": "x"})
        body = self.get(f"{ZONES}/{zone.public_id}/activity").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(
            self.get(f"{ZONES}/{zone.public_id}/activity", user=self.b_owner).status_code,
            404,
        )

    def test_constraints_via_the_orm(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_zone(
                self.field,
                planting_date=date(2026, 6, 1),
                expected_harvest_date=date(2026, 5, 1),
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_zone(self.field, area="0")
        # A zone cannot mix a field and a season of different organizations.
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_zone(self.field, season=self.b_season)

    def test_a_season_with_zones_cannot_be_deleted(self):
        from django.db.models import RestrictedError

        self.make_zone(self.field)
        with self.assertRaises(RestrictedError):
            self.a_season.delete()


class FieldExternalReferenceTests(FieldApiCase):
    def test_add_list_remove_on_a_field_and_a_zone(self):
        field = self.make_field()
        zone = self.make_zone(field)
        for base, target in ((FIELDS, field), (ZONES, zone)):
            url = f"{base}/{target.public_id}/external-references"
            created = self.post(url, {"system": "adapt", "external_id": "F-1"})
            self.assertEqual(created.status_code, 201, created.content)
            self.assertEqual(self.post(url, {"system": "adapt", "external_id": "F-1"}).status_code, 409)
            self.assertEqual(len(self.get(url).json()), 1)
            detail = self.get(f"{base}/{target.public_id}").json()
            self.assertEqual(detail["external_references"][0]["external_id"], "F-1")
            self.assertEqual(
                self.get(url, user=self.b_owner).status_code, 404
            )
            removed = self.delete(f"{url}/{created.json()['id']}")
            self.assertEqual(removed.status_code, 204)
        self.assertFalse(ExternalReference.objects.exists())


def _multi(lon: float = 69.0):
    from django.contrib.gis.geos import GEOSGeometry
    import json

    return GEOSGeometry(json.dumps(multipolygon_geojson(lon=lon)), srid=4326)


def _with_z(geojson: dict) -> dict:
    """The same GeoJSON with an altitude on every position."""
    def lift(node):
        if node and isinstance(node[0], (int, float)):
            return [*node[:2], 100]
        return [lift(child) for child in node]

    return {**geojson, "coordinates": lift(geojson["coordinates"])}


def _box(west, south, east, north) -> dict:
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {"type": "Polygon", "coordinates": [ring]}


# The whole globe (PostGIS cannot measure it), and a measurable box far past the column.
WORLD = _box(-180, -90, 180, 90)
HUGE = _box(-170, -80, 170, 80)


class ExternalReferenceColdCacheTests(FieldApiCase):
    def test_listing_references_works_with_a_cold_content_type_cache(self):
        from django.contrib.contenttypes.models import ContentType

        field = self.make_field()
        zone = self.make_zone(field)
        for base, target in ((FIELDS, field), (ZONES, zone)):
            ContentType.objects.clear_cache()
            response = self.get(f"{base}/{target.public_id}/external-references")
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json(), [])


class ThreeDimensionalGeometryTests(FieldApiCase):
    def test_altitude_is_dropped_everywhere(self):
        field = self.make_field()
        z_polygon = _with_z(polygon_geojson())

        posted = self.post(f"{FIELDS}/{field.public_id}/boundaries", {"geometry": z_polygon})
        self.assertEqual(posted.status_code, 201, posted.content)
        self.assertFalse(FieldBoundary.objects.get().geometry.hasz)
        self.assertEqual(len(posted.json()["geometry"]["coordinates"][0][0][0]), 2)

        created = self.post(
            FIELDS,
            {"farm_id": str(self.a_farm.public_id), "name": "Z", "boundary": z_polygon},
        )
        self.assertEqual(created.status_code, 201, created.content)
        self.assertFalse(
            FieldBoundary.objects.get(field__public_id=created.json()["id"]).geometry.hasz
        )

        zone = self.post(
            ZONES,
            {
                "field_id": str(field.public_id),
                "season_id": str(self.a_season.public_id),
                "crop_code": "TRZAX",
                "crop_name": "Wheat",
                "geometry": _with_z(multipolygon_geojson()),
            },
        )
        self.assertEqual(zone.status_code, 201, zone.content)
        self.assertFalse(CropZone.objects.get().geometry.hasz)


class AreaBoundsTests(FieldApiCase):
    def test_huge_quantity_is_422(self):
        body = {"farm_id": str(self.a_farm.public_id), "name": "Big"}
        for area in (
            {"value": "1000000000", "unit": "har"},
            {"value": "99999999999999", "unit": "har"},
            {"value": "10000000000", "unit": "m2"},
            {"value": "1e999", "unit": "har"},
            {"value": "0.00001", "unit": "har"},
        ):
            self.assertEqual(self.post(FIELDS, {**body, "area": area}).status_code, 422, area)
        field = self.make_field(name="Ok")
        self.assertEqual(
            self.patch(
                f"{FIELDS}/{field.public_id}", {"area": {"value": "1000000000", "unit": "har"}}
            ).status_code,
            422,
        )
        zone = self.post(
            ZONES,
            {
                "field_id": str(field.public_id),
                "season_id": str(self.a_season.public_id),
                "crop_code": "TRZAX",
                "crop_name": "Wheat",
                "area": {"value": "1000000000", "unit": "har"},
            },
        )
        self.assertEqual(zone.status_code, 422)
        edge = self.post(FIELDS, {**body, "area": {"value": "999999999.9999", "unit": "har"}})
        self.assertEqual(edge.status_code, 201, edge.content)

    def test_boundary_too_large_is_400(self):
        field = self.make_field()
        for index, geometry in enumerate((WORLD, HUGE)):
            response = self.post(
                f"{FIELDS}/{field.public_id}/boundaries", {"geometry": geometry}
            )
            self.assertEqual(response.status_code, 400, response.content)
            created = self.post(
                FIELDS,
                {
                    "farm_id": str(self.a_farm.public_id),
                    "name": "World",
                    "boundary": geometry,
                },
            )
            self.assertEqual(created.status_code, 400, created.content)
        self.assertFalse(FieldBoundary.objects.exists())
        self.assertFalse(Field.objects.filter(name="World").exists())
        self.assertEqual(self.activity_count(), 0)


class ReferenceCleanupTests(FieldApiCase):
    def test_deleting_a_field_or_zone_removes_its_references(self):
        field = self.make_field()
        zone = self.make_zone(field)
        for base, target in ((ZONES, zone), (FIELDS, field)):
            url = f"{base}/{target.public_id}"
            self.post(f"{url}/external-references", {"system": "adapt", "external_id": "1"})
            self.assertEqual(ExternalReference.objects.count(), 1)
            self.assertEqual(self.delete(url).status_code, 204)
            self.assertEqual(ExternalReference.objects.count(), 0)


class AreaQuantisationTests(FieldApiCase):
    def test_patching_a_sub_column_difference_is_a_noop(self):
        field = self.make_field(area="5")
        response = self.patch(
            f"{FIELDS}/{field.public_id}", {"area": {"value": "5.00001", "unit": "har"}}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.activity_count(), 0)

        zone = self.make_zone(field, area="5")
        response = self.patch(
            f"{ZONES}/{zone.public_id}", {"area": {"value": "5.00001", "unit": "har"}}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.activity_count(), 0)

    def test_create_stores_and_audits_the_quantised_area(self):
        response = self.post(
            FIELDS,
            {
                "farm_id": str(self.a_farm.public_id),
                "name": "Q",
                "area": {"value": "5.00005", "unit": "har"},
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        log = ActivityLog.objects.get(organization=self.a_org)
        self.assertEqual(str(log.changes["area"]["to"]), "5.0001")


class WhitespaceNameTests(FieldApiCase):
    def test_blank_field_name_is_rejected(self):
        farm = str(self.a_farm.public_id)
        self.assertEqual(self.post(FIELDS, {"farm_id": farm, "name": "   "}).status_code, 422)
        field = self.make_field()
        self.assertEqual(self.patch(f"{FIELDS}/{field.public_id}", {"name": " \t "}).status_code, 422)
        self.assertEqual(Field.objects.get().name, "North")
        created = self.post(FIELDS, {"farm_id": farm, "name": "  Padded  "})
        self.assertEqual(created.json()["name"], "Padded")
