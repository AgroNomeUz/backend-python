"""
Loads the demo marketplace from `equipment/demo_data/` — see the README there.

Goes through the ORM rather than SQL or the HTTP API: signing up needs an SMS
code, catalog rows have no public write endpoint, and the models do real work
on save (password hashing, public ids, slugs) that raw SQL would have to fake.

Idempotent. Every row is found by a natural key — phone for accounts, serial
number for machines, (machine, type) for listings — so editing a data file and
re-running updates rows in place instead of duplicating them.
"""

import json
import zlib
from datetime import timedelta
from pathlib import Path

from django.contrib.auth.hashers import make_password
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from equipment.models import (
    Asset,
    EquipmentCategory,
    EquipmentModel,
    EquipmentModelCompatibility,
    Manufacturer,
)
from inquiries.models import Inquiry
from listings.models import Listing, ListingImage
from users.models import Organization, Region, User
from users.services import username_for_phone

DATA_DIR = Path(__file__).resolve().parents[2] / "demo_data"
PASSWORD = "mockuser"

# Listings are backdated by up to this much, spread deterministically by serial
# number, so the "newest first" feed looks like a marketplace rather than one
# burst of 634 rows created in the same second.
BACKDATE_SPREAD = timedelta(days=60)


class Command(BaseCommand):
    help = "Seeds demo accounts, machines and listings (password: mockuser)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--region",
            action="append",
            dest="regions",
            metavar="CODE",
            help="Only this region's file, e.g. --region AN. Repeatable.",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "Delete the demo accounts (and everything they own) instead of "
                "seeding. The catalog is kept: real assets may point at it."
            ),
        )

    def handle(self, *args, regions=None, reset=False, **options):
        files = self._region_files(regions)
        region_data = [json.loads(path.read_text()) for path in files]

        if reset:
            phones = [a["phone"] for data in region_data for a in data["accounts"]]
            self._reset(phones)
            return

        by_code = Region.objects.in_bulk(field_name="code")
        missing = sorted({d["region"] for d in region_data} - set(by_code))
        if missing:
            raise CommandError(
                f"Unknown regions {', '.join(missing)} — run `manage.py seed_regions` first."
            )

        catalog = json.loads((DATA_DIR / "catalog.json").read_text())
        with transaction.atomic():
            models = self._seed_catalog(catalog)
        photos = {m["key"]: DATA_DIR / "photos" / m["photo"] for m in catalog["models"]}

        # PBKDF2 is deliberately slow; hashing once instead of 181 times saves
        # about a minute. Every demo account shares the password anyway.
        password_hash = make_password(PASSWORD)

        total_accounts = total_listings = 0
        for data in region_data:
            region = by_code[data["region"]]
            for account in data["accounts"]:
                with transaction.atomic():
                    self._seed_account(account, region, password_hash, models, photos)
                total_listings += len(account["listings"])
            total_accounts += len(data["accounts"])
            self.stdout.write(f"  {region.name}: {len(data['accounts'])} accounts")

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(models)} equipment models, {total_accounts} accounts "
                f"and {total_listings} listings. Password: {PASSWORD}"
            )
        )

    # ── inputs ────────────────────────────────────────────────────────────────

    def _region_files(self, codes: list[str] | None) -> list[Path]:
        available = {p.stem: p for p in sorted((DATA_DIR / "regions").glob("*.json"))}
        if not codes:
            return list(available.values())
        wanted = [code.lower() for code in codes]
        unknown = [code for code in wanted if code not in available]
        if unknown:
            raise CommandError(
                f"No demo data for {', '.join(unknown)}. "
                f"Available: {', '.join(available)}"
            )
        return [available[code] for code in wanted]

    # ── catalog ───────────────────────────────────────────────────────────────

    def _seed_catalog(self, catalog: dict) -> dict[str, EquipmentModel]:
        """Upsert categories, manufacturers and models; returns models by key."""
        categories: dict[str, EquipmentCategory] = {}
        # Listed parents-first in the file, so one pass resolves every parent.
        for entry in catalog["categories"]:
            categories[entry["slug"]], _ = EquipmentCategory.objects.update_or_create(
                slug=entry["slug"],
                defaults={
                    "name": entry["name"],
                    "parent": categories.get(entry.get("parent")),
                    "is_self_propelled": entry.get("is_self_propelled", False),
                },
            )

        manufacturers = {}
        for entry in catalog["manufacturers"]:
            manufacturers[entry["name"]], _ = Manufacturer.objects.update_or_create(
                name=entry["name"], defaults={"country": entry["country"]}
            )

        spec_fields = [
            "year_from", "engine_power_kw", "weight_kg", "length_mm", "width_mm",
            "height_mm", "working_width_mm", "fuel_type", "hitch_category",
        ]
        models = {}
        for entry in catalog["models"]:
            model, _ = EquipmentModel.objects.update_or_create(
                manufacturer=manufacturers[entry["manufacturer"]],
                name=entry["name"],
                defaults={
                    "category": categories[entry["category"]],
                    "is_self_propelled": entry.get("is_self_propelled", False),
                    "specifications": entry.get("specifications", {}),
                    **{field: entry[field] for field in spec_fields if field in entry},
                },
            )
            # What an implement needs from a tractor lives on the compatibility
            # table, not on the model — `hitch_category` is what a tractor offers.
            if entry.get("requires_hitch"):
                EquipmentModelCompatibility.objects.update_or_create(
                    primary_model=model,
                    compatible_with=None,
                    compatibility_type=EquipmentModelCompatibility.CompatibilityType.TRACTOR_IMPLEMENT,
                    defaults={"hitch_category": entry["requires_hitch"]},
                )
            models[entry["key"]] = model
        return models

    # ── accounts ──────────────────────────────────────────────────────────────

    def _seed_account(self, account, region, password_hash, models, photos) -> None:
        """One user, the organization they own, and its machines and listings."""
        phone = account["phone"]
        is_legal = account["entity_type"] == Organization.EntityType.LEGAL_ENTITY

        user = User.objects.filter(phone=phone).first()
        if user is None:
            user = User(phone=phone, username=username_for_phone(phone))
        user.full_name = account["name"]
        user.password = password_hash
        user.must_change_password = False
        user.is_active = True
        user.save()

        org = Organization.objects.filter(owner=user).first() or Organization(owner=user)
        org.name = account["name"]
        org.region = region
        org.address = account["district"]
        org.phone = phone
        org.entity_type = account["entity_type"]
        # So the verified badge shows up in the demo; individuals don't get it.
        org.is_verified = is_legal
        org.save()

        if user.organization_id != org.pk:
            user.organization = org
            user.save(update_fields=["organization"])

        for entry in account["listings"]:
            self._seed_listing(entry, org, user, region, account["district"], models, photos)

    def _seed_listing(self, entry, org, user, region, district, models, photos) -> None:
        asset, _ = Asset.objects.update_or_create(
            organization=org,
            serial_number=entry["serial_number"],
            defaults={
                "equipment_model": models[entry["model"]],
                "manufacture_year": entry["manufacture_year"],
                "current_meter_hours": entry["meter_hours"],
                "operational_status": Asset.OperationalStatus.AVAILABLE,
            },
        )

        fields = {
            "organization": org,
            "created_by": user,
            "status": Listing.Status.ACTIVE,
            "title": entry["title"],
            "description": entry["description"],
            "price": entry["price"],
            "currency": "UZS",
            "price_unit": entry["price_unit"],
            "has_operator": entry["has_operator"],
            "has_delivery": entry["has_delivery"],
            "region": region,
            "district": district,
        }
        listing = Listing.objects.filter(asset=asset, listing_type=entry["listing_type"]).first()
        if listing is None:
            listed_at = timezone.now() - _backdate(entry["serial_number"])
            listing = Listing.objects.create(
                asset=asset,
                listing_type=entry["listing_type"],
                published_at=listed_at,
                **fields,
            )
            # created_at is auto_now_add, so it can only be moved after the insert.
            Listing.objects.filter(pk=listing.pk).update(created_at=listed_at)
        else:
            for name, value in fields.items():
                setattr(listing, name, value)
            listing.published_at = listing.published_at or timezone.now()
            listing.save()

        self._ensure_photo(listing, photos[entry["model"]])

    def _ensure_photo(self, listing: Listing, source: Path) -> None:
        """
        Give the listing its card photo, or restore the file if it went missing.

        The second case is why this checks the storage rather than just the row:
        MEDIA_ROOT is not a volume in docker-compose, so recreating the container
        keeps the ListingImage rows and loses the files behind them.
        """
        image = listing.images.filter(is_primary=True).first()
        if image is not None and image.file.storage.exists(image.file.name):
            return
        if image is None:
            image = ListingImage(listing=listing, is_primary=True, sort_order=0)
        with source.open("rb") as fh:
            # One copy per listing, under listings/<public_id>/ like any upload,
            # so deleting one demo photo through the API can't break another.
            image.file.save(source.name, File(fh), save=True)

    # ── reset ─────────────────────────────────────────────────────────────────

    def _reset(self, phones: list[str]) -> None:
        owners = User.objects.filter(phone__in=phones)
        orgs = Organization.objects.filter(owner__in=owners)
        listings = Listing.objects.filter(organization__in=orgs)
        file_names = list(
            ListingImage.objects.filter(listing__in=listings).values_list("file", flat=True)
        )

        with transaction.atomic():
            # Inquiries protect the listings they were made on — including ones
            # real users made on demo listings during a demo.
            demo_inquiries = Inquiry.objects.filter(listing__in=listings)
            inquiries = demo_inquiries.count()
            demo_inquiries.delete()
            # Listings protect their assets, so they go before the organizations
            # (whose deletion cascades to the assets).
            listing_count = listings.count()
            listings.delete()
            # Staff an owner added are members, not owners; take them too.
            members = User.objects.filter(organization__in=orgs).exclude(pk__in=owners)
            members.delete()
            account_count = owners.count()
            owners.delete()

            def remove_files():
                storage = ListingImage._meta.get_field("file").storage
                for name in file_names:
                    storage.delete(name)

            transaction.on_commit(remove_files)

        self.stdout.write(
            self.style.SUCCESS(
                f"Removed {account_count} demo accounts, {listing_count} listings "
                f"and {inquiries} inquiries."
            )
        )


def _backdate(serial_number: str) -> timedelta:
    """A stable offset in [0, BACKDATE_SPREAD) derived from the serial number."""
    minutes = int(BACKDATE_SPREAD.total_seconds() // 60)
    return timedelta(minutes=zlib.crc32(serial_number.encode()) % minutes)
