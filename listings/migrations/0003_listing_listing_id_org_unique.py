"""
Give `Listing` a unique `(id, organization)` pair.

Nothing in the listings app needs it. It exists so another table can point a
composite foreign key at the pair, which is how `inquiries.Inquiry` keeps its
denormalised `provider_organization` in step with `listing.organization` —
the same job `asset_id_org_unique` (equipment/0008) does for this table.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0008_asset_id_org_unique'),
        ('listings', '0002_listing_listing_price_non_negative'),
        ('users', '0009_alter_user_permissions'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='listing',
            constraint=models.UniqueConstraint(fields=('id', 'organization'), name='listing_id_org_unique'),
        ),
    ]
