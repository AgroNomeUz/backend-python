"""
farms/test_farm_api.py
Farm clusters, farms, seasons and a farm's external references.

Org A is the caller; org B owns the ids that must come back 404. Writes go
through the API (the audit row is part of what is under test); rows that need
to exist beforehand are made through the ORM.
"""

from datetime import date
from decimal import Decimal

from core.models import ActivityLog
from equipment.models import ExternalReference

from .models import CropZone, Farm, FarmCluster, Field, Season
from .test_support import FarmsTestCase, auth, polygon_geojson

POINT = {"type": "Point", "coordinates": [69.24, 41.31]}


class FarmApiCase(FarmsTestCase):
    base = ""

    def get(self, path="", user=None, **params):
        return self.client.get(
            f"/api/v1/{self.base}{path}", params, **auth(user or self.a_member)
        )

    def post(self, path="", body=None, user=None):
        return self.client.post(
            f"/api/v1/{self.base}{path}",
            data=body or {},
            content_type="application/json",
            **auth(user or self.a_manager),
        )

    def patch(self, path, body, user=None):
        return self.client.patch(
            f"/api/v1/{self.base}{path}",
            data=body,
            content_type="application/json",
            **auth(user or self.a_manager),
        )

    def delete(self, path, user=None):
        return self.client.delete(
            f"/api/v1/{self.base}{path}", **auth(user or self.a_manager)
        )

    def audit(self, org=None):
        return ActivityLog.objects.filter(organization=org or self.a_org)

    def make_farm(self, org=None, name="North", **kwargs):
        return Farm.objects.create(organization=org or self.a_org, name=name, **kwargs)

    def make_field(self, farm, name="F1", **kwargs):
        return Field.objects.create(
            farm=farm, organization=farm.organization, name=name, **kwargs
        )

    def make_season(self, org=None, name="2026"):
        return Season.objects.create(
            organization=org or self.a_org,
            name=name,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )

    def assert_needs_permission(self, response):
        self.assertEqual(response.status_code, 403)
        self.assertIn("Manage farms", response.json()["detail"])


class ClusterApiTests(FarmApiCase):
    base = "farm-clusters"

    def test_create_get_list_and_pagination_shape(self):
        response = self.post("", {"name": "East", "description": "d"})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["farm_count"], 0)
        self.assertEqual(body["description"], "d")

        self.assertEqual(self.get(f"/{body['id']}").json()["name"], "East")
        listing = self.get("").json()
        self.assertEqual(set(listing), {"items", "count"})
        self.assertEqual(listing["count"], 1)

    def test_search_and_other_orgs_clusters_are_invisible(self):
        FarmCluster.objects.create(organization=self.a_org, name="Alpha")
        FarmCluster.objects.create(organization=self.a_org, name="Beta")
        FarmCluster.objects.create(organization=self.b_org, name="Alpha B")
        self.assertEqual(self.get("").json()["count"], 2)
        self.assertEqual(self.get("", search="alp").json()["count"], 1)

    def test_patch_logs_diff_and_noop_writes_nothing(self):
        cluster = FarmCluster.objects.create(organization=self.a_org, name="Old")
        response = self.patch(f"/{cluster.public_id}", {"name": "New"})
        self.assertEqual(response.json()["name"], "New")
        row = self.audit().get()
        self.assertEqual(row.action, "updated")
        self.assertEqual(row.changes["name"], {"from": "Old", "to": "New"})

        self.patch(f"/{cluster.public_id}", {"name": "New"})
        self.patch(f"/{cluster.public_id}", {})
        self.assertEqual(self.audit().count(), 1)

    def test_delete_detaches_farms_and_is_logged(self):
        cluster = FarmCluster.objects.create(organization=self.a_org, name="C")
        farm = self.make_farm(cluster=cluster)
        response = self.delete(f"/{cluster.public_id}")
        self.assertEqual(response.status_code, 204)
        farm.refresh_from_db()
        self.assertIsNone(farm.cluster_id)
        self.assertEqual(self.audit().get().action, "deleted")

    def test_duplicate_name_is_409(self):
        FarmCluster.objects.create(organization=self.a_org, name="Dup")
        self.assertEqual(self.post("", {"name": "Dup"}).status_code, 409)
        other = FarmCluster.objects.create(organization=self.a_org, name="Other")
        self.assertEqual(
            self.patch(f"/{other.public_id}", {"name": "Dup"}).status_code, 409
        )
        # Same name in another org is fine.
        FarmCluster.objects.create(organization=self.b_org, name="Same")
        self.assertEqual(self.post("", {"name": "Same"}).status_code, 201)

    def test_other_orgs_cluster_is_404(self):
        theirs = FarmCluster.objects.create(organization=self.b_org, name="B")
        for call in (
            lambda: self.get(f"/{theirs.public_id}"),
            lambda: self.patch(f"/{theirs.public_id}", {"name": "x"}),
            lambda: self.delete(f"/{theirs.public_id}"),
            lambda: self.get(f"/{theirs.public_id}/activity"),
        ):
            self.assertEqual(call().status_code, 404)
        self.assertTrue(FarmCluster.objects.filter(pk=theirs.pk).exists())

    def test_member_without_permission_reads_but_cannot_write(self):
        cluster = FarmCluster.objects.create(organization=self.a_org, name="C")
        self.assertEqual(self.get("").status_code, 200)
        self.assertEqual(self.get(f"/{cluster.public_id}").status_code, 200)
        member = self.a_member
        self.assert_needs_permission(self.post("", {"name": "x"}, user=member))
        self.assert_needs_permission(
            self.patch(f"/{cluster.public_id}", {"name": "x"}, user=member)
        )
        self.assert_needs_permission(self.delete(f"/{cluster.public_id}", user=member))
        self.assertEqual(self.audit().count(), 0)

    def test_owner_may_write_and_activity_lists_history(self):
        response = self.post("", {"name": "Own"}, user=self.a_owner)
        self.assertEqual(response.status_code, 201)
        activity = self.get(f"/{response.json()['id']}/activity").json()
        self.assertEqual(activity["count"], 1)
        self.assertEqual(activity["items"][0]["action"], "created")

    def test_blank_name_is_rejected(self):
        self.assertEqual(self.post("", {"name": ""}).status_code, 422)


class FarmApiTests(FarmApiCase):
    base = "farms"

    def test_create_with_everything_and_read_back(self):
        cluster = FarmCluster.objects.create(organization=self.a_org, name="C")
        response = self.post(
            "",
            {
                "name": "Main",
                "address": "Road 1",
                "notes": "n",
                "cluster_id": str(cluster.public_id),
                "region_id": str(self.region.public_id),
                "location": POINT,
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["cluster"], {"id": str(cluster.public_id), "name": "C"})
        self.assertEqual(body["region"]["name"], "Farms Test Region")
        self.assertEqual(body["location"]["type"], "Point")
        self.assertEqual(body["location"]["coordinates"], [69.24, 41.31])
        self.assertEqual(body["field_count"], 0)
        self.assertIsNone(body["total_area"])
        self.assertEqual(body["external_references"], [])
        self.assertEqual(self.get(f"/{body['id']}").json()["name"], "Main")

        row = self.audit().get()
        self.assertEqual(row.action, "created")
        self.assertEqual(row.changes["cluster"]["to"], "C")

    def test_field_count_and_total_area_in_hectares(self):
        farm = self.make_farm()
        self.make_field(farm, "a", area=Decimal("2.5"), area_unit="har")
        self.make_field(farm, "b", area=Decimal("5000"), area_unit="m2")
        self.make_field(farm, "c")
        body = self.get(f"/{farm.public_id}").json()
        self.assertEqual(body["field_count"], 3)
        self.assertEqual(Decimal(body["total_area"]["value"]), Decimal("3.0000"))
        self.assertEqual(body["total_area"]["unit"], "har")
        # Both the detail and the list carry it.
        self.assertEqual(self.get("").json()["items"][0]["field_count"], 3)

    def test_no_areas_means_no_total(self):
        farm = self.make_farm()
        self.make_field(farm)
        body = self.get(f"/{farm.public_id}").json()
        self.assertEqual(body["field_count"], 1)
        self.assertIsNone(body["total_area"])

    def test_list_filters(self):
        cluster = FarmCluster.objects.create(organization=self.a_org, name="C")
        self.make_farm(name="Alpha", cluster=cluster, region=self.region)
        self.make_farm(name="Beta", address="Green street")
        self.make_farm(org=self.b_org, name="Alpha B")
        self.assertEqual(self.get("").json()["count"], 2)
        self.assertEqual(self.get("", search="alp").json()["count"], 1)
        self.assertEqual(self.get("", search="green").json()["count"], 1)
        self.assertEqual(self.get("", region_id=str(self.region.public_id)).json()["count"], 1)
        self.assertEqual(self.get("", cluster_id=str(cluster.public_id)).json()["count"], 1)

    def test_pagination(self):
        for i in range(3):
            self.make_farm(name=f"F{i}")
        page = self.get("", limit=2, offset=2).json()
        self.assertEqual(page["count"], 3)
        self.assertEqual(len(page["items"]), 1)

    def test_other_orgs_cluster_is_404_on_create_and_patch(self):
        theirs = FarmCluster.objects.create(organization=self.b_org, name="B")
        response = self.post("", {"name": "X", "cluster_id": str(theirs.public_id)})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Farm.objects.filter(name="X").exists())

        farm = self.make_farm()
        response = self.patch(
            f"/{farm.public_id}", {"cluster_id": str(theirs.public_id)}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.audit().count(), 0)

    def test_other_orgs_farm_is_404_everywhere(self):
        theirs = self.make_farm(org=self.b_org, name="B")
        pid = theirs.public_id
        calls = [
            self.get(f"/{pid}"),
            self.patch(f"/{pid}", {"name": "x"}),
            self.delete(f"/{pid}"),
            self.get(f"/{pid}/activity"),
            self.get(f"/{pid}/external-references"),
            self.post(f"/{pid}/external-references", {"system": "adapt", "external_id": "1"}),
        ]
        for response in calls:
            self.assertEqual(response.status_code, 404)
        self.assertTrue(Farm.objects.filter(pk=theirs.pk).exists())
        self.assertEqual(ExternalReference.objects.count(), 0)

    def test_patch_updates_attaches_and_clears(self):
        cluster = FarmCluster.objects.create(organization=self.a_org, name="C")
        farm = self.make_farm()
        body = self.patch(
            f"/{farm.public_id}",
            {
                "name": "Renamed",
                "cluster_id": str(cluster.public_id),
                "region_id": str(self.region.public_id),
                "location": POINT,
            },
        ).json()
        self.assertEqual(body["name"], "Renamed")
        self.assertEqual(body["cluster"]["name"], "C")
        row = self.audit().get()
        self.assertEqual(row.action, "updated")
        self.assertEqual(
            set(row.changes), {"name", "cluster", "region", "location"}
        )

        body = self.patch(
            f"/{farm.public_id}",
            {"cluster_id": None, "region_id": None, "location": None},
        ).json()
        self.assertIsNone(body["cluster"])
        self.assertIsNone(body["region"])
        self.assertIsNone(body["location"])
        self.assertEqual(self.audit().count(), 2)

    def test_noop_patch_writes_no_audit_row(self):
        farm = self.make_farm(address="A")
        self.assertEqual(self.patch(f"/{farm.public_id}", {}).status_code, 200)
        self.assertEqual(
            self.patch(f"/{farm.public_id}", {"name": "North", "address": "A"}).status_code,
            200,
        )
        self.assertEqual(self.audit().count(), 0)

    def test_unknown_region_is_400(self):
        farm = self.make_farm()
        unknown = "00000000-0000-4000-8000-000000000000"
        self.assertEqual(
            self.patch(f"/{farm.public_id}", {"region_id": unknown}).status_code, 400
        )
        self.assertEqual(
            self.post("", {"name": "X", "region_id": unknown}).status_code, 400
        )

    def test_bad_geometry_is_4xx(self):
        farm = self.make_farm()
        for location in (
            {"type": "Polygon", "coordinates": polygon_geojson()["coordinates"]},
            {"type": "Point", "coordinates": [200, 100]},
            {"type": "Nonsense"},
            {"coordinates": "x"},
        ):
            response = self.patch(f"/{farm.public_id}", {"location": location})
            self.assertIn(response.status_code, (400, 422), location)
            response = self.post("", {"name": "G", "location": location})
            self.assertIn(response.status_code, (400, 422), location)
        self.assertEqual(self.audit().count(), 0)

    def test_duplicate_name_is_409(self):
        self.make_farm(name="Dup")
        self.assertEqual(self.post("", {"name": "Dup"}).status_code, 409)
        other = self.make_farm(name="Other")
        self.assertEqual(
            self.patch(f"/{other.public_id}", {"name": "Dup"}).status_code, 409
        )
        self.assertEqual(self.audit().count(), 0)

    def test_delete_and_fields_block_it(self):
        farm = self.make_farm()
        field = self.make_field(farm)
        response = self.delete(f"/{farm.public_id}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("fields", response.json()["detail"])
        self.assertEqual(self.audit().count(), 0)

        field.delete()
        self.assertEqual(self.delete(f"/{farm.public_id}").status_code, 204)
        self.assertFalse(Farm.objects.filter(pk=farm.pk).exists())
        self.assertEqual(self.audit().get().action, "deleted")

    def test_delete_removes_external_references(self):
        farm = self.make_farm()
        self.post(
            f"/{farm.public_id}/external-references",
            {"system": "adapt", "external_id": "1"},
        )
        self.delete(f"/{farm.public_id}")
        self.assertEqual(ExternalReference.objects.count(), 0)

    def test_permission_gate(self):
        farm = self.make_farm()
        self.assert_needs_permission(self.post("", {"name": "x"}, user=self.a_member))
        self.assert_needs_permission(
            self.patch(f"/{farm.public_id}", {"name": "x"}, user=self.a_member)
        )
        self.assert_needs_permission(self.delete(f"/{farm.public_id}", user=self.a_member))
        self.assertEqual(self.get("", user=self.a_member).status_code, 200)
        self.assertEqual(self.get(f"/{farm.public_id}", user=self.a_member).status_code, 200)
        self.assertEqual(self.audit().count(), 0)

    def test_activity_is_paginated_history(self):
        farm = self.make_farm()
        self.patch(f"/{farm.public_id}", {"notes": "x"})
        activity = self.get(f"/{farm.public_id}/activity").json()
        self.assertEqual(set(activity), {"items", "count"})
        self.assertEqual(activity["count"], 1)


class FarmExternalReferenceTests(FarmApiCase):
    base = "farms"

    def setUp(self):
        self.farm = self.make_farm()
        self.path = f"/{self.farm.public_id}/external-references"

    def add(self, **body):
        body = {"system": "adapt", "external_id": "ABC", **body}
        return self.post(self.path, body)

    def test_add_list_and_embedded_in_farm(self):
        response = self.add(external_version="v2")
        self.assertEqual(response.status_code, 201, response.content)
        ref = response.json()
        self.assertEqual(ref["system"], "adapt")
        self.assertEqual(ref["external_id"], "ABC")
        self.assertEqual(ref["external_version"], "v2")

        self.assertEqual([r["id"] for r in self.get(self.path).json()], [ref["id"]])
        farm = self.get(f"/{self.farm.public_id}").json()
        self.assertEqual(farm["external_references"][0]["id"], ref["id"])
        listed = self.get("").json()["items"][0]
        self.assertEqual(listed["external_references"][0]["id"], ref["id"])

    def test_add_is_audited_as_update_on_the_farm(self):
        self.add()
        row = self.audit().get()
        self.assertEqual(row.action, "updated")
        self.assertEqual(row.object_id, self.farm.pk)
        self.assertEqual(
            row.changes["external_reference"], {"from": None, "to": "adapt:ABC"}
        )

    def test_duplicate_on_same_farm_is_409_but_other_farm_is_fine(self):
        self.assertEqual(self.add().status_code, 201)
        self.assertEqual(self.add().status_code, 409)
        self.assertEqual(self.add(external_id="XYZ").status_code, 201)
        self.assertEqual(self.add(system="other").status_code, 201)
        other = self.make_farm(name="Second")
        response = self.post(
            f"/{other.public_id}/external-references",
            {"system": "adapt", "external_id": "ABC"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.audit().count(), 4)

    def test_unknown_system_is_422(self):
        # Declared as an enum on the schema, so the framework refuses it.
        self.assertEqual(self.add(system="nope").status_code, 422)
        self.assertEqual(ExternalReference.objects.count(), 0)
        self.assertEqual(self.audit().count(), 0)

    def test_delete_reference(self):
        ref_id = self.add().json()["id"]
        response = self.delete(f"{self.path}/{ref_id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(ExternalReference.objects.count(), 0)
        row = self.audit().latest("created_at", "id")
        self.assertEqual(
            row.changes["external_reference"], {"from": "adapt:ABC", "to": None}
        )
        self.assertEqual(self.delete(f"{self.path}/{ref_id}").status_code, 404)

    def test_reference_of_another_farm_is_404_through_this_one(self):
        other = self.make_farm(name="Second")
        ref_id = self.post(
            f"/{other.public_id}/external-references",
            {"system": "adapt", "external_id": "Q"},
        ).json()["id"]
        self.assertEqual(self.delete(f"{self.path}/{ref_id}").status_code, 404)
        self.assertEqual(ExternalReference.objects.count(), 1)

    def test_writes_need_permission_reads_do_not(self):
        ref_id = self.add().json()["id"]
        self.assert_needs_permission(
            self.post(self.path, {"system": "adapt", "external_id": "2"}, user=self.a_member)
        )
        self.assert_needs_permission(self.delete(f"{self.path}/{ref_id}", user=self.a_member))
        self.assertEqual(self.get(self.path, user=self.a_member).status_code, 200)


class SeasonApiTests(FarmApiCase):
    base = "seasons"

    def body(self, **extra):
        return {
            "name": "2027",
            "start_date": "2027-01-01",
            "end_date": "2027-12-31",
            **extra,
        }

    def test_create_get_list_patch(self):
        response = self.post("", self.body())
        self.assertEqual(response.status_code, 201)
        season = response.json()
        self.assertEqual(season["crop_zone_count"], 0)
        self.assertEqual(self.get(f"/{season['id']}").json()["name"], "2027")
        listing = self.get("").json()
        self.assertEqual(listing["count"], 1)
        self.assertEqual(set(listing), {"items", "count"})

        patched = self.patch(f"/{season['id']}", {"end_date": "2027-06-30"}).json()
        self.assertEqual(patched["end_date"], "2027-06-30")
        self.assertEqual(self.audit().count(), 2)
        self.assertEqual(
            self.audit().first().changes["end_date"],
            {"from": "2027-12-31", "to": "2027-06-30"},
        )

    def test_noop_patch_writes_nothing(self):
        season = self.make_season()
        self.patch(f"/{season.public_id}", {"name": "2026"})
        self.patch(f"/{season.public_id}", {})
        self.assertEqual(self.audit().count(), 0)

    def test_invalid_dates_and_duplicates(self):
        self.assertEqual(
            self.post("", self.body(end_date="2026-01-01")).status_code, 400
        )
        season = self.make_season()
        self.assertEqual(
            self.patch(f"/{season.public_id}", {"end_date": "2025-01-01"}).status_code,
            400,
        )
        self.assertEqual(self.post("", self.body(name="2026")).status_code, 409)
        other = self.make_season(name="2028")
        self.assertEqual(
            self.patch(f"/{other.public_id}", {"name": "2026"}).status_code, 409
        )
        self.assertEqual(self.audit().count(), 0)

    def test_other_orgs_season_is_404(self):
        theirs = self.make_season(org=self.b_org, name="B")
        for response in (
            self.get(f"/{theirs.public_id}"),
            self.patch(f"/{theirs.public_id}", {"name": "x"}),
            self.delete(f"/{theirs.public_id}"),
        ):
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.get("").json()["count"], 0)

    def test_delete_and_crop_zones_block_it(self):
        season = self.make_season()
        field = self.make_field(self.make_farm())
        zone = CropZone.objects.create(
            field=field,
            season=season,
            organization=self.a_org,
            crop_code="TRZAX",
            crop_name="Wheat",
        )
        self.assertEqual(self.get(f"/{season.public_id}").json()["crop_zone_count"], 1)
        response = self.delete(f"/{season.public_id}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.audit().count(), 0)

        zone.delete()
        self.assertEqual(self.delete(f"/{season.public_id}").status_code, 204)
        self.assertEqual(self.audit().get().action, "deleted")

    def test_permission_gate(self):
        season = self.make_season()
        self.assert_needs_permission(self.post("", self.body(), user=self.a_member))
        self.assert_needs_permission(
            self.patch(f"/{season.public_id}", {"name": "x"}, user=self.a_member)
        )
        self.assert_needs_permission(self.delete(f"/{season.public_id}", user=self.a_member))
        self.assertEqual(self.get(f"/{season.public_id}", user=self.a_member).status_code, 200)
        self.assertEqual(self.audit().count(), 0)


class ReviewFixTests(FarmApiCase):
    base = "farms"

    def test_listing_farm_references_works_with_a_cold_content_type_cache(self):
        from django.contrib.contenttypes.models import ContentType

        farm = self.make_farm()
        ContentType.objects.clear_cache()
        response = self.get(f"/{farm.public_id}/external-references")
        self.assertEqual(response.status_code, 200, response.content)

    def test_farm_location_altitude_is_dropped(self):
        point = {"type": "Point", "coordinates": [69.24, 41.31, 450]}
        response = self.post("", {"name": "Z", "location": point})
        self.assertEqual(response.status_code, 201, response.content)
        farm = Farm.objects.get()
        self.assertFalse(farm.location.hasz)
        other = self.make_farm(name="Other")
        response = self.patch(f"/{other.public_id}", {"location": point})
        self.assertEqual(response.status_code, 200, response.content)
        other.refresh_from_db()
        self.assertFalse(other.location.hasz)

    def test_whitespace_names_are_rejected(self):
        for base in ("farms", "farm-clusters", "seasons"):
            self.base = base
            body = {"name": "   "}
            if base == "seasons":
                body.update(start_date="2026-01-01", end_date="2026-12-31")
            self.assertEqual(self.post("", body).status_code, 422, base)
        self.base = "farms"
        farm = self.make_farm()
        self.assertEqual(self.patch(f"/{farm.public_id}", {"name": "  "}).status_code, 422)
        self.base = "farm-clusters"
        cluster = FarmCluster.objects.create(organization=self.a_org, name="C")
        self.assertEqual(self.patch(f"/{cluster.public_id}", {"name": "  "}).status_code, 422)
        self.base = "seasons"
        season = self.make_season()
        self.assertEqual(self.patch(f"/{season.public_id}", {"name": "  "}).status_code, 422)
        self.assertEqual(self.audit().count(), 0)

    def test_patch_null_name_is_400(self):
        farm = self.make_farm()
        self.assertEqual(self.patch(f"/{farm.public_id}", {"name": None}).status_code, 400)
        self.base = "farm-clusters"
        cluster = FarmCluster.objects.create(organization=self.a_org, name="C")
        self.assertEqual(self.patch(f"/{cluster.public_id}", {"name": None}).status_code, 400)
        self.assertEqual(self.audit().count(), 0)

    def test_delete_farm_integrity_error_is_409(self):
        from unittest import mock

        from django.db import IntegrityError

        farm = self.make_farm()
        with mock.patch(
            "farms.farm_api.references_of", side_effect=IntegrityError("fk violation")
        ):
            response = self.delete(f"/{farm.public_id}")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Farm.objects.filter(pk=farm.pk).exists())
        self.assertEqual(self.audit().count(), 0)

    def test_delete_cluster_integrity_error_is_409(self):
        from unittest import mock

        from django.db import IntegrityError

        self.base = "farm-clusters"
        cluster = FarmCluster.objects.create(organization=self.a_org, name="C")
        with mock.patch.object(
            FarmCluster, "delete", side_effect=IntegrityError("fk violation")
        ):
            response = self.delete(f"/{cluster.public_id}")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(FarmCluster.objects.filter(pk=cluster.pk).exists())
        self.assertEqual(self.audit().count(), 0)
