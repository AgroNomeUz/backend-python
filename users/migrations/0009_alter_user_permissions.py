"""
Add the `inquiries.manage` permission code.

No database change — the column is a text array either way — but the choices
are part of the field, so Django wants the migration and the admin and the
API's validation both read them from here.
"""

import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0008_region_slug_soato'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='permissions',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.CharField(choices=[('equipment.manage', 'Manage equipment'), ('users.manage', 'Manage users'), ('inquiries.manage', 'Manage inquiries')], max_length=32), blank=True, default=list, help_text="Permission codes granted inside the user's own organization. Ignored for the owner, who implicitly holds all of them."),
        ),
    ]
