"""
Add `InquiryMessage`: the reply thread on top of an `Inquiry`.

No composite foreign key here, unlike `inquiries/0001`: a message's sender is
legitimately either party to the inquiry, so nothing is denormalised onto this
table for one to point at. `sender_side` (renter/provider) is enough to make
"the sender is a party" true by construction — see the model docstring.

`0003` backfills a first `InquiryMessage` for every `Inquiry` that predates
this migration, from its `message`/`created_by`/`created_at`.
"""

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inquiries', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='InquiryMessage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('sender_side', models.CharField(choices=[('renter', 'Renter'), ('provider', 'Provider')], max_length=8)),
                ('body', models.TextField()),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('created_by', models.ForeignKey(blank=True, help_text='Member who sent it; null once the account is deleted', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='inquiry_messages_created', to=settings.AUTH_USER_MODEL)),
                ('inquiry', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='inquiries.inquiry')),
            ],
            options={
                'ordering': ['created_at', 'id'],
                'indexes': [models.Index(fields=['inquiry', 'created_at'], name='inquiry_thread_idx')],
            },
        ),
    ]
