"""
A starting catalogue, safe to re-run. Rows are matched by slug and only created,
so anything an admin has since edited is left alone.
"""

from django.core.management.base import BaseCommand

from marketplace.models import Product, ProductCategory, Unit

# slug, name, [(slug, name, EPPO code, regulated, unit)]
CATALOGUE = [
    (
        "vegetables",
        "Vegetables",
        [
            ("tomato", "Tomato", "LYPES", False, Unit.KILOGRAM),
            ("cucumber", "Cucumber", "CUMSA", False, Unit.KILOGRAM),
            ("potato", "Potato", "SOLTU", False, Unit.KILOGRAM),
            ("onion", "Onion", "ALLCE", False, Unit.KILOGRAM),
            ("carrot", "Carrot", "DAUCS", False, Unit.KILOGRAM),
            ("cabbage", "Cabbage", "BRSOL", False, Unit.KILOGRAM),
            ("sweet-pepper", "Sweet pepper", "CPSAN", False, Unit.KILOGRAM),
            ("lettuce", "Lettuce", "LACSA", False, Unit.KILOGRAM),
        ],
    ),
    (
        "herbs",
        "Herbs",
        [
            ("dill", "Dill", "ANEGR", False, Unit.BUNCH),
            ("parsley", "Parsley", "PARCR", False, Unit.BUNCH),
            ("coriander", "Coriander", "CORSA", False, Unit.BUNCH),
        ],
    ),
    (
        "fruit",
        "Fruit",
        [
            ("apple", "Apple", "MABSD", False, Unit.KILOGRAM),
            ("pear", "Pear", "PYUCO", False, Unit.KILOGRAM),
            ("peach", "Peach", "PRNPS", False, Unit.KILOGRAM),
            ("apricot", "Apricot", "PRNAR", False, Unit.KILOGRAM),
            ("grape", "Grape", "VITVI", False, Unit.KILOGRAM),
            ("melon", "Melon", "CUMME", False, Unit.KILOGRAM),
            ("watermelon", "Watermelon", "CITLA", False, Unit.KILOGRAM),
        ],
    ),
    (
        "grains-and-legumes",
        "Grains and legumes",
        [
            ("wheat", "Wheat", "TRZAX", False, Unit.TONNE),
            ("rice", "Rice", "ORYSA", False, Unit.KILOGRAM),
            ("mung-bean", "Mung bean", "PHSAU", False, Unit.KILOGRAM),
        ],
    ),
    (
        "meat",
        "Meat",
        [
            ("beef", "Beef", "", True, Unit.KILOGRAM),
            ("lamb", "Lamb", "", True, Unit.KILOGRAM),
            ("chicken", "Chicken", "", True, Unit.KILOGRAM),
        ],
    ),
    (
        "dairy-and-eggs",
        "Dairy and eggs",
        [
            ("milk", "Milk", "", True, Unit.LITRE),
            ("eggs", "Eggs", "", True, Unit.PIECE),
        ],
    ),
]


class Command(BaseCommand):
    help = "Create the starter produce catalogue. Existing rows are left untouched."

    def handle(self, *args, **options):
        created = 0
        for order, (slug, name, products) in enumerate(CATALOGUE):
            category, made = ProductCategory.objects.get_or_create(
                slug=slug, defaults={"name": name, "sort_order": order}
            )
            created += made
            for product_slug, product_name, eppo, regulated, unit in products:
                _, made = Product.objects.get_or_create(
                    slug=product_slug,
                    defaults={
                        "category": category,
                        "name": product_name,
                        "eppo_code": eppo,
                        "is_regulated": regulated,
                        "default_unit": unit,
                    },
                )
                created += made
        self.stdout.write(f"{created} catalogue rows created.")
