"""
Rename `Farm.owner` to `Farm.organization`.

The column always pointed at Organization, but `owner` reads as a User FK at
a glance — and every other org-owned model calls the field `organization`.
A RenameField, so the data is untouched and the reverse is exact.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("farms", "0002_public_id"),
    ]

    operations = [
        migrations.RenameField(
            model_name="farm",
            old_name="owner",
            new_name="organization",
        ),
    ]
