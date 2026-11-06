import hashlib

from django.db import migrations, models


def hash_tokens(apps, schema_editor):
    RefreshToken = apps.get_model("api", "RefreshToken")
    for row in RefreshToken.objects.iterator():
        row.token_hash = hashlib.sha256(row.token_hash.encode()).hexdigest()
        row.save(update_fields=["token_hash"])


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0003_refreshtoken_rotated_at"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="refreshtoken",
            name="api_refresh_token_fbb98a_idx",
        ),
        migrations.RenameField(
            model_name="refreshtoken",
            old_name="token",
            new_name="token_hash",
        ),
        # Hashes cannot be undone: migrating back leaves digests in `token`,
        # which match nothing, so everyone simply signs in again.
        migrations.RunPython(hash_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="refreshtoken",
            name="token_hash",
            field=models.CharField(max_length=64, unique=True),
        ),
    ]
