"""
favorites/tests.py
One member's shortlist, their colleague's, and the line between the two.

A favorite is scoped to `(organization, user, listing)`, and almost everything
that can go wrong is a scoping mistake: the default view showing a colleague's
picks, `?scope=organization` failing to, or one org's list leaking into
another's. So the fixture is two organizations, each with two members, and the
tests check what each of the four accounts sees as often as what one of them
saved.

The uniqueness invariant is written through the ORM rather than the API, for
the reason listings/tests.py does: a view that forgot its `get_or_create`
could not make it pass.
"""

import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from api.auth import create_access_token
from core.models import ActivityLog
from equipment.models import Asset, EquipmentCategory, EquipmentModel, Manufacturer
from favorites.models import Favorite
from listings.models import Listing, ListingImage
from users.models import Organization, Region, User

# The smallest valid PNG, as listings/tests.py uses: enough to get past the
# signature check and onto disk, and nothing more.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def auth(user: User) -> dict:
    """Request kwargs carrying a bearer token for `user`."""
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}


class FavoriteTestCase(TestCase):
    """
    A seller with two machines on the market, and a buyer organization whose
    two members each save things — because "is this list mine or ours?" is
    only a real question with more than one member in the room.
    """

    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(
            name="Andijan Favorite", code="ANF", soato="1704"
        )

        cls.seller_owner = User.objects.create_user(
            username="fav-seller-owner", password="x", full_name="Seller Owner"
        )
        cls.seller_org = Organization.objects.create(
            name="Seller Org",
            owner=cls.seller_owner,
            region=cls.region,
            is_verified=True,
        )
        cls.seller_owner.organization = cls.seller_org
        cls.seller_owner.save(update_fields=["organization"])

        cls.buyer_owner = User.objects.create_user(
            username="fav-buyer-owner", password="x", full_name="Buyer Owner"
        )
        cls.buyer_org = Organization.objects.create(
            name="Buyer Org", owner=cls.buyer_owner, region=cls.region
        )
        cls.buyer_owner.organization = cls.buyer_org
        cls.buyer_owner.save(update_fields=["organization"])
        # No permission codes at all — saving a listing is the one write that
        # is not gated by one (§0.2), and this account is what proves it.
        cls.buyer_member = User.objects.create_user(
            username="fav-buyer-member",
            password="x",
            organization=cls.buyer_org,
            full_name="Buyer Member",
        )

        # A third organization, so "another org's list" is a real place rather
        # than the absence of one.
        cls.other_owner = User.objects.create_user(
            username="fav-other-owner", password="x", full_name="Other Owner"
        )
        cls.other_org = Organization.objects.create(
            name="Other Org", owner=cls.other_owner, region=cls.region
        )
        cls.other_owner.organization = cls.other_org
        cls.other_owner.save(update_fields=["organization"])

        manufacturer = Manufacturer.objects.create(name="MTZ")
        category = EquipmentCategory.objects.create(name="Tractor", slug="tractor")
        equipment_model = EquipmentModel.objects.create(
            manufacturer=manufacturer, category=category, name="82.1"
        )

        cls.asset = Asset.objects.create(
            organization=cls.seller_org, equipment_model=equipment_model
        )
        cls.other_asset = Asset.objects.create(
            organization=cls.seller_org, equipment_model=equipment_model
        )

        cls.listing = cls.make_listing(title="MTZ-82 tractor with operator")
        cls.second_listing = cls.make_listing(
            asset=cls.other_asset, title="MTZ-82 tractor, bare"
        )

    @classmethod
    def make_listing(cls, **overrides):
        fields = {
            "organization": cls.seller_org,
            "asset": cls.asset,
            "created_by": cls.seller_owner,
            "region": cls.region,
            "listing_type": Listing.ListingType.RENT,
            "status": Listing.Status.ACTIVE,
            "title": "MTZ-82 tractor with operator",
            "price": "400000.00",
            "price_unit": Listing.PriceUnit.DAY,
        }
        fields.update(overrides)
        return Listing.objects.create(**fields)

    def save_favorite(self, listing=None, user=None):
        target = listing or self.listing
        return self.client.put(
            f"/api/v1/favorites/{target.public_id}", **auth(user or self.buyer_owner)
        )

    def remove_favorite(self, listing=None, user=None):
        target = listing or self.listing
        return self.client.delete(
            f"/api/v1/favorites/{target.public_id}", **auth(user or self.buyer_owner)
        )

    def list_favorites(self, user=None, **params):
        return self.client.get(
            "/api/v1/favorites", params, **auth(user or self.buyer_owner)
        )


class InvariantTests(FavoriteTestCase):
    """The one rule the database keeps, checked where it is kept."""

    def test_a_member_cannot_save_the_same_listing_twice(self):
        Favorite.objects.create(
            organization=self.buyer_org, user=self.buyer_owner, listing=self.listing
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Favorite.objects.create(
                organization=self.buyer_org, user=self.buyer_owner, listing=self.listing
            )

    def test_two_members_may_each_save_the_same_listing(self):
        """
        The key is `(organization, user, listing)`, not `(organization,
        listing)` — a shared shortlist is the union of personal ones, not a
        first-come-first-served claim on a machine.
        """
        Favorite.objects.create(
            organization=self.buyer_org, user=self.buyer_owner, listing=self.listing
        )
        Favorite.objects.create(
            organization=self.buyer_org, user=self.buyer_member, listing=self.listing
        )
        self.assertEqual(Favorite.objects.filter(listing=self.listing).count(), 2)

    def test_unsaving_a_listing_does_not_touch_the_listing(self):
        """CASCADE points one way: a bookmark never protects what it points at."""
        favorite = Favorite.objects.create(
            organization=self.buyer_org, user=self.buyer_owner, listing=self.listing
        )
        favorite.delete()
        self.assertTrue(Listing.objects.filter(pk=self.listing.pk).exists())


class SaveTests(FavoriteTestCase):
    """`PUT /favorites/{listing_id}` — idempotent, and open to any member."""

    def test_saving_returns_the_listing_and_who_saved_it(self):
        response = self.save_favorite()
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()

        self.assertEqual(payload["listing"]["id"], str(self.listing.public_id))
        self.assertEqual(payload["listing"]["title"], self.listing.title)
        # The full feed card, so a saved list renders with one component.
        self.assertEqual(payload["listing"]["owner"]["name"], "Seller Org")
        self.assertEqual(payload["listing"]["status"], Listing.Status.ACTIVE)
        self.assertEqual(payload["saved_by"]["name"], "Buyer Owner")
        self.assertIsNotNone(payload["saved_at"])
        # The favorite's own id, not the listing's — they are not
        # interchangeable and the payload must not suggest they are.
        self.assertNotEqual(payload["id"], str(self.listing.public_id))

    def test_saving_needs_no_permission_code(self):
        """
        §0.2 gates writes behind codes; this is the exception, and it is
        deliberate. The row is the member's own and commits the organization
        to nothing.
        """
        self.assertEqual(self.save_favorite(user=self.buyer_member).status_code, 200)

    def test_saving_twice_is_one_row_and_one_audit_entry(self):
        first = self.save_favorite()
        second = self.save_favorite()

        self.assertEqual(second.status_code, 200)
        # Same row, same id: the client need not know it had already saved it.
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(Favorite.objects.filter(user=self.buyer_owner).count(), 1)
        # A no-op is not a write, and an org's history should not fill up with
        # the noise of one member's double-click.
        self.assertEqual(ActivityLog.objects.filter(action="created").count(), 1)

    def test_saving_is_attributed_to_the_members_own_organization(self):
        self.save_favorite()
        entry = ActivityLog.objects.get(content_type__model="favorite")
        self.assertEqual(entry.organization, self.buyer_org)
        self.assertEqual(entry.actor, self.buyer_owner)
        self.assertEqual(entry.action, ActivityLog.Action.CREATED)

    def test_an_unpublished_listing_cannot_be_saved(self):
        """
        Not on the market, so not saveable — and answered exactly as a
        stranger's id is, so the two are indistinguishable (§0.1).
        """
        for status in (
            Listing.Status.DRAFT,
            Listing.Status.PAUSED,
            Listing.Status.ARCHIVED,
        ):
            with self.subTest(status=status):
                self.listing.status = status
                self.listing.save(update_fields=["status"])
                self.assertEqual(self.save_favorite().status_code, 404)
        self.listing.status = Listing.Status.ACTIVE
        self.listing.save(update_fields=["status"])

    def test_a_machine_in_for_repair_cannot_be_saved(self):
        """The feed is an intersection (§0.5), and so is this."""
        self.asset.operational_status = Asset.OperationalStatus.UNDER_MAINTENANCE
        self.asset.save(update_fields=["operational_status"])
        self.assertEqual(self.save_favorite().status_code, 404)

    def test_an_unknown_listing_is_a_404(self):
        response = self.client.put(
            "/api/v1/favorites/00000000-0000-4000-8000-000000000000",
            **auth(self.buyer_owner),
        )
        self.assertEqual(response.status_code, 404)

    def test_saving_your_own_organizations_listing_is_allowed(self):
        """
        Unlike an inquiry, which would be a 400: bookmarking your own offer
        commits nobody to anything, and an owner watching their own listing is
        a normal thing to do.
        """
        self.assertEqual(self.save_favorite(user=self.seller_owner).status_code, 200)

    def test_saving_requires_authentication(self):
        response = self.client.put(f"/api/v1/favorites/{self.listing.public_id}")
        self.assertEqual(response.status_code, 401)


class ListTests(FavoriteTestCase):
    """
    `GET /favorites` — the member's own list by default, the team's under
    `?scope=organization`.
    """

    def setUp(self):
        self.mine = Favorite.objects.create(
            organization=self.buyer_org, user=self.buyer_owner, listing=self.listing
        )
        self.colleagues = Favorite.objects.create(
            organization=self.buyer_org,
            user=self.buyer_member,
            listing=self.second_listing,
        )
        # Another organization's, which must never appear in either view.
        Favorite.objects.create(
            organization=self.other_org, user=self.other_owner, listing=self.listing
        )

    def test_the_default_list_is_the_callers_own(self):
        payload = self.list_favorites().json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["id"], str(self.mine.public_id))
        self.assertEqual(payload["items"][0]["saved_by"]["name"], "Buyer Owner")

    def test_the_org_scope_is_the_union_of_both_members_lists(self):
        payload = self.list_favorites(scope="organization").json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            {item["saved_by"]["name"] for item in payload["items"]},
            {"Buyer Owner", "Buyer Member"},
        )

    def test_neither_view_shows_another_organizations_list(self):
        for scope in (None, "organization"):
            with self.subTest(scope=scope):
                params = {"scope": scope} if scope else {}
                payload = self.list_favorites(user=self.other_owner, **params).json()
                self.assertEqual(payload["count"], 1)
                self.assertEqual(
                    payload["items"][0]["saved_by"]["name"], "Other Owner"
                )

    def test_an_unrecognised_scope_falls_back_to_the_personal_list(self):
        """
        The parameter widens a view; a typo should not turn a saved list into
        an error page.
        """
        payload = self.list_favorites(scope="nonsense").json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["id"], str(self.mine.public_id))

    def test_the_list_is_newest_first(self):
        second = Favorite.objects.create(
            organization=self.buyer_org,
            user=self.buyer_owner,
            listing=self.second_listing,
        )
        payload = self.list_favorites().json()
        self.assertEqual(payload["items"][0]["id"], str(second.public_id))

    def test_a_withdrawn_listing_stays_on_the_list_with_its_status(self):
        """
        A favorite records what a member chose to keep. Dropping the rows that
        have since come off the market would silently shrink the list they
        curated; `status` is how the client greys one out instead.
        """
        self.listing.status = Listing.Status.ARCHIVED
        self.listing.save(update_fields=["status"])

        payload = self.list_favorites().json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["items"][0]["listing"]["status"], Listing.Status.ARCHIVED
        )

    def test_the_list_is_readable_with_no_permission_codes(self):
        self.assertEqual(self.list_favorites(user=self.buyer_member).status_code, 200)

    def test_a_saved_listings_photos_come_back_as_absolute_urls(self):
        """
        `ListingOut` is nested one level deeper here than anywhere else, and
        its image URLs are built from the request in the serialiser context
        (§0b). Worth a test of its own: a context that failed to reach a
        nested schema would not raise, it would quietly return stored paths
        and break every client that had been told to expect absolute ones.
        """
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                ListingImage.objects.create(
                    listing=self.listing,
                    file=SimpleUploadedFile("a.png", PNG_BYTES, "image/png"),
                    is_primary=True,
                )
                item = self.list_favorites().json()["items"][0]

        self.assertEqual(len(item["listing"]["images"]), 1)
        self.assertTrue(item["listing"]["images"][0]["url"].startswith("http://"))


class RemoveTests(FavoriteTestCase):
    """`DELETE /favorites/{listing_id}` — idempotent, and scoped to yourself."""

    def setUp(self):
        self.mine = Favorite.objects.create(
            organization=self.buyer_org, user=self.buyer_owner, listing=self.listing
        )

    def test_removing_deletes_the_row(self):
        response = self.remove_favorite()
        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(Favorite.objects.filter(pk=self.mine.pk).exists())

    def test_removing_is_logged_against_the_members_organization(self):
        self.remove_favorite()
        entry = ActivityLog.objects.get(content_type__model="favorite")
        self.assertEqual(entry.organization, self.buyer_org)
        self.assertEqual(entry.actor, self.buyer_owner)
        self.assertEqual(entry.action, ActivityLog.Action.DELETED)
        # Written before the delete, so the history keeps a label for
        # something that no longer exists.
        self.assertIn(str(self.listing), entry.target_repr)

    def test_removing_twice_is_still_a_204_and_one_audit_row(self):
        self.remove_favorite()
        self.assertEqual(self.remove_favorite().status_code, 204)
        self.assertEqual(ActivityLog.objects.filter(action="deleted").count(), 1)

    def test_removing_an_unknown_listing_is_a_204_not_a_404(self):
        """
        "It is not on your list" is the true and complete answer to both, and
        telling them apart would let the endpoint probe for listings the
        caller cannot otherwise see.
        """
        response = self.client.delete(
            "/api/v1/favorites/00000000-0000-4000-8000-000000000000",
            **auth(self.buyer_owner),
        )
        self.assertEqual(response.status_code, 204)

    def test_a_member_cannot_remove_a_colleagues_favorite(self):
        """
        The shared shortlist is readable by the team, but a member's picks are
        theirs to remove. The call succeeds — it is idempotent — and changes
        nothing.
        """
        self.assertEqual(self.remove_favorite(user=self.buyer_member).status_code, 204)
        self.assertTrue(Favorite.objects.filter(pk=self.mine.pk).exists())

    def test_another_organization_cannot_remove_it(self):
        self.assertEqual(self.remove_favorite(user=self.other_owner).status_code, 204)
        self.assertTrue(Favorite.objects.filter(pk=self.mine.pk).exists())
