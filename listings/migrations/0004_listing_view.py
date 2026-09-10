"""
The listing views counter (§7).

`ListingView.organization` is a copy of `listing.organization`, so the owner
dashboard can count a whole org's views with one filter instead of a join. The
copy is kept honest the way `inquiries/0001` keeps `provider_organization`
honest — a composite foreign key onto
`(listings_listing.id, listings_listing.organization_id)`, which the unique
constraint from `listings/0003` exists to back. A CHECK could not express it:
it would have to reference another table.
"""

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0003_listing_listing_id_org_unique'),
        ('users', '0009_alter_user_permissions'),
    ]

    operations = [
        migrations.CreateModel(
            name='ListingView',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('viewer_key', models.CharField(help_text='Salted hash of the viewer, for de-duplication only', max_length=64)),
                ('viewed_on', models.DateField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('listing', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='views', to='listings.listing')),
                ('organization', models.ForeignKey(help_text='The organization that owns the listing, for the dashboard query', on_delete=django.db.models.deletion.CASCADE, related_name='listing_views', to='users.organization')),
            ],
            options={
                'verbose_name': 'Listing view',
                'verbose_name_plural': 'Listing views',
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['organization', 'viewed_on'], name='listing_view_org_day_idx')],
                'constraints': [models.UniqueConstraint(fields=('listing', 'viewer_key', 'viewed_on'), name='listing_view_once_per_viewer_day')],
            },
        ),
        # ── the same-org invariant ───────────────────────────────────────────
        #
        # NOT DEFERRABLE, like the equivalent on listings and inquiries: a
        # listing never moves between organizations, so there is no legitimate
        # window in which the pair is briefly inconsistent, and an immediate
        # failure names the statement that caused it rather than COMMIT.
        migrations.RunSQL(
            sql="""
                ALTER TABLE listings_listingview
                  ADD CONSTRAINT listing_view_same_org_fk
                  FOREIGN KEY (listing_id, organization_id)
                  REFERENCES listings_listing (id, organization_id)
                  NOT DEFERRABLE;
            """,
            reverse_sql="""
                ALTER TABLE listings_listingview
                  DROP CONSTRAINT listing_view_same_org_fk;
            """,
        ),
    ]
