"""
A listing's price may not be negative.

`Listing.price` has carried `MinValueValidator(0)` since 0001, but a validator
only runs under `full_clean()`, which no write path calls — so the rule was
documentation, not a guarantee, and a negative price saved happily and then
sorted to the front of the public feed under `sort=price_asc`. Stated here for
the same reason as the invariants in 0001: the database is the one place a
view cannot forget it.

Only `listings.0001_initial` is listed as a dependency; it already depends on
equipment, users and the swappable user model.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("listings", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="listing",
            constraint=models.CheckConstraint(
                condition=models.Q(("price__gte", 0)),
                name="listing_price_non_negative",
                violation_error_message="A price cannot be negative.",
            ),
        ),
    ]
