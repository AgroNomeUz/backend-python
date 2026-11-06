"""
marketplace/test_requests.py
A buyer posting what they need, sellers quoting, and quotes becoming orders.
"""

from datetime import timedelta
from decimal import Decimal

from billing import services as billing
from billing.models import Payment
from core.models import ActivityLog
from marketplace.models import Order, Product, PurchaseRequest, Quote
from marketplace.order_services import ORDER_REFERENCE, expire_unpaid
from users.models import OrgPermission, Organization

from .test_support import MarketTestCase, fund, make_org, settle_payment


class RequestTestCase(MarketTestCase):
    def make_request(self, **overrides) -> dict:
        body = {
            "product_id": str(self.tomato.public_id),
            "quantity": "300.000",
            "needed_from": str(self.today + timedelta(days=1)),
            "needed_until": str(self.today + timedelta(days=10)),
            "target_price": "7000.00",
            "delivery_address": "Chirchiq, Factory 1",
        }
        body.update(overrides)
        response = self.post("market/requests", body)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def quote_body(self, **overrides) -> dict:
        body = {
            "unit_price": "7200.00",
            "quantity": "300.000",
            "valid_until": str(self.today + timedelta(days=3)),
            "grade": "a",
        }
        body.update(overrides)
        return body

    def quote(self, request_id, user=None, **overrides):
        return self.post(
            f"market/requests/{request_id}/quotes",
            self.quote_body(**overrides),
            user or self.seller_owner,
        )

    def accept_body(self, **overrides) -> dict:
        body = {
            "delivery_date": str(self.today + timedelta(days=2)),
            "delivery_method": "seller_delivery",
            "delivery_address": "Chirchiq, Factory 1",
        }
        body.update(overrides)
        return body


class RequestTests(RequestTestCase):
    def test_posting_a_request(self):
        data = self.make_request()
        self.assertEqual(data["status"], "open")
        self.assertEqual(data["customer"]["id"], str(self.buyer.public_id))
        self.assertEqual(Decimal(data["quantity_covered"]), 0)
        purchase_request = PurchaseRequest.objects.get(public_id=data["id"])
        self.assertEqual(purchase_request.delivery_region, self.region)
        self.assertTrue(
            ActivityLog.objects.filter(
                object_id=purchase_request.pk, organization=self.buyer, action="created"
            ).exists()
        )

    def test_dates_must_make_sense(self):
        body = {
            "product_id": str(self.tomato.public_id),
            "quantity": "1.000",
            "needed_from": str(self.today + timedelta(days=5)),
            "needed_until": str(self.today + timedelta(days=1)),
        }
        self.assertEqual(self.post("market/requests", body).status_code, 400)

    def test_the_feed_needs_a_token(self):
        self.assertEqual(self.get("market/requests", user=False).status_code, 401)

    def test_the_feed_is_for_other_organizations(self):
        data = self.make_request()
        mine = self.get("market/requests").json()
        self.assertEqual(mine["count"], 0)
        theirs = self.get("market/requests", self.seller_owner).json()
        self.assertEqual([row["id"] for row in theirs["items"]], [data["id"]])

    def test_a_past_request_leaves_the_feed(self):
        data = self.make_request()
        PurchaseRequest.objects.filter(public_id=data["id"]).update(
            needed_from=self.today - timedelta(days=9),
            needed_until=self.today - timedelta(days=1),
        )
        self.assertEqual(self.get("market/requests", self.seller_owner).json()["count"], 0)

    def test_feed_filters(self):
        self.make_request(is_recurring=True)
        rows = self.get("market/requests?is_recurring=false", self.seller_owner).json()
        self.assertEqual(rows["count"], 0)
        rows = self.get("market/requests?product=tomato", self.seller_owner).json()
        self.assertEqual(rows["count"], 1)

    def test_closing_rejects_the_waiting_quotes(self):
        data = self.make_request()
        quote = self.quote(data["id"]).json()
        response = self.post(f"market/requests/{data['id']}/close")
        self.assertEqual(response.json()["status"], "closed")
        self.assertEqual(Quote.objects.get(public_id=quote["id"]).status, "rejected")
        self.assertEqual(self.post(f"market/requests/{data['id']}/close").status_code, 409)

    def test_only_the_buyer_can_close(self):
        data = self.make_request()
        response = self.post(f"market/requests/{data['id']}/close", user=self.seller_owner)
        self.assertEqual(response.status_code, 404)


class QuoteTests(RequestTestCase):
    def test_quoting(self):
        data = self.make_request()
        response = self.quote(data["id"])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["provider"]["id"], str(self.seller.public_id))

    def test_you_cannot_quote_on_your_own_request(self):
        data = self.make_request()
        self.assertEqual(self.quote(data["id"], self.buyer_owner).status_code, 400)

    def test_regulated_goods_need_a_verified_seller_here_too(self):
        beef = Product.objects.create(
            category=self.vegetables, name="Beef", slug="beef", is_regulated=True
        )
        data = self.make_request(product_id=str(beef.public_id))
        self.assertFalse(self.outsider.is_verified)
        self.assertEqual(self.quote(data["id"], self.outsider_owner).status_code, 403)
        self.assertEqual(self.quote(data["id"]).status_code, 201)

    def test_a_quote_on_regulated_goods_dies_with_the_sellers_verification(self):
        beef = Product.objects.create(
            category=self.vegetables, name="Beef", slug="beef", is_regulated=True
        )
        data = self.make_request(product_id=str(beef.public_id))
        quote = self.quote(data["id"]).json()
        Organization.objects.filter(pk=self.seller.pk).update(is_verified=False)
        response = self.post(f"market/quotes/{quote['id']}/accept", self.accept_body())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Quote.objects.get(public_id=quote["id"]).status, "submitted")
        self.assertEqual(Order.objects.count(), 0)

    def test_one_live_quote_per_seller(self):
        data = self.make_request()
        first = self.quote(data["id"]).json()
        self.assertEqual(self.quote(data["id"]).status_code, 409)
        self.post(f"market/quotes/{first['id']}/withdraw", user=self.seller_owner)
        self.assertEqual(self.quote(data["id"]).status_code, 201)

    def test_an_expired_quote_cannot_be_sent(self):
        data = self.make_request()
        response = self.quote(data["id"], valid_until=str(self.today - timedelta(days=1)))
        self.assertEqual(response.status_code, 400)

    def test_a_closed_request_takes_no_quotes(self):
        data = self.make_request()
        self.post(f"market/requests/{data['id']}/close")
        self.assertEqual(self.quote(data["id"]).status_code, 404)

    def test_the_buyer_sees_every_quote_cheapest_first(self):
        data = self.make_request()
        self.quote(data["id"], unit_price="7500.00")
        self.quote(data["id"], self.outsider_owner, unit_price="7000.00")
        prices = [
            row["unit_price"]
            for row in self.get(f"market/requests/{data['id']}/quotes").json()["items"]
        ]
        self.assertEqual([Decimal(price) for price in prices], [7000, 7500])

    def test_a_seller_sees_only_their_own_quote(self):
        data = self.make_request()
        self.quote(data["id"], unit_price="7500.00")
        self.quote(data["id"], self.outsider_owner, unit_price="7000.00")
        rows = self.get(f"market/requests/{data['id']}/quotes", self.seller_owner).json()
        self.assertEqual(rows["count"], 1)
        self.assertEqual(Decimal(rows["items"][0]["unit_price"]), 7500)

    def test_a_seller_can_still_read_a_closed_request_they_quoted_on(self):
        data = self.make_request()
        self.quote(data["id"])
        self.post(f"market/requests/{data['id']}/close")
        path = f"market/requests/{data['id']}"
        self.assertEqual(self.get(path, self.seller_owner).status_code, 200)
        self.assertEqual(self.get(path, self.outsider_owner).status_code, 404)

    def test_my_quotes(self):
        data = self.make_request()
        self.quote(data["id"])
        rows = self.get("market/quotes/my", self.seller_owner).json()
        self.assertEqual(rows["count"], 1)

    def test_rejecting_a_quote(self):
        data = self.make_request()
        quote = self.quote(data["id"]).json()
        response = self.post(f"market/quotes/{quote['id']}/reject")
        self.assertEqual(response.json()["status"], "rejected")
        self.assertEqual(self.post(f"market/quotes/{quote['id']}/reject").status_code, 409)

    def test_only_the_buyer_can_reject_and_only_the_seller_can_withdraw(self):
        data = self.make_request()
        quote = self.quote(data["id"]).json()
        path = f"market/quotes/{quote['id']}"
        self.assertEqual(self.post(f"{path}/reject", user=self.seller_owner).status_code, 404)
        self.assertEqual(self.post(f"{path}/withdraw", user=self.buyer_owner).status_code, 404)


class AcceptingQuotesTests(RequestTestCase):
    def test_accepting_makes_an_order_waiting_for_payment(self):
        data = self.make_request()
        quote = self.quote(data["id"]).json()
        response = self.post(f"market/quotes/{quote['id']}/accept", self.accept_body())
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["status"], "awaiting_payment")
        self.assertEqual(Decimal(body["subtotal"]), Decimal("2160000.00"))
        self.assertEqual(body["quote_id"], quote["id"])
        self.assertEqual(body["provider"]["id"], str(self.seller.public_id))
        self.assertIsNotNone(body["pay_by"])

    def take(self, quote) -> str:
        response = self.post(f"market/quotes/{quote['id']}/accept", self.accept_body())
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["id"]

    def pay(self, order_id) -> None:
        fund(self.buyer, Order.objects.get(public_id=order_id).subtotal)
        response = self.post(f"market/orders/{order_id}/pay", {"method": "wallet"})
        self.assertEqual(response.status_code, 200, response.content)

    def status_of(self, quote) -> str:
        return Quote.objects.get(public_id=quote["id"]).status

    def request_status(self, data) -> str:
        return PurchaseRequest.objects.get(public_id=data["id"]).status

    def test_a_request_closes_once_paid_orders_cover_it(self):
        data = self.make_request()
        winner = self.quote(data["id"]).json()
        other = self.quote(data["id"], self.outsider_owner).json()

        order_id = self.take(winner)
        self.assertEqual(self.request_status(data), "open")
        self.assertEqual(self.status_of(other), "submitted")

        self.pay(order_id)
        self.assertEqual(self.request_status(data), "closed")
        self.assertEqual(self.status_of(other), "rejected")

    def test_an_order_paid_through_the_gateway_closes_the_request_too(self):
        data = self.make_request()
        order = Order.objects.get(public_id=self.take(self.quote(data["id"]).json()))
        settle_payment(
            billing.start_payment(
                organization=self.buyer,
                amount=order.subtotal,
                purpose=Payment.Purpose.ORDER,
                reference_type=ORDER_REFERENCE,
                reference_id=order.public_id,
            )
        )
        self.assertEqual(self.request_status(data), "closed")

    def test_an_unpaid_order_gives_the_request_back(self):
        data = self.make_request()
        winner = self.quote(data["id"]).json()
        other = self.quote(data["id"], self.outsider_owner).json()
        order = Order.objects.get(public_id=self.take(winner))

        expire_unpaid(order)

        self.assertEqual(self.request_status(data), "open")
        self.assertEqual(self.status_of(winner), "submitted")
        self.assertEqual(self.status_of(other), "submitted")
        self.assertTrue(
            ActivityLog.objects.filter(
                object_id=order.pk, action="updated", changes__quote__to="submitted"
            ).exists()
        )
        self.take(other)
        self.take(winner)
        self.assertEqual(Order.objects.count(), 3)

    def test_cancelling_before_paying_gives_the_quote_back(self):
        data = self.make_request()
        quote = self.quote(data["id"]).json()
        self.post(f"market/orders/{self.take(quote)}/cancel", {"reason": "Wrong one"})
        self.assertEqual(self.status_of(quote), "submitted")

    def test_a_refunded_order_keeps_its_quote(self):
        data = self.make_request()
        quote = self.quote(data["id"], quantity="100.000").json()
        order_id = self.take(quote)
        self.pay(order_id)
        self.post(f"market/orders/{order_id}/cancel", {"reason": "Wrong one"})
        self.assertEqual(self.status_of(quote), "accepted")

    def test_a_refunded_order_does_not_cover_the_request(self):
        data = self.make_request()
        first = self.quote(data["id"], quantity="200.000").json()
        second = self.quote(data["id"], self.outsider_owner, quantity="100.000").json()
        refunded = self.take(first)
        self.pay(refunded)
        self.post(f"market/orders/{refunded}/cancel", {"reason": "Wrong one"})
        self.pay(self.take(second))
        self.assertEqual(self.request_status(data), "open")

    def test_a_lapsed_quote_is_not_given_back(self):
        data = self.make_request()
        quote = self.quote(data["id"]).json()
        order = Order.objects.get(public_id=self.take(quote))
        Quote.objects.filter(public_id=quote["id"]).update(
            valid_until=self.today - timedelta(days=1)
        )
        expire_unpaid(order)
        self.assertEqual(self.status_of(quote), "accepted")

    def test_a_newer_quote_from_the_seller_wins(self):
        data = self.make_request()
        first = self.quote(data["id"]).json()
        order = Order.objects.get(public_id=self.take(first))
        second = self.quote(data["id"]).json()
        expire_unpaid(order)
        self.assertEqual(self.status_of(first), "accepted")
        self.assertEqual(self.status_of(second), "submitted")

    def test_a_closed_request_gets_no_quote_back(self):
        data = self.make_request()
        quote = self.quote(data["id"]).json()
        order = Order.objects.get(public_id=self.take(quote))
        self.post(f"market/requests/{data['id']}/close")
        expire_unpaid(order)
        self.assertEqual(self.status_of(quote), "accepted")

    def test_several_small_farms_can_cover_one_request(self):
        data = self.make_request(quantity="300.000")
        first = self.quote(data["id"], quantity="100.000").json()
        second = self.quote(data["id"], self.outsider_owner, quantity="150.000").json()
        third_org, third_owner = make_org("third-farm", self.region)
        third = self.quote(data["id"], third_owner, quantity="50.000").json()

        orders = [self.take(first)]
        read = self.get(f"market/requests/{data['id']}").json()
        self.assertEqual(read["status"], "open")
        self.assertEqual(Decimal(read["quantity_covered"]), 100)

        orders += [self.take(second), self.take(third)]
        read = self.get(f"market/requests/{data['id']}").json()
        self.assertEqual(read["status"], "open")
        self.assertEqual(Decimal(read["quantity_covered"]), 300)
        self.assertEqual(Order.objects.filter(customer_organization=self.buyer).count(), 3)

        self.pay(orders[0])
        self.pay(orders[1])
        self.assertEqual(self.request_status(data), "open")
        self.pay(orders[2])
        self.assertEqual(self.request_status(data), "closed")

    def test_covered_is_the_whole_total_for_a_seller_too(self):
        data = self.make_request()
        first = self.quote(data["id"], quantity="100.000").json()
        self.quote(data["id"], self.outsider_owner, quantity="50.000")
        self.post(f"market/quotes/{first['id']}/accept", self.accept_body())
        self.post(f"market/requests/{data['id']}/close")
        read = self.get(f"market/requests/{data['id']}", self.outsider_owner).json()
        self.assertEqual(Decimal(read["quantity_covered"]), 100)

    def test_a_quote_can_only_be_taken_once(self):
        data = self.make_request(quantity="900.000")
        quote = self.quote(data["id"]).json()
        path = f"market/quotes/{quote['id']}/accept"
        self.assertEqual(self.post(path, self.accept_body()).status_code, 201)
        self.assertEqual(self.post(path, self.accept_body()).status_code, 409)
        self.assertEqual(Order.objects.count(), 1)

    def test_an_expired_quote_cannot_be_taken(self):
        data = self.make_request()
        quote = self.quote(data["id"]).json()
        Quote.objects.filter(public_id=quote["id"]).update(
            valid_until=self.today - timedelta(days=1)
        )
        response = self.post(f"market/quotes/{quote['id']}/accept", self.accept_body())
        self.assertEqual(response.status_code, 409)

    def test_regular_supply_goes_through_a_contract(self):
        data = self.make_request(is_recurring=True)
        quote = self.quote(data["id"]).json()
        response = self.post(f"market/quotes/{quote['id']}/accept", self.accept_body())
        self.assertEqual(response.status_code, 409)

    def test_delivery_has_to_be_when_the_buyer_said(self):
        data = self.make_request()
        quote = self.quote(data["id"]).json()
        path = f"market/quotes/{quote['id']}/accept"
        late = self.accept_body(delivery_date=str(self.today + timedelta(days=30)))
        self.assertEqual(self.post(path, late).status_code, 400)
        past = self.accept_body(delivery_date=str(self.today - timedelta(days=1)))
        self.assertEqual(self.post(path, past).status_code, 400)

    def test_a_delivery_needs_an_address(self):
        data = self.make_request()
        quote = self.quote(data["id"]).json()
        response = self.post(
            f"market/quotes/{quote['id']}/accept", self.accept_body(delivery_address="  ")
        )
        self.assertEqual(response.status_code, 400)

    def test_only_the_buyer_can_accept(self):
        data = self.make_request()
        quote = self.quote(data["id"]).json()
        path = f"market/quotes/{quote['id']}/accept"
        self.assertEqual(
            self.post(path, self.accept_body(), self.seller_owner).status_code, 404
        )
        self.assertEqual(
            self.post(path, self.accept_body(), self.outsider_owner).status_code, 404
        )

    def test_accepting_needs_the_permission(self):
        data = self.make_request()
        quote = self.quote(data["id"]).json()
        viewer = self.member(OrgPermission.VIEW_MARKET)
        response = self.post(f"market/quotes/{quote['id']}/accept", self.accept_body(), viewer)
        self.assertEqual(response.status_code, 403)
