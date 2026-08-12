"""
Phone-first identity: the login identifier, a single-field name, a Telegram
handle, and the individual/legal-entity split on Organization.

`phone` is nullable rather than blank-able on purpose. A unique column treats
every empty string as equal, so `blank=True, unique=True` would permit exactly
one phoneless account; NULLs don't collide. Accounts predating phone auth —
and anything made by `createsuperuser` — legitimately have none.

`full_name` is backfilled from Django's first/last pair so no existing account
renders nameless. The two original fields stay for admin compatibility.
"""

from django.db import migrations, models
from django.db.models import Value
from django.db.models.functions import Concat, Trim


def backfill_full_name(apps, schema_editor):
    User = apps.get_model("users", "User")
    User.objects.filter(full_name="").update(
        full_name=Trim(Concat("first_name", Value(" "), "last_name"))
    )


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0006_org_permissions"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="phone",
            field=models.CharField(
                max_length=32,
                null=True,
                blank=True,
                unique=True,
                db_index=True,
                help_text="E.164, e.g. +998901234567. The default login identifier.",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="full_name",
            field=models.CharField(max_length=255, blank=True, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="user",
            name="telegram",
            field=models.CharField(
                max_length=64,
                blank=True,
                default="",
                help_text="Telegram handle without the @, for future notifications",
            ),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_full_name, migrations.RunPython.noop),
        migrations.AddField(
            model_name="organization",
            name="entity_type",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("individual", "Individual"),
                    ("legal_entity", "Legal entity"),
                ],
                default="individual",
                db_index=True,
                help_text=(
                    "Promoted to 'legal_entity' by ONEID verification, which is "
                    "what fills the legal fields above."
                ),
            ),
        ),
    ]
