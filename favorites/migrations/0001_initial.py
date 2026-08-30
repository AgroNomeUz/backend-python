"""
Initial favorites schema.

One table and one invariant: `favorite_unique_per_member` over
`(organization, user, listing)`, which is what makes `PUT /favorites/{id}`
idempotent in the database rather than only in the view.

No composite foreign key here, unlike inquiries/0001. `organization` is
denormalised off `user.organization` and would want the same treatment, but
`User.organization` is SET_NULL — removing an organization nulls the column on
the user while the copy on this row stands, and the constraint would then fail
the delete rather than the write. See the note at the bottom of
`Favorite.Meta`.
"""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('listings', '0003_listing_listing_id_org_unique'),
        ('users', '0009_alter_user_permissions'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Favorite',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('listing', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='favorites', to='listings.listing')),
                ('organization', models.ForeignKey(help_text='The organization whose shared shortlist this is on', on_delete=django.db.models.deletion.CASCADE, related_name='favorites', to='users.organization')),
                ('user', models.ForeignKey(help_text='Member who saved it', on_delete=django.db.models.deletion.CASCADE, related_name='favorites', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['user', '-created_at'], name='favorite_user_idx'), models.Index(fields=['organization', '-created_at'], name='favorite_org_idx')],
                'constraints': [models.UniqueConstraint(fields=('organization', 'user', 'listing'), name='favorite_unique_per_member')],
            },
        ),
    ]
