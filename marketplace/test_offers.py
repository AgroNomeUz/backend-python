"""
marketplace/test_offers.py
The catalogue, the public feed and a seller's own offers.
"""

import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import override_settings

from core.models import ActivityLog
from farms.models import CropZone, Farm, Field, Season
from marketplace.models import (
    Offer,
    OfferImage,
    Product,
    ProductCategory,
    active_offer_q,
    market_today,
)
from users.models import OrgPermission, Organization

from .test_support import MarketTestCase, auth

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class ClockTests(MarketTestCase):
    def test_today_is_tashkents_today_not_the_servers(self):
        late_evening_utc = datetime(2026, 3, 10, 20, 0, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=late_evening_utc):
            self.assertEqual(str(market_today()), "2026-03-11")


class CatalogueTests(MarketTestCase):
    def test_the_catalogue_is_public(self):
        response = self.get("market/products?search=tom", user=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["slug"], "tomato")

    def test_a_parent_category_finds_its_subcategories(self):
        child = ProductCategory.objects.create(
            name="Greenhouse", slug="greenhouse", parent=self.vegetables
        )
        Product.objects.create(category=child, name="Cherry tomato", slug="cherry-tomato")
        slugs = [
            row["slug"]
            for row in self.get("market/products?category=vegetables", user=False).json()["items"]
        ]
        self.assertIn("cherry-tomato", slugs)

    def test_categories_are_a_tree(self):
        ProductCategory.objects.create(name="Roots", slug="roots", parent=self.vegetables)
        tree = self.get("market/categories", user=False).json()
        top = next(row for row in tree if row["slug"] == "vegetables")
        self.assertEqual([row["slug"] for row in top["children"]], ["roots"])

    def test_an_inactive_product_is_hidden(self):
        Product.objects.create(
            category=self.vegetables, name="Retired", slug="retired", is_active=False
        )
        rows = self.get("market/products?search=Retired", user=False).json()
        self.assertEqual(rows["count"], 0)

    def test_the_seed_can_be_run_twice(self):
        call_command("seed_products", stdout=StringIO())
        count = Product.objects.count()
        call_command("seed_products", stdout=StringIO())
        self.assertEqual(Product.objects.count(), count)
        self.assertTrue(Product.objects.get(slug="beef").is_regulated)
        self.assertFalse(Product.objects.get(slug="lettuce").is_regulated)


class FeedTests(MarketTestCase):
    def feed(self, query=""):
        return self.get(f"market/offers{query}", user=False).json()

    def test_only_what_can_be_bought_is_public(self):
        shown = self.make_offer(title="Shown")
        self.make_offer(title="Draft", status=Offer.Status.DRAFT)
        self.make_offer(title="Paused", status=Offer.Status.PAUSED)
        self.make_offer(title="Archived", status=Offer.Status.ARCHIVED)
        self.make_offer(title="Sold out", quantity_available=0)
        self.make_offer(
            title="Over",
            available_from=self.today - timedelta(days=9),
            available_until=self.today - timedelta(days=1),
        )
        rows = self.feed()
        self.assertEqual([row["id"] for row in rows["items"]], [str(shown.public_id)])

    def test_regulated_goods_leave_the_market_with_the_sellers_verification(self):
        beef = Product.objects.create(
            category=self.vegetables, name="Beef", slug="beef", is_regulated=True
        )
        offer = self.make_offer(title="Beef", product=beef)
        self.assertEqual(self.feed()["count"], 1)
        profile = f"organizations/{self.seller.public_id}"
        self.assertEqual(self.get(profile, user=False).status_code, 200)

        Organization.objects.filter(pk=self.seller.pk).update(is_verified=False)

        self.assertEqual(self.feed()["count"], 0)
        detail = f"market/offers/{offer.public_id}"
        self.assertEqual(self.get(detail, user=False).status_code, 404)
        self.assertEqual(self.get(detail, self.seller_owner).status_code, 200)
        self.assertEqual(self.get(profile, user=False).status_code, 404)
        self.assertEqual(self.post("market/orders", self.order_body(offer)).status_code, 404)

        self.make_offer(title="Tomatoes")
        self.assertEqual(self.feed()["count"], 1)
        self.assertEqual(self.get(profile, user=False).status_code, 200)

    def test_the_regulated_rule_follows_the_prefix(self):
        beef = Product.objects.create(
            category=self.vegetables, name="Beef", slug="beef", is_regulated=True
        )
        image = OfferImage.objects.create(offer=self.make_offer(product=beef), file="x.png")
        Organization.objects.filter(pk=self.seller.pk).update(is_verified=False)
        self.assertFalse(OfferImage.objects.filter(active_offer_q("offer__")).exists())
        Organization.objects.filter(pk=self.seller.pk).update(is_verified=True)
        self.assertEqual(OfferImage.objects.get(active_offer_q("offer__")), image)

    def test_an_offer_on_its_last_day_is_still_shown(self):
        self.make_offer(available_until=self.today)
        self.assertEqual(self.feed()["count"], 1)

    def test_a_page_cannot_ask_for_a_whole_table(self):
        self.assertEqual(self.get("market/offers?limit=100", user=False).status_code, 200)
        self.assertEqual(self.get("market/offers?limit=101", user=False).status_code, 422)

    def test_a_retired_product_takes_its_offers_with_it(self):
        retired = Product.objects.create(
            category=self.vegetables, name="Old", slug="old", is_active=False
        )
        self.make_offer(product=retired)
        self.assertEqual(self.feed()["count"], 0)

    def test_filters(self):
        self.make_offer(title="A", price=Decimal("5000"), grade="a", variety="Pink")
        self.make_offer(title="B", price=Decimal("9000"), grade="b", offers_delivery=False)

        def titles(query):
            return sorted(row["title"] for row in self.feed(query)["items"])

        self.assertEqual(titles("?max_price=6000"), ["A"])
        self.assertEqual(titles("?grade=b"), ["B"])
        self.assertEqual(titles("?search=pink"), ["A"])
        self.assertEqual(titles("?has_delivery=true"), ["A"])
        self.assertEqual(titles("?product=tomato"), ["A", "B"])
        self.assertEqual(titles("?category=vegetables"), ["A", "B"])
        self.assertEqual(titles(f"?region={self.region.slug}"), ["A", "B"])
        self.assertEqual(titles(f"?owner={self.outsider.public_id}"), [])

    def test_available_on_narrows_to_that_date(self):
        self.make_offer(title="Now", available_until=self.today + timedelta(days=2))
        self.make_offer(
            title="Later",
            offer_type=Offer.OfferType.FORWARD,
            available_from=self.today + timedelta(days=30),
            available_until=self.today + timedelta(days=60),
        )
        day = self.today + timedelta(days=45)
        rows = self.feed(f"?available_on={day}")["items"]
        self.assertEqual([row["title"] for row in rows], ["Later"])

    def test_sorting_by_price(self):
        self.make_offer(title="Dear", price=Decimal("9000"))
        self.make_offer(title="Cheap", price=Decimal("5000"))
        self.assertEqual(self.feed("?sort=price_asc")["items"][0]["title"], "Cheap")
        self.assertEqual(self.get("market/offers?sort=nope", user=False).status_code, 400)

    def test_a_stranger_reads_an_active_offer_but_not_a_draft(self):
        active = self.make_offer()
        draft = self.make_offer(status=Offer.Status.DRAFT)
        self.assertEqual(self.get(f"market/offers/{active.public_id}", user=False).status_code, 200)
        self.assertEqual(self.get(f"market/offers/{draft.public_id}", user=False).status_code, 404)

    def test_a_temporary_password_still_sees_public_pages(self):
        offer = self.make_offer()
        member = self.member(must_change=True)
        self.assertEqual(self.get(f"market/offers/{offer.public_id}", member).status_code, 200)

    def test_the_owner_reads_their_draft(self):
        draft = self.make_offer(status=Offer.Status.DRAFT)
        path = f"market/offers/{draft.public_id}"
        self.assertEqual(self.get(path, self.seller_owner).status_code, 200)
        self.assertEqual(self.get(path, self.buyer_owner).status_code, 404)
        blind = self.member(org=self.seller)
        self.assertEqual(self.get(path, blind).status_code, 404)


class OfferWriteTests(MarketTestCase):
    def body(self, **overrides) -> dict:
        body = {
            "product_id": str(self.tomato.public_id),
            "title": "Tomatoes",
            "price": "8000.00",
            "quantity_available": "100.000",
            "available_from": str(self.today),
            "available_until": str(self.today + timedelta(days=5)),
            "status": "active",
        }
        body.update(overrides)
        return body

    def create(self, user=None, **overrides):
        return self.post("market/offers", self.body(**overrides), user or self.seller_owner)

    def test_creating_an_offer(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.content)
        offer = Offer.objects.get(public_id=response.json()["id"])
        self.assertEqual(offer.organization, self.seller)
        self.assertEqual(offer.created_by, self.seller_owner)
        self.assertEqual(offer.region, self.region)
        self.assertEqual(offer.unit, "kg")
        self.assertIsNotNone(offer.published_at)

    def test_the_price_is_audited_exactly(self):
        response = self.create(price="1234567890123.45")
        offer = Offer.objects.get(public_id=response.json()["id"])
        row = ActivityLog.objects.get(object_id=offer.pk, action="created")
        self.assertEqual(row.organization, self.seller)
        self.assertEqual(row.changes["price"]["to"], "1234567890123.45")

    def test_writing_needs_the_permission(self):
        viewer = self.member(OrgPermission.VIEW_MARKET, org=self.seller)
        manager = self.member(OrgPermission.MANAGE_MARKET, org=self.seller)
        self.assertEqual(self.create(viewer).status_code, 403)
        self.assertEqual(self.create(manager).status_code, 201)

    def test_bad_input_is_refused(self):
        self.assertEqual(self.create(price="0").status_code, 422)
        self.assertEqual(self.create(price="1.001").status_code, 422)
        self.assertEqual(self.create(quantity_available="-1").status_code, 422)
        self.assertEqual(self.create(title="   ").status_code, 422)
        self.assertEqual(
            self.create(product_id="00000000-0000-0000-0000-000000000000").status_code, 400
        )

    def test_dates_must_make_sense(self):
        backwards = self.create(available_until=str(self.today - timedelta(days=1)))
        self.assertEqual(backwards.status_code, 400)

    def test_offering_neither_pickup_nor_delivery_is_refused(self):
        response = self.create(offers_pickup=False, offers_delivery=False)
        self.assertEqual(response.status_code, 400)

    def test_a_past_offer_cannot_go_live(self):
        past = self.today - timedelta(days=2)
        response = self.create(available_from=str(past - timedelta(days=1)), available_until=str(past))
        self.assertEqual(response.status_code, 400)

    def test_a_new_offer_cannot_be_born_archived(self):
        self.assertEqual(self.create(status="archived").status_code, 400)

    def test_regulated_goods_need_a_verified_seller(self):
        beef = Product.objects.create(
            category=self.vegetables, name="Beef", slug="beef", is_regulated=True
        )
        response = self.post(
            "market/offers",
            self.body(product_id=str(beef.public_id)),
            self.buyer_owner,
        )
        self.assertEqual(response.status_code, 403)
        draft = self.post(
            "market/offers",
            self.body(product_id=str(beef.public_id), status="draft"),
            self.buyer_owner,
        )
        self.assertEqual(draft.status_code, 201)
        publish = self.patch(
            f"market/offers/{draft.json()['id']}", {"status": "active"}, self.buyer_owner
        )
        self.assertEqual(publish.status_code, 403)
        verified = self.create(product_id=str(beef.public_id))
        self.assertEqual(verified.status_code, 201)

    def _crop_zone(self, org):
        farm = Farm.objects.create(organization=org, name=f"Farm {org.pk}")
        field = Field.objects.create(farm=farm, organization=org, name="North")
        season = Season.objects.create(
            organization=org,
            name="2026",
            start_date=self.today,
            end_date=self.today + timedelta(days=100),
        )
        return CropZone.objects.create(
            field=field,
            season=season,
            organization=org,
            crop_code="LYPES",
            crop_name="Tomato",
            expected_harvest_date=self.today + timedelta(days=60),
        )

    def test_a_forward_offer_can_point_at_a_crop(self):
        zone = self._crop_zone(self.seller)
        response = self.create(offer_type="forward", crop_zone_id=str(zone.public_id))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["crop_zone_id"], str(zone.public_id))

    def test_another_organizations_crop_is_a_404(self):
        zone = self._crop_zone(self.outsider)
        response = self.create(crop_zone_id=str(zone.public_id))
        self.assertEqual(response.status_code, 404)

    def test_the_database_refuses_another_organizations_crop(self):
        zone = self._crop_zone(self.outsider)
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                self.make_offer(crop_zone=zone)
        self.assertIn("offer_crop_zone_same_org_fk", str(caught.exception))

    def test_deleting_a_crop_leaves_the_offer(self):
        zone = self._crop_zone(self.seller)
        offer = self.make_offer(crop_zone=zone)
        zone.delete()
        offer.refresh_from_db()
        self.assertIsNone(offer.crop_zone)

    def test_a_partial_update(self):
        offer = self.make_offer()
        response = self.patch(
            f"market/offers/{offer.public_id}", {"price": "7500.00"}, self.seller_owner
        )
        self.assertEqual(response.status_code, 200)
        offer.refresh_from_db()
        self.assertEqual(offer.price, Decimal("7500.00"))
        self.assertEqual(offer.title, "Greenhouse tomatoes")
        row = ActivityLog.objects.get(object_id=offer.pk, action="updated")
        self.assertEqual(
            row.changes["price"], {"from": "8000.00", "to": "7500.00"}
        )

    def test_a_null_is_refused_where_it_means_nothing(self):
        offer = self.make_offer()
        response = self.patch(
            f"market/offers/{offer.public_id}", {"price": None}, self.seller_owner
        )
        self.assertEqual(response.status_code, 400)

    def test_a_region_can_be_cleared(self):
        offer = self.make_offer()
        response = self.patch(
            f"market/offers/{offer.public_id}", {"region_id": None}, self.seller_owner
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["region"])

    def test_pausing_takes_an_offer_off_the_market(self):
        offer = self.make_offer()
        self.patch(f"market/offers/{offer.public_id}", {"status": "paused"}, self.seller_owner)
        self.assertEqual(self.get("market/offers", user=False).json()["count"], 0)
        row = ActivityLog.objects.get(object_id=offer.pk, action="status_changed")
        self.assertEqual(row.changes["status"]["to"], "paused")

    def test_archiving_is_a_soft_delete(self):
        offer = self.make_offer()
        response = self.call("delete", f"market/offers/{offer.public_id}", user=self.seller_owner)
        self.assertEqual(response.status_code, 204)
        offer.refresh_from_db()
        self.assertEqual(offer.status, Offer.Status.ARCHIVED)

    def test_another_organizations_offer_is_a_404(self):
        offer = self.make_offer()
        path = f"market/offers/{offer.public_id}"
        self.assertEqual(self.patch(path, {"price": "1.00"}, self.buyer_owner).status_code, 404)
        self.assertEqual(self.call("delete", path, user=self.buyer_owner).status_code, 404)

    def test_my_offers_lists_every_status(self):
        self.make_offer(status=Offer.Status.DRAFT)
        self.make_offer()
        rows = self.get("market/offers/my", self.seller_owner).json()
        self.assertEqual(rows["count"], 2)
        rows = self.get("market/offers/my?status=draft", self.seller_owner).json()
        self.assertEqual(rows["count"], 1)
        self.assertEqual(self.get("market/offers/my").json()["count"], 0)


class ImageTests(MarketTestCase):
    def upload(self, offer, name="photo.png", body=PNG, user=None):
        return self.client.post(
            f"/api/v1/market/offers/{offer.public_id}/images",
            {"file": SimpleUploadedFile(name, body, content_type="image/png")},
            **auth(user or self.seller_owner),
        )

    def test_images(self):
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            offer = self.make_offer()
            first = self.upload(offer)
            self.assertEqual(first.status_code, 201, first.content)
            self.assertTrue(first.json()["is_primary"])
            self.assertTrue(first.json()["url"].startswith("http"))
            self.assertIn("/media/offers/", first.json()["url"])

            second = self.upload(offer, "second.png")
            self.assertFalse(second.json()["is_primary"])

            removed = self.call(
                "delete",
                f"market/offers/{offer.public_id}/images/{first.json()['id']}",
                user=self.seller_owner,
            )
            self.assertEqual(removed.status_code, 204)
            self.assertTrue(offer.images.get().is_primary)

    def test_a_file_that_is_not_an_image_is_refused(self):
        offer = self.make_offer()
        self.assertEqual(self.upload(offer, body=b"not an image").status_code, 400)
        self.assertEqual(self.upload(offer, name="run.exe").status_code, 400)

    def test_another_organization_cannot_add_images(self):
        self.assertEqual(self.upload(self.make_offer(), user=self.buyer_owner).status_code, 404)
