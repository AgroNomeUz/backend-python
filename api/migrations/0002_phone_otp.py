"""
One-time passcodes for phone-first login.

The table doubles as the rate-limiting substrate — the resend cooldown and
the per-phone / per-IP request caps are counted from these rows rather than
from a cache, so they hold across processes and restarts. Hence the two
`(…, -created_at)` indexes: every OTP request runs both counts.
"""

import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='PhoneOtp',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('phone', models.CharField(db_index=True, max_length=32)),
                ('code_hash', models.CharField(max_length=128)),
                ('expires_at', models.DateTimeField()),
                ('attempts', models.PositiveSmallIntegerField(default=0, help_text='Verification attempts against this code; capped to stop guessing')),
                ('verified_at', models.DateTimeField(blank=True, help_text='Set once the right code was presented; the code is then burnt', null=True)),
                ('signup_claimed_at', models.DateTimeField(blank=True, help_text='Set when the signup token minted from this row was exchanged', null=True)),
                ('ip', models.GenericIPAddressField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                'verbose_name': 'Phone OTP',
                'verbose_name_plural': 'Phone OTPs',
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['phone', '-created_at'], name='api_phoneot_phone_05da51_idx'), models.Index(fields=['ip', '-created_at'], name='api_phoneot_ip_0abfa2_idx')],
            },
        ),
    ]
