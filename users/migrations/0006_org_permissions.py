"""
Organization member management: per-user permission codes, the one-time
password flag, and case-insensitive email uniqueness.

The unique constraint is what makes "sign in with your email" well-defined,
so existing emails are normalised to lowercase first. If two accounts already
share an email (differing only in case or not), the constraint will refuse to
build — that is the point: resolve the duplicate, then re-run.
"""

import django.contrib.postgres.fields
import django.db.models.functions.text
from django.db import migrations, models
from django.db.models.functions import Lower


def normalize_emails(apps, schema_editor):
    User = apps.get_model("users", "User")
    User.objects.exclude(email="").update(email=Lower("email"))


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('users', '0005_organization_is_verified'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='must_change_password',
            field=models.BooleanField(default=False, help_text='Set when the account is created or its password is reset by an admin. Cleared once the user sets a password themselves.'),
        ),
        migrations.AddField(
            model_name='user',
            name='permissions',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.CharField(choices=[('equipment.manage', 'Manage equipment'), ('users.manage', 'Manage users')], max_length=32), blank=True, default=list, help_text="Permission codes granted inside the user's own organization. Ignored for the owner, who implicitly holds all of them."),
        ),
        migrations.RunPython(normalize_emails, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='user',
            constraint=models.UniqueConstraint(django.db.models.functions.text.Lower('email'), condition=models.Q(('email', ''), _negated=True), name='user_email_ci_unique'),
        ),
    ]
