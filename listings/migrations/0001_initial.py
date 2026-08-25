"""
Initial listings schema.

Two of the three invariants §0.5 asks for are ordinary Django constraints and
live in `Listing.Meta`. The third — a listing belongs to the same organization
as its asset — cannot be: a Postgres CHECK may not reference another table. It
is added at the bottom of this file as a composite foreign key onto
`(equipment_asset.id, equipment_asset.organization_id)`, which the unique
constraint in equipment/0008 exists to back.
"""


import django.core.validators
import django.db.models.deletion
import listings.models
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('equipment', '0008_asset_id_org_unique'),
        ('users', '0008_region_slug_soato'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Listing',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('listing_type', models.CharField(choices=[('rent', 'For rent'), ('sale', 'For sale')], db_index=True, max_length=20)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('active', 'Active'), ('paused', 'Paused'), ('archived', 'Archived')], db_index=True, default='draft', max_length=20)),
                ('title', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True)),
                ('availability', models.CharField(blank=True, help_text="Free text, as the frontend sends it — e.g. 'weekdays, March–October'", max_length=255)),
                ('price', models.DecimalField(decimal_places=2, max_digits=12, validators=[django.core.validators.MinValueValidator(0)])),
                ('currency', models.CharField(default='UZS', max_length=3)),
                ('price_unit', models.CharField(choices=[('hour', 'Per hour'), ('day', 'Per day'), ('hectare', 'Per hectare'), ('shift', 'Per shift'), ('operation', 'Per operation'), ('km', 'Per kilometre'), ('total', 'Total price')], max_length=20)),
                ('has_operator', models.BooleanField(default=False)),
                ('has_delivery', models.BooleanField(default=False)),
                ('district', models.CharField(blank=True, max_length=120)),
                ('published_at', models.DateTimeField(blank=True, help_text='First time this listing went active; never cleared afterwards', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('asset', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='listings', to='equipment.asset')),
                ('created_by', models.ForeignKey(blank=True, help_text='Member who published it; null once the account is deleted', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='listings_created', to=settings.AUTH_USER_MODEL)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='listings', to='users.organization')),
                ('region', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='listings', to='users.region')),
            ],
            options={
                'verbose_name': 'Listing',
                'verbose_name_plural': 'Listings',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='ListingImage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('file', models.FileField(upload_to=listings.models.listing_image_path, validators=[django.core.validators.FileExtensionValidator(['jpg', 'jpeg', 'png', 'webp'])])),
                ('sort_order', models.PositiveSmallIntegerField(default=0)),
                ('is_primary', models.BooleanField(default=False, help_text='The card image. At most one per listing.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('listing', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='images', to='listings.listing')),
            ],
            options={
                'verbose_name': 'Listing image',
                'verbose_name_plural': 'Listing images',
                'ordering': ['sort_order', 'pk'],
            },
        ),
        migrations.AddIndex(
            model_name='listing',
            index=models.Index(fields=['status', '-created_at'], name='listing_status_created_idx'),
        ),
        migrations.AddIndex(
            model_name='listing',
            index=models.Index(fields=['status', 'price'], name='listing_status_price_idx'),
        ),
        migrations.AddIndex(
            model_name='listing',
            index=models.Index(fields=['organization', 'status'], name='listing_org_status_idx'),
        ),
        migrations.AddConstraint(
            model_name='listing',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 'active')), fields=('asset', 'listing_type'), name='listing_one_active_per_asset_type', violation_error_message='This machine already has an active listing of that type.'),
        ),
        migrations.AddConstraint(
            model_name='listing',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('listing_type', 'sale'), ('price_unit', 'total')), models.Q(models.Q(('listing_type', 'sale'), _negated=True), models.Q(('price_unit', 'total'), _negated=True)), _connector='OR'), name='listing_total_price_unit_iff_sale', violation_error_message="price_unit 'total' is for sale listings, and a sale must be priced as a total."),
        ),
        migrations.AddConstraint(
            model_name='listingimage',
            constraint=models.UniqueConstraint(condition=models.Q(('is_primary', True)), fields=('listing',), name='listing_one_primary_image', violation_error_message='A listing can only have one primary image.'),
        ),
        # ── the same-org invariant (§0.5) ────────────────────────────────────
        #
        # NOT DEFERRABLE, unlike the foreign keys Django generates: an asset
        # never moves between organizations (nothing writes Asset.organization
        # after creation), so there is no legitimate window in which the pair
        # is briefly inconsistent, and an immediate failure points at the
        # statement that caused it rather than at COMMIT.
        migrations.RunSQL(
            sql="""
                ALTER TABLE listings_listing
                  ADD CONSTRAINT listing_asset_same_org_fk
                  FOREIGN KEY (asset_id, organization_id)
                  REFERENCES equipment_asset (id, organization_id)
                  NOT DEFERRABLE;
            """,
            reverse_sql="""
                ALTER TABLE listings_listing
                  DROP CONSTRAINT listing_asset_same_org_fk;
            """,
        ),
    ]
