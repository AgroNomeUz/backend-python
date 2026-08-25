"""
Region gains the two identifiers §3 of the API contract needs: a `slug` to
key `/regions/{slug}` on, and `soato` — Uzbekistan's national classifier of
administrative-territorial objects, which is what regions, districts and
cities are actually identified by here.

Both are added blank, backfilled, and only then made unique: adding a unique
column to a populated table in one step would collide on the first two rows.
"""

from django.db import migrations, models
from django.utils.text import slugify

# Region-level SOATO codes, keyed by the ISO 3166-2:UZ code already stored on
# each row. Districts and cities extend these to seven digits.
SOATO_BY_CODE = {
    "AN": "1703",
    "BU": "1706",
    "FA": "1730",
    "JI": "1708",
    "NG": "1714",
    "NW": "1712",
    "QA": "1710",
    "QR": "1735",
    "SA": "1718",
    "SI": "1724",
    "SU": "1722",
    "TK": "1727",
    "TO": "1726",
    "XO": "1733",
}


def backfill(apps, schema_editor):
    """
    Fill both columns for every region already in the database.

    `Region.save()` derives the slug for new rows, but historical models in a
    migration carry no custom methods — so the same rule is applied by hand
    here, collision counter included.
    """
    Region = apps.get_model("users", "Region")
    taken = set()
    for region in Region.objects.order_by("pk"):
        base = slugify(region.name)[:60] or "region"
        candidate = base
        counter = 2
        while candidate in taken:
            candidate = f"{base}-{counter}"
            counter += 1
        taken.add(candidate)

        region.slug = candidate
        # Left NULL for anything outside the seeded 14 — a NULL doesn't
        # collide under the unique constraint applied below, an empty string
        # would.
        region.soato = SOATO_BY_CODE.get(region.code)
        region.save(update_fields=["slug", "soato"])


def unfill(apps, schema_editor):
    """Reverse is a no-op: the columns themselves are dropped after this."""


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0007_identity_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="region",
            name="slug",
            # db_index=False on the way in: SlugField indexes by default,
            # and the unique constraint applied below builds the same
            # varchar_pattern_ops index again under a name that would collide.
            field=models.SlugField(
                blank=True, db_index=False, default="", max_length=64
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="region",
            name="soato",
            field=models.CharField(
                blank=True,
                help_text="SOATO administrative code, e.g. 1703 for Andijan region",
                max_length=10,
                null=True,
            ),
        ),
        migrations.RunPython(backfill, unfill),
        migrations.AlterField(
            model_name="region",
            name="slug",
            field=models.SlugField(blank=True, max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name="region",
            name="soato",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="SOATO administrative code, e.g. 1703 for Andijan region",
                max_length=10,
                null=True,
                unique=True,
            ),
        ),
    ]
