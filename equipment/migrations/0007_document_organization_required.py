"""
Make `Document.organization` required.

A document with no owning organization is unreachable by every org-scoped
endpoint and invisible to the audit trail — it is not a valid state, only one
the schema used to allow.

Existing NULL rows are not guessed at or deleted: the generic `related_object`
would have to be resolved per content type to infer an owner, and silently
discarding uploaded files during a migration is worse than stopping. The guard
below fails with a count and instructions instead; resolve those rows, then
re-run. On a database with no orphans it is a no-op.
"""

from django.db import migrations, models
import django.db.models.deletion


def refuse_orphaned_documents(apps, schema_editor):
    Document = apps.get_model("equipment", "Document")
    orphans = Document.objects.filter(organization__isnull=True).count()
    if orphans:
        raise RuntimeError(
            f"{orphans} Document row(s) have no organization. Assign each to "
            "its owning organization (or delete it) before applying this "
            "migration — see equipment.Document.related_object for what each "
            "one is attached to."
        )


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0007_identity_fields"),
        ("equipment", "0006_asset_status_created_idx"),
    ]

    operations = [
        migrations.RunPython(refuse_orphaned_documents, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="document",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="documents",
                to="users.organization",
            ),
        ),
    ]
