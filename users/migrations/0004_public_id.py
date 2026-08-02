"""
Add `public_id` UUID to all users models.

Three phases in one file (safe on populated tables):
  1. Add the column as nullable — no default at the DB level.
  2. Backfill a distinct UUID per row.
  3. Tighten to unique + not-null, matching the model definition.

A single AddField(unique=True, default=uuid.uuid4) would give every existing
row the SAME uuid (the default is evaluated once) and violate uniqueness.
"""

import uuid

from django.db import migrations, models

MODELS = ["user", "region", "organization"]


def populate_public_ids(apps, schema_editor):
    for model_name in MODELS:
        model = apps.get_model("users", model_name)
        for obj in model.objects.filter(public_id__isnull=True).iterator():
            obj.public_id = uuid.uuid4()
            obj.save(update_fields=["public_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0003_alter_organization_name_alter_organization_region"),
    ]

    operations = (
        [
            migrations.AddField(
                model_name=name,
                name="public_id",
                field=models.UUIDField(editable=False, null=True),
            )
            for name in MODELS
        ]
        + [migrations.RunPython(populate_public_ids, migrations.RunPython.noop)]
        + [
            migrations.AlterField(
                model_name=name,
                name="public_id",
                field=models.UUIDField(
                    default=uuid.uuid4, editable=False, unique=True
                ),
            )
            for name in MODELS
        ]
    )
