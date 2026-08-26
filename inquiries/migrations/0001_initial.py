"""
Initial inquiries schema.

Both ordinary constraints — an organization may not enquire of itself, and a
date range may not run backwards — live in `Inquiry.Meta`. The third invariant
cannot: `provider_organization` is denormalised off `listing.organization`, and
a Postgres CHECK may not reference another table. It is added at the bottom of
this file as a composite foreign key onto
`(listings_listing.id, listings_listing.organization_id)`, which the unique
constraint in listings/0003 exists to back — the same arrangement
`listing_asset_same_org_fk` uses against `equipment_asset`.
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
            name='Inquiry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('message', models.TextField()),
                ('start_date', models.DateField(blank=True, null=True)),
                ('end_date', models.DateField(blank=True, null=True)),
                ('read_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, help_text='Member who sent it; null once the account is deleted', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='inquiries_created', to=settings.AUTH_USER_MODEL)),
                ('customer_organization', models.ForeignKey(help_text='The organization asking', on_delete=django.db.models.deletion.CASCADE, related_name='inquiries_sent', to='users.organization')),
                ('listing', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='inquiries', to='listings.listing')),
                ('provider_organization', models.ForeignKey(help_text='The organization that owns the listing', on_delete=django.db.models.deletion.CASCADE, related_name='inquiries_received', to='users.organization')),
                ('read_by', models.ForeignKey(blank=True, help_text="Member who picked it up. In a shared inbox 'read' means nothing without a name against it.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='inquiries_handled', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Inquiry',
                'verbose_name_plural': 'Inquiries',
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['provider_organization', '-created_at'], name='inquiry_inbox_idx'), models.Index(fields=['customer_organization', '-created_at'], name='inquiry_sent_idx'), models.Index(condition=models.Q(('read_at__isnull', True)), fields=['provider_organization'], name='inquiry_unread_idx')],
                'constraints': [models.CheckConstraint(condition=models.Q(('customer_organization', models.F('provider_organization')), _negated=True), name='inquiry_not_to_own_org', violation_error_message='You cannot send an inquiry to your own organization.'), models.CheckConstraint(condition=models.Q(('start_date__isnull', True), ('end_date__isnull', True), ('end_date__gte', models.F('start_date')), _connector='OR'), name='inquiry_dates_ordered', violation_error_message='The end date cannot precede the start date.')],
            },
        ),
        # ── the same-org invariant ───────────────────────────────────────────
        #
        # `provider_organization` is a copy of `listing.organization`, kept so
        # the inbox is one filter rather than a join. A copy is only safe if
        # something keeps it in step, and that something is here rather than in
        # the view, because a view can be forgotten.
        #
        # NOT DEFERRABLE, like the equivalent on listings: a listing never
        # moves between organizations, so there is no legitimate window in
        # which the pair is briefly inconsistent, and an immediate failure
        # names the statement that caused it rather than COMMIT.
        migrations.RunSQL(
            sql="""
                ALTER TABLE inquiries_inquiry
                  ADD CONSTRAINT inquiry_listing_same_org_fk
                  FOREIGN KEY (listing_id, provider_organization_id)
                  REFERENCES listings_listing (id, organization_id)
                  NOT DEFERRABLE;
            """,
            reverse_sql="""
                ALTER TABLE inquiries_inquiry
                  DROP CONSTRAINT inquiry_listing_same_org_fk;
            """,
        ),
    ]
