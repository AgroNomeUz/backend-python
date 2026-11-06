"""
marketplace/test_support.py
Shared fixtures: a seller, a buyer and an outsider, each with an owner, plus one
product and helpers that drive an order to any point in its life.

Owners hold every permission, so most tests act as them and the permission tests
build their own members. Each test ends by asking `reconcile()` whether the
books still agree, so any test that moves money also tests the ledger.
"""

import json
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from api.auth import create_access_token
from billing import gateway as gateways
from billing import services as billing
from billing.models import Payment
from billing.reconciliation import reconcile
from marketplace.models import (
    Offer,
    Order,
    Product,
    ProductCategory,
    SupplyContract,
    Unit,
    market_today,
)
from users.models import Organization, Region, User


def auth(user: User) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {create_access_token(user.public_id)}"}


def make_org(slug: str, region: Region, **fields) -> tuple[Organization, User]:
    owner = User.objects.create_user(username=f"{slug}-owner", password="x")
    org = Organization.objects.create(
        name=f"{slug} org", owner=owner, region=region, **fields
    )
    owner.organization = org
    owner.save(update_fields=["organization"])
    return org, owner


def fund(org: Organization, amount: str) -> None:
    """Put money in a wallet the way a real payment does."""
    payment = billing.start_payment(
        organization=org, amount=Decimal(amount), purpose=Payment.Purpose.TOP_UP
    )
    settle_payment(payment)


def settle_payment(payment: Payment) -> None:
    event = gateways.GatewayEvent(
        event_id=f"evt-{payment.public_id}",
        event_type=gateways.PAYMENT_SUCCEEDED,
        provider_reference=payment.provider_reference,
        amount=payment.amount,
    )
    billing.process_event("mock", event)


class MarketTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(name="Market Region", code="MKT", soato="1796")
        cls.seller, cls.seller_owner = make_org("seller", cls.region, is_verified=True)
        cls.buyer, cls.buyer_owner = make_org("buyer", cls.region)
        cls.outsider, cls.outsider_owner = make_org("outsider", cls.region)

        cls.vegetables = ProductCategory.objects.create(name="Vegetables", slug="vegetables")
        cls.tomato = Product.objects.create(
            category=cls.vegetables, name="Tomato", slug="tomato", eppo_code="LYPES"
        )

    def setUp(self):
        self.today = market_today()

    def tearDown(self):
        self.assertEqual(reconcile(), [])

    # ── requests ─────────────────────────────────────────────────────────────

    def call(self, method, path, data=None, user=None):
        kwargs = {}
        if data is not None:
            kwargs = {
                "data": json.dumps(data, default=str),
                "content_type": "application/json",
            }
        if user is not False:
            kwargs.update(auth(user or self.buyer_owner))
        return getattr(self.client, method)(f"/api/v1/{path}", **kwargs)

    def get(self, path, user=None):
        return self.call("get", path, user=user)

    def post(self, path, data=None, user=None):
        return self.call("post", path, data if data is not None else {}, user)

    def patch(self, path, data, user=None):
        return self.call("patch", path, data, user)

    def member(self, *codes, org=None) -> User:
        return User.objects.create_user(
            username=f"member-{User.objects.count()}",
            organization=org or self.buyer,
            permissions=sorted(code.value for code in codes),
        )

    # ── fixtures ─────────────────────────────────────────────────────────────

    def make_offer(self, **overrides) -> Offer:
        fields = {
            "organization": self.seller,
            "created_by": self.seller_owner,
            "product": self.tomato,
            "region": self.region,
            "title": "Greenhouse tomatoes",
            "status": Offer.Status.ACTIVE,
            "grade": "a",
            "unit": Unit.KILOGRAM,
            "price": Decimal("8000.00"),
            "quantity_available": Decimal("500.000"),
            "min_order_quantity": Decimal("10.000"),
            "available_from": self.today,
            "available_until": self.today + timedelta(days=7),
            "offers_pickup": True,
            "offers_delivery": True,
        }
        fields.update(overrides)
        return Offer.objects.create(**fields)

    def order_body(self, offer, **overrides) -> dict:
        body = {
            "offer_id": str(offer.public_id),
            "quantity": "100.000",
            "delivery_date": str(self.today + timedelta(days=1)),
            "delivery_method": "seller_delivery",
            "delivery_address": "Tashkent, Chilonzor 5",
        }
        body.update(overrides)
        return body

    def order_at(self, status: str, *, offer=None, amount="800000.00", **overrides) -> Order:
        """A 100 kg order at 8000, driven through the API to `status`."""
        offer = offer or self.make_offer()
        response = self.post("market/orders", self.order_body(offer, **overrides))
        self.assertEqual(response.status_code, 201, response.content)
        order = Order.objects.get(public_id=response.json()["id"])
        steps = {
            "awaiting_payment": ("confirm",),
            "paid": ("confirm", "pay"),
            "dispatched": ("confirm", "pay", "dispatch"),
            "delivered": ("confirm", "pay", "dispatch", "deliver"),
        }
        for step in steps.get(status, ()):
            self.step(order, step, amount)
        order.refresh_from_db()
        self.assertEqual(order.status, status)
        return order

    def step(self, order: Order, step: str, amount="800000.00") -> None:
        path = f"market/orders/{order.public_id}"
        if step == "pay":
            fund(self.buyer, amount)
            response = self.post(f"{path}/pay", {"method": "wallet"})
        else:
            response = self.post(f"{path}/{step}", user=self.seller_owner)
        self.assertEqual(response.status_code, 200, response.content)

    def contract_terms(self, **overrides) -> dict:
        terms = {
            "quantity_per_delivery": "200.000",
            "delivery_weekdays": [0, 1, 2, 3, 4, 5, 6],
            "start_date": str(self.today),
            "end_date": str(self.today + timedelta(days=90)),
            "delivery_address": "Chirchiq, Factory 1",
            "notice_days": 7,
        }
        terms.update(overrides)
        return terms

    def active_contract(self, offer=None, **overrides) -> SupplyContract:
        offer = offer or self.make_offer()
        body = {"offer_id": str(offer.public_id), **self.contract_terms(**overrides)}
        response = self.post("market/contracts", body)
        self.assertEqual(response.status_code, 201, response.content)
        contract_id = response.json()["id"]
        accepted = self.post(
            f"market/contracts/{contract_id}/accept", user=self.seller_owner
        )
        self.assertEqual(accepted.status_code, 200, accepted.content)
        return SupplyContract.objects.get(public_id=contract_id)


def days_from(start: date, days: int) -> date:
    return start + timedelta(days=days)
