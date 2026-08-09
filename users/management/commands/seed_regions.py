from django.core.management.base import BaseCommand

from users.models import Region

# Uzbekistan's 14 administrative units: 12 regions, the Republic of
# Karakalpakstan, and Tashkent city. Codes are the ISO 3166-2:UZ subdivision
# codes without the country prefix.
REGIONS = [
    ("AN", "Andijan"),
    ("BU", "Bukhara"),
    ("FA", "Fergana"),
    ("JI", "Jizzakh"),
    ("QR", "Karakalpakstan"),
    ("NG", "Namangan"),
    ("NW", "Navoiy"),
    ("QA", "Qashqadaryo"),
    ("SA", "Samarqand"),
    ("SI", "Sirdaryo"),
    ("SU", "Surxondaryo"),
    ("TK", "Tashkent Region"),
    ("TO", "Tashkent City"),
    ("XO", "Xorazm"),
]


class Command(BaseCommand):
    help = "Seeds the 14 Uzbekistan regions used by Organization/Farm/Asset filters."

    def handle(self, *args, **options):
        created = 0
        for code, name in REGIONS:
            _, was_created = Region.objects.update_or_create(code=code, defaults={"name": name})
            created += was_created

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(REGIONS)} regions ({created} created, "
                f"{len(REGIONS) - created} already existed)."
            )
        )
