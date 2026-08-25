from django.core.management.base import BaseCommand

from users.models import Region

# Uzbekistan's 14 administrative units: 12 regions, the Republic of
# Karakalpakstan, and Tashkent city.
#
#   code  — ISO 3166-2:UZ subdivision code without the country prefix.
#   soato — the national classifier of administrative-territorial objects.
#           Four digits at region level; districts and cities extend the same
#           code to seven, so this is the key any finer-grained data hangs off.
#
# Slugs are not listed: Region.save() derives them from the name.
REGIONS = [
    ("AN", "Andijan", "1703"),
    ("BU", "Bukhara", "1706"),
    ("FA", "Fergana", "1730"),
    ("JI", "Jizzakh", "1708"),
    ("QR", "Karakalpakstan", "1735"),
    ("NG", "Namangan", "1714"),
    ("NW", "Navoiy", "1712"),
    ("QA", "Qashqadaryo", "1710"),
    ("SA", "Samarqand", "1718"),
    ("SI", "Sirdaryo", "1724"),
    ("SU", "Surxondaryo", "1722"),
    ("TK", "Tashkent Region", "1727"),
    ("TO", "Tashkent City", "1726"),
    ("XO", "Xorazm", "1733"),
]


class Command(BaseCommand):
    help = "Seeds the 14 Uzbekistan regions used by Organization/Farm/Asset filters."

    def handle(self, *args, **options):
        created = 0
        for code, name, soato in REGIONS:
            # update_or_create() on the model (not a queryset .update()) so
            # Region.save() runs and fills the slug for rows created here.
            _, was_created = Region.objects.update_or_create(
                code=code, defaults={"name": name, "soato": soato}
            )
            created += was_created

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(REGIONS)} regions ({created} created, "
                f"{len(REGIONS) - created} already existed)."
            )
        )
