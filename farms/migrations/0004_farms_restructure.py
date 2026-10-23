"""
Restructure farms into the ADAPT-aligned shape.

FarmCluster, FieldBoundary, Season and CropZone are new; Farm gains a cluster;
Field swaps `area_ha` for `area` + `area_unit`, loses its `boundary` column
(history now lives in FieldBoundary) and gains a denormalised `organization`.
`CropSeason` is dropped: the tables held no data, so nothing is carried over.

Three invariants cannot be a CHECK or a plain unique constraint, because each
compares a row with a *different* table: a farm's cluster, a field's farm, and
a crop zone's field and season must all belong to the row's own organization.
They are added at the bottom as composite foreign keys onto the
`(id, organization_id)` unique constraints created here, the arrangement
`inquiry_listing_same_org_fk` uses (see inquiries/0001). NOT DEFERRABLE for the
same reason as there: none of these parents ever changes organization.
"""

import uuid

import django.contrib.gis.db.models.fields
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import farms.units


def _area_unit(**extra):
    return models.CharField(
        choices=[("har", "Hectare"), ("m2", "Square metre")],
        default="har",
        max_length=8,
        validators=[farms.units.validate_area_unit],
        **extra,
    )


def _public_id():
    return models.UUIDField(default=uuid.uuid4, editable=False, unique=True)


def _composite_fk(table, name, columns, ref_table, ref_columns):
    return migrations.RunSQL(
        sql=f"""
            ALTER TABLE {table}
              ADD CONSTRAINT {name}
              FOREIGN KEY ({columns})
              REFERENCES {ref_table} ({ref_columns})
              NOT DEFERRABLE;
        """,
        reverse_sql=f"ALTER TABLE {table} DROP CONSTRAINT {name};",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("farms", "0003_rename_farm_owner_to_organization"),
        ("users", "0011_merge_20261023_1110"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.DeleteModel(name="CropSeason"),
        # ── FarmCluster ──────────────────────────────────────────────────────
        migrations.CreateModel(
            name="FarmCluster",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("public_id", _public_id()),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="farm_clusters", to="users.organization")),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddConstraint(
            model_name="farmcluster",
            constraint=models.UniqueConstraint(fields=("organization", "name"), name="farmcluster_org_name_unique"),
        ),
        migrations.AddConstraint(
            model_name="farmcluster",
            constraint=models.UniqueConstraint(fields=("id", "organization"), name="farmcluster_id_org_unique"),
        ),
        # ── Farm ─────────────────────────────────────────────────────────────
        migrations.AddField(
            model_name="farm",
            name="cluster",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="farms", to="farms.farmcluster"),
        ),
        migrations.AddConstraint(
            model_name="farm",
            constraint=models.UniqueConstraint(fields=("organization", "name"), name="farm_org_name_unique"),
        ),
        migrations.AddConstraint(
            model_name="farm",
            constraint=models.UniqueConstraint(fields=("id", "organization"), name="farm_id_org_unique"),
        ),
        # ── Field ────────────────────────────────────────────────────────────
        migrations.RemoveField(model_name="field", name="boundary"),
        migrations.RemoveField(model_name="field", name="area_ha"),
        migrations.AlterUniqueTogether(name="field", unique_together=set()),
        migrations.AddField(
            model_name="field",
            name="soato",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="field",
            name="area",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="field",
            name="area_unit",
            field=_area_unit(help_text="UCUM code of `area`"),
        ),
        # Added nullable, filled from the farm, then tightened: safe whether
        # or not the table holds rows.
        migrations.AddField(
            model_name="field",
            name="organization",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="fields", to="users.organization"),
        ),
        migrations.RunSQL(
            sql="UPDATE farms_field f SET organization_id = fm.organization_id FROM farms_farm fm WHERE fm.id = f.farm_id;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="field",
            name="organization",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fields", to="users.organization"),
        ),
        migrations.AddConstraint(
            model_name="field",
            constraint=models.UniqueConstraint(fields=("farm", "name"), name="field_farm_name_unique"),
        ),
        migrations.AddConstraint(
            model_name="field",
            constraint=models.UniqueConstraint(fields=("id", "organization"), name="field_id_org_unique"),
        ),
        migrations.AddConstraint(
            model_name="field",
            constraint=models.CheckConstraint(
                condition=models.Q(("area__isnull", True), ("area__gt", 0), _connector="OR"),
                name="field_area_positive",
                violation_error_message="Field area must be positive.",
            ),
        ),
        # ── FieldBoundary ────────────────────────────────────────────────────
        migrations.CreateModel(
            name="FieldBoundary",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("public_id", _public_id()),
                ("geometry", django.contrib.gis.db.models.fields.MultiPolygonField(srid=4326)),
                ("area", models.DecimalField(decimal_places=4, editable=False, max_digits=14)),
                ("area_unit", _area_unit(editable=False)),
                ("valid_from", models.DateField()),
                ("valid_to", models.DateField(blank=True, null=True)),
                ("source", models.CharField(choices=[("drawn", "Drawn"), ("gps", "GPS"), ("imported", "Imported")], default="drawn", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("field", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="boundaries", to="farms.field")),
            ],
            options={"ordering": ["-valid_from", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="fieldboundary",
            constraint=models.CheckConstraint(
                condition=models.Q(("valid_to__isnull", True), ("valid_to__gte", models.F("valid_from")), _connector="OR"),
                name="fieldboundary_valid_range",
                violation_error_message="valid_to cannot be before valid_from.",
            ),
        ),
        migrations.AddConstraint(
            model_name="fieldboundary",
            constraint=models.UniqueConstraint(
                condition=models.Q(("valid_to__isnull", True)),
                fields=("field",),
                name="fieldboundary_one_open_per_field",
                violation_error_message="This field already has an open boundary.",
            ),
        ),
        # ── Season ───────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="Season",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("public_id", _public_id()),
                ("name", models.CharField(max_length=100)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="seasons", to="users.organization")),
            ],
            options={"ordering": ["-start_date", "name"]},
        ),
        migrations.AddConstraint(
            model_name="season",
            constraint=models.UniqueConstraint(fields=("organization", "name"), name="season_org_name_unique"),
        ),
        migrations.AddConstraint(
            model_name="season",
            constraint=models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="season_end_after_start",
                violation_error_message="end_date cannot be before start_date.",
            ),
        ),
        migrations.AddConstraint(
            model_name="season",
            constraint=models.UniqueConstraint(fields=("id", "organization"), name="season_id_org_unique"),
        ),
        # ── CropZone ─────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="CropZone",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("public_id", _public_id()),
                ("crop_code", models.CharField(db_index=True, help_text="EPPO code, uppercase", max_length=10)),
                ("crop_name", models.CharField(help_text="Local display label", max_length=255)),
                ("geometry", django.contrib.gis.db.models.fields.MultiPolygonField(blank=True, null=True, srid=4326)),
                ("area", models.DecimalField(blank=True, decimal_places=4, max_digits=14, null=True)),
                ("area_unit", _area_unit()),
                ("planting_date", models.DateField(blank=True, null=True)),
                ("expected_harvest_date", models.DateField(blank=True, null=True)),
                ("harvest_date", models.DateField(blank=True, null=True)),
                ("notes", models.TextField(blank=True)),
                ("field", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="crop_zones", to="farms.field")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="crop_zones", to="users.organization")),
                ("season", models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name="crop_zones", to="farms.season")),
            ],
            options={"ordering": ["-season__start_date", "field", "crop_code"]},
        ),
        migrations.AddConstraint(
            model_name="cropzone",
            constraint=models.CheckConstraint(
                condition=models.Q(("planting_date__isnull", True), ("expected_harvest_date__isnull", True), ("expected_harvest_date__gte", models.F("planting_date")), _connector="OR"),
                name="cropzone_harvest_after_planting",
                violation_error_message="expected_harvest_date cannot be before planting_date.",
            ),
        ),
        migrations.AddConstraint(
            model_name="cropzone",
            constraint=models.CheckConstraint(
                condition=models.Q(("area__isnull", True), ("area__gt", 0), _connector="OR"),
                name="cropzone_area_positive",
                violation_error_message="Crop zone area must be positive.",
            ),
        ),
        # ── same-organization composite foreign keys ─────────────────────────
        _composite_fk("farms_farm", "farm_cluster_same_org_fk", "cluster_id, organization_id", "farms_farmcluster", "id, organization_id"),
        _composite_fk("farms_field", "field_farm_same_org_fk", "farm_id, organization_id", "farms_farm", "id, organization_id"),
        _composite_fk("farms_cropzone", "cropzone_field_same_org_fk", "field_id, organization_id", "farms_field", "id, organization_id"),
        _composite_fk("farms_cropzone", "cropzone_season_same_org_fk", "season_id, organization_id", "farms_season", "id, organization_id"),
    ]
