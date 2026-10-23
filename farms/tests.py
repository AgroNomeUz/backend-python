"""
farms/tests.py
The database invariants under the farms models, and the little code around them.

Written through the ORM rather than the API, for the reason inquiries/tests.py
gives: a view that forgot its guard could not make these pass, so they test
Postgres and not the endpoints.
"""

from datetime import date
from decimal import Decimal

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Polygon
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from farms.models import (
    CropZone,
    Farm,
    FarmCluster,
    Field,
    FieldBoundary,
    Season,
    geometry_area_hectares,
)
from farms.test_support import FarmsTestCase, multipolygon_geojson, polygon_geojson
from farms.units import to_hectares, validate_area_unit


def square(lon=69.0, lat=41.0, size=0.001) -> MultiPolygon:
    return MultiPolygon(
        GEOSGeometry(str(polygon_geojson(lon, lat, size)).replace("'", '"'), srid=4326)
    )


class InvariantTestCase(FarmsTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.a_farm = Farm.objects.create(organization=cls.a_org, name="A farm")
        cls.b_farm = Farm.objects.create(organization=cls.b_org, name="B farm")
        cls.a_field = Field.objects.create(
            organization=cls.a_org, farm=cls.a_farm, name="A field"
        )
        cls.b_field = Field.objects.create(
            organization=cls.b_org, farm=cls.b_farm, name="B field"
        )
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

    def assertRejected(self, make):
        """`make()` must fail in Postgres, and leave the test transaction usable."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make()


class FarmClusterTests(InvariantTestCase):
    def test_cluster_name_unique_per_org_but_not_across_orgs(self):
        FarmCluster.objects.create(organization=self.a_org, name="North")
        FarmCluster.objects.create(organization=self.b_org, name="North")
        self.assertRejected(
            lambda: FarmCluster.objects.create(organization=self.a_org, name="North")
        )

    def test_farm_name_unique_per_org(self):
        self.assertRejected(
            lambda: Farm.objects.create(organization=self.a_org, name="A farm")
        )
        Farm.objects.create(organization=self.b_org, name="A farm")

    def test_cross_org_cluster_on_a_farm_is_rejected(self):
        foreign = FarmCluster.objects.create(organization=self.b_org, name="Theirs")
        self.assertRejected(
            lambda: Farm.objects.create(
                organization=self.a_org, name="Mixed", cluster=foreign
            )
        )
        own = FarmCluster.objects.create(organization=self.a_org, name="Mine")
        Farm.objects.create(organization=self.a_org, name="Fine", cluster=own)

    def test_moving_a_farm_to_another_orgs_cluster_is_rejected(self):
        foreign = FarmCluster.objects.create(organization=self.b_org, name="Theirs")
        self.a_farm.cluster = foreign
        self.assertRejected(lambda: self.a_farm.save())

    def test_deleting_a_cluster_detaches_its_farms(self):
        cluster = FarmCluster.objects.create(organization=self.a_org, name="Gone")
        farm = Farm.objects.create(
            organization=self.a_org, name="Member", cluster=cluster
        )
        cluster.delete()
        farm.refresh_from_db()
        self.assertIsNone(farm.cluster)


class FieldTests(InvariantTestCase):
    def test_cross_org_farm_on_a_field_is_rejected(self):
        self.assertRejected(
            lambda: Field.objects.create(
                organization=self.a_org, farm=self.b_farm, name="Sneaky"
            )
        )

    def test_field_name_unique_per_farm(self):
        self.assertRejected(
            lambda: Field.objects.create(
                organization=self.a_org, farm=self.a_farm, name="A field"
            )
        )

    def test_area_must_be_positive_in_the_database(self):
        for bad in (Decimal("0"), Decimal("-1")):
            self.assertRejected(
                lambda: Field.objects.create(
                    organization=self.a_org, farm=self.a_farm, name="Bad", area=bad
                )
            )
        Field.objects.create(organization=self.a_org, farm=self.a_farm, name="None")
        Field.objects.create(
            organization=self.a_org, farm=self.a_farm, name="Ok", area=Decimal("1.5")
        )

    def test_clean_rejects_non_positive_area_and_foreign_farm(self):
        field = Field(
            organization=self.a_org, farm=self.a_farm, name="X", area=Decimal("0")
        )
        with self.assertRaises(ValidationError) as ctx:
            field.clean()
        self.assertIn("area", ctx.exception.message_dict)
        field = Field(organization=self.a_org, farm=self.b_farm, name="X")
        with self.assertRaises(ValidationError) as ctx:
            field.clean()
        self.assertIn("farm", ctx.exception.message_dict)

    def test_default_unit_is_hectare_and_unknown_unit_fails_validation(self):
        self.assertEqual(self.a_field.area_unit, "har")
        field = Field(
            organization=self.a_org, farm=self.a_farm, name="U", area_unit="acre"
        )
        with self.assertRaises(ValidationError) as ctx:
            field.full_clean()
        self.assertIn("area_unit", ctx.exception.message_dict)

    def test_deleting_an_org_cascades_through_all_farm_tables(self):
        FieldBoundary.objects.create(
            field=self.b_field, geometry=square(), valid_from=date(2026, 1, 1)
        )
        CropZone.objects.create(
            organization=self.b_org,
            field=self.b_field,
            season=self.b_season,
            crop_code="TRZAX",
            crop_name="Wheat",
        )
        self.b_org.delete()
        self.assertFalse(Field.objects.filter(pk=self.b_field.pk).exists())
        self.assertFalse(CropZone.objects.exists())


class FieldBoundaryTests(InvariantTestCase):
    def test_one_open_boundary_per_field(self):
        FieldBoundary.objects.create(
            field=self.a_field, geometry=square(), valid_from=date(2026, 1, 1)
        )
        self.assertRejected(
            lambda: FieldBoundary.objects.create(
                field=self.a_field, geometry=square(), valid_from=date(2026, 6, 1)
            )
        )
        # Another field may have its own open boundary.
        FieldBoundary.objects.create(
            field=self.b_field, geometry=square(), valid_from=date(2026, 1, 1)
        )

    def test_closed_boundaries_stack_beside_an_open_one(self):
        for year in (2023, 2024):
            FieldBoundary.objects.create(
                field=self.a_field,
                geometry=square(),
                valid_from=date(year, 1, 1),
                valid_to=date(year, 12, 31),
            )
        FieldBoundary.objects.create(
            field=self.a_field, geometry=square(), valid_from=date(2025, 1, 1)
        )
        self.assertEqual(self.a_field.boundaries.count(), 3)

    def test_valid_to_cannot_precede_valid_from(self):
        self.assertRejected(
            lambda: FieldBoundary.objects.create(
                field=self.a_field,
                geometry=square(),
                valid_from=date(2026, 5, 2),
                valid_to=date(2026, 5, 1),
            )
        )
        FieldBoundary.objects.create(
            field=self.a_field,
            geometry=square(),
            valid_from=date(2026, 5, 1),
            valid_to=date(2026, 5, 1),
        )

    def test_area_is_computed_on_save_in_hectares(self):
        boundary = FieldBoundary.objects.create(
            field=self.a_field,
            geometry=square(size=0.001),
            valid_from=date(2026, 1, 1),
            area=Decimal("999"),  # ignored: always derived from the geometry
        )
        boundary.refresh_from_db()
        self.assertEqual(boundary.area_unit, "har")
        # 0.001 degrees at 41N: ~111.2 m north-south by ~83.9 m east-west.
        self.assertAlmostEqual(float(boundary.area), 0.933, delta=0.02)

    def test_area_helper_known_square(self):
        # 0.01 x 0.01 degrees on the equator is ~1113 m east-west by ~1106 m north-south: ~123.1 ha (a degree of longitude is longer than one of latitude on the spheroid).
        ha = geometry_area_hectares(square(lon=0.0, lat=0.0, size=0.01))
        self.assertAlmostEqual(float(ha), 123.1, delta=0.3)

    def test_geojson_polygon_and_multipolygon_helpers_agree(self):
        poly = GEOSGeometry(str(polygon_geojson()).replace("'", '"'))
        multi = GEOSGeometry(str(multipolygon_geojson()).replace("'", '"'))
        self.assertIsInstance(poly, Polygon)
        self.assertEqual(geometry_area_hectares(multi), geometry_area_hectares(poly))


class SeasonTests(InvariantTestCase):
    def test_season_name_unique_per_org(self):
        self.assertRejected(
            lambda: Season.objects.create(
                organization=self.a_org,
                name="2026",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 2, 1),
            )
        )

    def test_end_date_cannot_precede_start_date(self):
        self.assertRejected(
            lambda: Season.objects.create(
                organization=self.a_org,
                name="Backwards",
                start_date=date(2026, 2, 1),
                end_date=date(2026, 1, 1),
            )
        )


class CropZoneTests(InvariantTestCase):
    def make(self, **overrides):
        values = dict(
            organization=self.a_org,
            field=self.a_field,
            season=self.a_season,
            crop_code="TRZAX",
            crop_name="Wheat",
        )
        values.update(overrides)
        return CropZone.objects.create(**values)

    def test_valid_zone(self):
        zone = self.make(area=Decimal("2"), planting_date=date(2026, 3, 1))
        self.assertEqual(zone.area_unit, "har")

    def test_season_of_another_org_is_rejected(self):
        self.assertRejected(lambda: self.make(season=self.b_season))

    def test_field_of_another_org_is_rejected(self):
        self.assertRejected(lambda: self.make(field=self.b_field))

    def test_organization_must_match_both_parents(self):
        self.assertRejected(lambda: self.make(organization=self.b_org))

    def test_clean_flags_season_org_mismatch(self):
        zone = CropZone(
            organization=self.a_org,
            field=self.a_field,
            season=self.b_season,
            crop_code="TRZAX",
            crop_name="Wheat",
        )
        with self.assertRaises(ValidationError) as ctx:
            zone.clean()
        self.assertIn("season", ctx.exception.message_dict)

    def test_harvest_cannot_precede_planting(self):
        self.assertRejected(
            lambda: self.make(
                planting_date=date(2026, 5, 1),
                expected_harvest_date=date(2026, 4, 1),
            )
        )
        # One date alone is fine.
        self.make(expected_harvest_date=date(2026, 4, 1))

    def test_ordering_ends_with_a_unique_tie_break(self):
        self.assertEqual(CropZone._meta.ordering[-1], "id")

    def test_area_must_be_positive(self):
        self.assertRejected(lambda: self.make(area=Decimal("0")))

    def test_season_with_zones_is_restricted_from_deletion(self):
        from django.db.models import RestrictedError

        self.make()
        with self.assertRaises(RestrictedError):
            self.a_season.delete()


class UnitTests(FarmsTestCase):
    def test_to_hectares(self):
        self.assertEqual(to_hectares(Decimal("2.5"), "har"), Decimal("2.5"))
        self.assertEqual(to_hectares(Decimal("25000"), "m2"), Decimal("2.5"))

    def test_unknown_unit_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_area_unit("acre")
        with self.assertRaises(ValidationError):
            to_hectares(Decimal("1"), "acre")
