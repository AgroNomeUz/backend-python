"""
Add `public_id` UUID to all farms models.

Three phases in one file (safe on populated tables): add nullable column,
backfill distinct UUIDs, then tighten to unique + not-null. See
users/migrations/0004_public_id.py for the rationale.
"""

import uuid

from django.db import migrations, models

MODELS = ["farm", "field", "cropseason"]


def populate_public_ids(apps, schema_editor):
    for model_name in MODELS:
        model = apps.get_model("farms", model_name)
        for obj in model.objects.filter(public_id__isnull=True).iterator():
            obj.public_id = uuid.uuid4()
            obj.save(update_fields=["public_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("farms", "0001_initial"),
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
