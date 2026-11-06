"""
marketplace/test_orders.py
An order's whole life, and what it does to stock and money on the way.

Scoping mistakes and second effects are what cost real money here, so the tests
try the wrong side's action as often as the right one, repeat what should only
happen once, and always finish by checking the balances.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from billing import services as billing
from billing.models import EscrowHold, LedgerAccount, Payment
from billing.reconciliation import reconcile
from core.models import ActivityLog
from deals.models import Deal
from marketplace import contract_services, jobs
from marketplace import order_services as services
from marketplace.models import (
    Offer,
    Order,
    OrderDispute,
    Product,
    ProductCategory,
    Side,
    market_today,
)
from marketplace.reliability import provider_reliability
from users.models import OrgPermission, Region, User

from .test_support import MarketTestCase, fund, make_org, settle_payment

Status = Order.Status


def path(order: Order, action: str = "") -> str:
    return f"market/orders/{order.public_id}" + (f"/{action}" if action else "")


def commission_total() -> Decimal:
    return billing.balance_of(billing.platform_account(LedgerAccount.Kind.COMMISSION))


class HappyPathTests(MarketTestCase):
    def test_a_one_time_order_from_start_to_finish(self):
        offer = self.make_offer()
        fund(self.buyer, "1000000.00")

        placed = self.post("market/orders", self.order_body(offer))
        self.assertEqual(placed.status_code, 201, placed.content)
        body = placed.json()
        self.assertEqual(body["status"], "pending_confirmation")
        self.assertEqual(Decimal(body["unit_price"]), Decimal("8000.00"))
        self.assertEqual(Decimal(body["subtotal"]), Decimal("800000.00"))
        order = Order.objects.get(public_id=body["id"])
        offer.refresh_from_db()
        self.assertEqual(offer.quantity_available, Decimal("400.000"))

        self.post(path(order, "confirm"), user=self.seller_owner)
        paid = self.post(path(order, "pay"), {"method": "wallet"})
        self.assertEqual(paid.json()["order"]["status"], "paid")
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("200000.00"))
        self.assertEqual(billing.held_for(self.buyer), Decimal("800000.00"))
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("0.00"))

        self.post(path(order, "dispatch"), user=self.seller_owner)
        delivered = self.post(path(order, "deliver"), user=self.seller_owner).json()
        self.assertIsNotNone(delivered["acceptance_deadline"])

        done = self.post(path(order, "accept")).json()
        self.assertEqual(done["status"], "settled")
        self.assertEqual(Decimal(done["accepted_quantity"]), Decimal("100.000"))
        self.assertEqual(Decimal(done["settlement"]["released"]), Decimal("800000.00"))
        self.assertEqual(Decimal(done["settlement"]["commission"]), Decimal("12000.00"))
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("788000.00"))
        self.assertEqual(commission_total(), Decimal("12000.00"))
        self.assertEqual(billing.held_for(self.buyer), Decimal("0.00"))

        self.assertEqual(
            [event["to_status"] for event in done["events"]],
            [
                "pending_confirmation",
                "awaiting_payment",
                "paid",
                "dispatched",
                "delivered",
                "settled",
            ],
        )

    def test_a_settled_order_becomes_a_reviewable_deal(self):
        order = self.order_at("delivered")
        body = self.post(path(order, "accept")).json()
        deal = Deal.objects.get(public_id=body["deal_id"])
        self.assertEqual(deal.provider_organization, self.seller)
        self.assertEqual(deal.customer_organization, self.buyer)
        self.assertEqual(deal.status, Deal.Status.COMPLETED)
        review = self.post(f"deals/{deal.public_id}/review", {"rating": 5})
        self.assertEqual(review.status_code, 201, review.content)

    def test_the_seller_profile_shows_produce_sellers(self):
        path_to_profile = f"organizations/{self.seller.public_id}"
        self.assertEqual(self.get(path_to_profile, user=False).status_code, 404)
        self.make_offer()
        self.assertEqual(self.get(path_to_profile, user=False).status_code, 200)

    def test_each_step_is_audited_for_the_side_that_took_it(self):
        order = self.order_at("paid")
        rows = ActivityLog.objects.filter(object_id=order.pk, action="status_changed")
        confirm = rows.get(changes__status__to="awaiting_payment")
        self.assertEqual(confirm.organization, self.seller)
        self.assertEqual(confirm.actor, self.seller_owner)
        pay = rows.get(changes__status__to="paid")
        self.assertEqual(pay.organization, self.buyer)
        self.assertEqual(pay.actor, self.buyer_owner)

    def test_both_sides_can_read_an_order_and_nobody_else(self):
        order = self.order_at("pending_confirmation")
        self.assertEqual(self.get(path(order)).status_code, 200)
        self.assertEqual(self.get(path(order), self.seller_owner).status_code, 200)
        self.assertEqual(self.get(path(order), self.outsider_owner).status_code, 404)

    def test_listing_by_role(self):
        self.order_at("pending_confirmation")
        def count(user, query):
            return self.get(f"market/orders{query}", user).json()["count"]

        self.assertEqual(count(self.buyer_owner, "?role=customer"), 1)
        self.assertEqual(count(self.buyer_owner, "?role=provider"), 0)
        self.assertEqual(count(self.seller_owner, "?role=provider"), 1)
        self.assertEqual(count(self.outsider_owner, ""), 0)
        self.assertEqual(self.get("market/orders?role=nope").status_code, 400)


class PlacingTests(MarketTestCase):
    def place(self, offer, user=None, **overrides):
        return self.post("market/orders", self.order_body(offer, **overrides), user)

    def test_price_and_parties_come_from_the_server(self):
        offer = self.make_offer()
        body = self.order_body(
            offer, unit_price="1.00", provider_organization_id=str(self.buyer.public_id)
        )
        response = self.post("market/orders", body)
        self.assertEqual(Decimal(response.json()["unit_price"]), Decimal("8000.00"))
        self.assertEqual(response.json()["provider"]["id"], str(self.seller.public_id))

    def test_a_changed_price_is_not_quietly_charged(self):
        offer = self.make_offer()
        offer.price = Decimal("9000.00")
        offer.save(update_fields=["price"])
        response = self.place(offer, expected_unit_price="8000.00")
        self.assertEqual(response.status_code, 409)
        offer.refresh_from_db()
        self.assertEqual(offer.quantity_available, Decimal("500.000"))

    def test_an_order_keeps_its_price_when_the_offer_changes(self):
        offer = self.make_offer()
        order = self.order_at("pending_confirmation", offer=offer)
        self.patch(f"market/offers/{offer.public_id}", {"price": "1.00"}, self.seller_owner)
        order.refresh_from_db()
        self.assertEqual(order.unit_price, Decimal("8000.00"))

    def test_you_cannot_overbuy(self):
        offer = self.make_offer(quantity_available=Decimal("50.000"))
        self.assertEqual(self.place(offer).status_code, 409)
        offer.refresh_from_db()
        self.assertEqual(offer.quantity_available, Decimal("50.000"))

    def test_the_last_of_the_stock_can_be_bought(self):
        offer = self.make_offer(quantity_available=Decimal("100.000"))
        self.assertEqual(self.place(offer).status_code, 201)
        offer.refresh_from_db()
        self.assertEqual(offer.quantity_available, Decimal("0.000"))
        self.assertEqual(self.place(offer).status_code, 404)

    def test_the_minimum_order(self):
        offer = self.make_offer(min_order_quantity=Decimal("150.000"))
        self.assertEqual(self.place(offer).status_code, 400)

    def test_delivery_has_to_fit_the_offer(self):
        offer = self.make_offer()
        late = str(self.today + timedelta(days=30))
        self.assertEqual(self.place(offer, delivery_date=late).status_code, 400)
        early = str(self.today - timedelta(days=1))
        self.assertEqual(self.place(offer, delivery_date=early).status_code, 400)

    def test_a_past_delivery_date_inside_a_long_offer_is_refused(self):
        offer = self.make_offer(available_from=self.today - timedelta(days=5))
        early = str(self.today - timedelta(days=1))
        self.assertEqual(self.place(offer, delivery_date=early).status_code, 400)

    def test_delivery_method_has_to_be_offered(self):
        pickup_only = self.make_offer(offers_delivery=False)
        self.assertEqual(self.place(pickup_only).status_code, 400)
        delivery_only = self.make_offer(offers_pickup=False)
        response = self.place(delivery_only, delivery_method="pickup")
        self.assertEqual(response.status_code, 400)

    def test_a_delivery_needs_an_address(self):
        offer = self.make_offer()
        self.assertEqual(self.place(offer, delivery_address="   ").status_code, 400)

    def test_you_cannot_buy_from_yourself(self):
        offer = self.make_offer()
        self.assertEqual(self.place(offer, user=self.seller_owner).status_code, 400)

    def test_a_draft_cannot_be_bought(self):
        offer = self.make_offer(status=Offer.Status.DRAFT)
        self.assertEqual(self.place(offer).status_code, 404)

    def test_an_expired_offer_cannot_be_bought(self):
        offer = self.make_offer()
        Offer.objects.filter(pk=offer.pk).update(
            available_from=self.today - timedelta(days=9),
            available_until=self.today - timedelta(days=1),
        )
        self.assertEqual(self.place(offer).status_code, 404)

    def test_silly_quantities_are_refused(self):
        offer = self.make_offer()
        self.assertEqual(self.place(offer, quantity="0").status_code, 422)
        self.assertEqual(self.place(offer, quantity="-5").status_code, 422)
        self.assertEqual(self.place(offer, quantity="1.2345").status_code, 422)
        self.assertEqual(self.place(offer, quantity="NaN").status_code, 422)

    def test_a_total_too_large_for_the_ledger_is_refused(self):
        offer = self.make_offer(
            price=Decimal("99999999999999.00"),
            quantity_available=Decimal("999999999.000"),
        )
        response = self.place(offer, quantity="999999.000")
        self.assertEqual(response.status_code, 400)

    def test_a_double_tap_places_one_order(self):
        offer = self.make_offer()
        first = self.place(offer, idempotency_key="tap")
        second = self.place(offer, idempotency_key="tap")
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(Order.objects.count(), 1)
        offer.refresh_from_db()
        self.assertEqual(offer.quantity_available, Decimal("400.000"))

    def test_a_retry_finds_its_order_after_taking_the_last_stock(self):
        offer = self.make_offer(quantity_available=Decimal("100.000"))
        first = self.place(offer, idempotency_key="tap")
        second = self.place(offer, idempotency_key="tap")
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(first.json()["id"], second.json()["id"])

    def test_a_reused_key_for_another_offer_is_refused(self):
        self.place(self.make_offer(), idempotency_key="tap")
        response = self.place(self.make_offer(), idempotency_key="tap")
        self.assertEqual(response.status_code, 409)

    def test_a_reused_key_for_another_quantity_is_refused(self):
        offer = self.make_offer()
        self.place(offer, idempotency_key="tap")
        response = self.place(offer, idempotency_key="tap", quantity="120.000")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Order.objects.count(), 1)

    def test_a_key_that_lost_the_race_is_checked_too(self):
        offer = self.make_offer()
        self.place(offer, idempotency_key="tap")
        with mock.patch.object(Order.objects, "filter") as lookup:
            lookup.return_value.first.return_value = None
            response = self.place(offer, idempotency_key="tap", quantity="120.000")
        self.assertEqual(response.status_code, 409)

    def test_placing_needs_the_permission(self):
        offer = self.make_offer()
        viewer = self.member(OrgPermission.VIEW_MARKET)
        self.assertEqual(self.place(offer, user=viewer).status_code, 403)
        manager = self.member(OrgPermission.MANAGE_MARKET)
        self.assertEqual(self.place(offer, user=manager).status_code, 201)


class TheSellersAnswerTests(MarketTestCase):
    def test_only_the_seller_can_answer(self):
        order = self.order_at("pending_confirmation")
        for action in ("confirm", "decline"):
            self.assertEqual(self.post(path(order, action)).status_code, 404)
            self.assertEqual(
                self.post(path(order, action), user=self.outsider_owner).status_code, 404
            )

    def test_declining_returns_the_stock(self):
        offer = self.make_offer()
        order = self.order_at("pending_confirmation", offer=offer)
        body = self.post(
            path(order, "decline"), {"reason": "Sold at the bazaar"}, self.seller_owner
        ).json()
        self.assertEqual(body["status"], "declined")
        self.assertEqual(body["cancelled_by"], "provider")
        self.assertEqual(body["cancel_reason"], "Sold at the bazaar")
        offer.refresh_from_db()
        self.assertEqual(offer.quantity_available, Decimal("500.000"))

    def test_an_answer_cannot_be_changed(self):
        order = self.order_at("awaiting_payment")
        self.assertEqual(self.post(path(order, "decline"), user=self.seller_owner).status_code, 409)
        self.assertEqual(self.post(path(order, "confirm"), user=self.seller_owner).status_code, 409)

    def test_a_one_time_order_keeps_its_quantity(self):
        order = self.order_at("pending_confirmation")
        response = self.post(path(order, "confirm"), {"quantity": "90.000"}, self.seller_owner)
        self.assertEqual(response.status_code, 400)

    def test_unpaid_goods_cannot_be_dispatched(self):
        order = self.order_at("awaiting_payment")
        self.assertEqual(self.post(path(order, "dispatch"), user=self.seller_owner).status_code, 409)

    def test_goods_cannot_be_delivered_before_dispatch(self):
        order = self.order_at("paid")
        self.assertEqual(self.post(path(order, "deliver"), user=self.seller_owner).status_code, 409)


class PayingTests(MarketTestCase):
    def test_opening_a_checkout_is_audited(self):
        order = self.order_at("awaiting_payment")
        self.post(path(order, "pay"), {"method": "gateway"})
        payment = Payment.objects.get(reference_id=order.public_id)
        row = ActivityLog.objects.get(object_id=payment.pk, action="created")
        self.assertEqual(row.organization, self.buyer)
        self.assertEqual(row.actor, self.buyer_owner)
        self.assertEqual(row.changes["amount"]["to"], "800000.00")

    def test_an_empty_wallet_cannot_pay(self):
        order = self.order_at("awaiting_payment")
        fund(self.buyer, "799999.99")
        response = self.post(path(order, "pay"), {"method": "wallet"})
        self.assertEqual(response.status_code, 409)
        order.refresh_from_db()
        self.assertEqual(order.status, Status.AWAITING_PAYMENT)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("799999.99"))

    def test_an_order_cannot_be_paid_twice(self):
        order = self.order_at("paid")
        fund(self.buyer, "800000.00")
        response = self.post(path(order, "pay"), {"method": "wallet"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("800000.00"))

    def test_an_order_cannot_be_paid_before_the_seller_confirms(self):
        order = self.order_at("pending_confirmation")
        fund(self.buyer, "800000.00")
        response = self.post(path(order, "pay"), {"method": "wallet"})
        self.assertEqual(response.status_code, 409)

    def test_only_the_buyer_can_pay(self):
        order = self.order_at("awaiting_payment")
        fund(self.seller, "800000.00")
        response = self.post(path(order, "pay"), {"method": "wallet"}, self.seller_owner)
        self.assertEqual(response.status_code, 404)

    def test_paying_needs_billing_permission_not_market_permission(self):
        order = self.order_at("awaiting_payment")
        fund(self.buyer, "800000.00")
        market = self.member(OrgPermission.MANAGE_MARKET)
        finance = self.member(OrgPermission.MANAGE_BILLING)
        self.assertEqual(self.post(path(order, "pay"), {"method": "wallet"}, market).status_code, 403)
        self.assertEqual(self.post(path(order, "pay"), {"method": "wallet"}, finance).status_code, 200)

    def checkout(self, order):
        response = self.post(path(order, "pay"), {"method": "gateway"})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_paying_by_card_moves_the_order_when_the_money_arrives(self):
        order = self.order_at("awaiting_payment")
        body = self.checkout(order)
        self.assertEqual(body["order"]["status"], "awaiting_payment")
        self.assertTrue(body["payment"]["checkout_url"])

        done = self.post(f"billing/payments/{body['payment']['id']}/mock-complete")
        self.assertEqual(done.json()["status"], "succeeded")

        order.refresh_from_db()
        self.assertEqual(order.status, Status.PAID)
        self.assertEqual(order.hold.amount, Decimal("800000.00"))
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("0.00"))

    def test_a_replayed_payment_holds_once(self):
        order = self.order_at("awaiting_payment")
        body = self.checkout(order)
        payment = Payment.objects.get(public_id=body["payment"]["id"])
        settle_payment(payment)
        settle_payment(payment)
        self.assertEqual(EscrowHold.objects.count(), 1)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("0.00"))

    def test_two_tabs_share_one_checkout(self):
        order = self.order_at("awaiting_payment")
        first = self.checkout(order)
        second = self.checkout(order)
        self.assertEqual(first["payment"]["id"], second["payment"]["id"])
        self.assertEqual(Payment.objects.filter(purpose="order").count(), 1)

    def test_a_failed_card_can_be_tried_again(self):
        order = self.order_at("awaiting_payment")
        first = self.checkout(order)
        self.post(f"billing/payments/{first['payment']['id']}/mock-complete", {"outcome": "failed"})
        second = self.checkout(order)
        self.assertNotEqual(first["payment"]["id"], second["payment"]["id"])

    def test_a_late_payment_stays_in_the_wallet(self):
        order = self.order_at("awaiting_payment")
        body = self.checkout(order)
        Order.objects.filter(pk=order.pk).update(pay_by=timezone.now() - timedelta(minutes=1))
        jobs.run_all()
        self.post(f"billing/payments/{body['payment']['id']}/mock-complete")

        order.refresh_from_db()
        self.assertEqual(order.status, Status.EXPIRED)
        self.assertIsNone(order.hold)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("800000.00"))

    def test_a_payment_for_a_cancelled_order_stays_in_the_wallet(self):
        order = self.order_at("awaiting_payment")
        body = self.checkout(order)
        self.post(path(order, "cancel"), {"reason": "Changed my mind"})
        self.post(f"billing/payments/{body['payment']['id']}/mock-complete")
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("800000.00"))

    @override_settings(BILLING_COMMISSION_RATE=Decimal("0.0200"))
    def test_the_rate_is_fixed_when_the_money_is_held(self):
        order = self.order_at("delivered")
        with override_settings(BILLING_COMMISSION_RATE=Decimal("0.5000")):
            self.post(path(order, "accept"))
        self.assertEqual(commission_total(), Decimal("16000.00"))


class CancellingTests(MarketTestCase):
    def test_a_refund_survives_stock_at_the_columns_limit(self):
        limit = Decimal("999999999.999")
        offer = self.make_offer()
        order = self.order_at("paid", offer=offer)
        Offer.objects.filter(pk=offer.pk).update(quantity_available=limit)
        response = self.post(path(order, "cancel"), {"reason": "Plans changed"})
        self.assertEqual(response.json()["status"], "cancelled", response.content)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("800000.00"))
        offer.refresh_from_db()
        self.assertEqual(offer.quantity_available, limit)

    def test_a_paid_order_is_refunded_in_full(self):
        offer = self.make_offer()
        order = self.order_at("paid", offer=offer)
        body = self.post(path(order, "cancel"), {"reason": "Plans changed"}).json()
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(body["cancelled_by"], "customer")
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("800000.00"))
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("0.00"))
        self.assertEqual(commission_total(), Decimal("0.00"))
        offer.refresh_from_db()
        self.assertEqual(offer.quantity_available, Decimal("500.000"))

    def test_the_seller_can_cancel_before_dispatch(self):
        order = self.order_at("paid")
        body = self.post(path(order, "cancel"), user=self.seller_owner).json()
        self.assertEqual(body["cancelled_by"], "provider")
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("800000.00"))

    def test_nothing_can_be_cancelled_once_it_has_left(self):
        order = self.order_at("dispatched")
        self.assertEqual(self.post(path(order, "cancel")).status_code, 409)
        self.assertEqual(self.post(path(order, "cancel"), user=self.seller_owner).status_code, 409)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("0.00"))

    def test_a_finished_order_cannot_be_cancelled(self):
        order = self.order_at("delivered")
        self.post(path(order, "accept"))
        self.assertEqual(self.post(path(order, "cancel")).status_code, 409)

    def test_a_failed_cancel_changes_nothing(self):
        offer = self.make_offer()
        order = self.order_at("delivered", offer=offer)
        self.post(path(order, "accept"))
        before = billing.wallet_balance(self.buyer)
        self.post(path(order, "cancel"))
        offer.refresh_from_db()
        self.assertEqual(offer.quantity_available, Decimal("400.000"))
        self.assertEqual(billing.wallet_balance(self.buyer), before)

    def test_an_outsider_cannot_cancel(self):
        order = self.order_at("paid")
        self.assertEqual(self.post(path(order, "cancel"), user=self.outsider_owner).status_code, 404)


class AcceptingTests(MarketTestCase):
    def test_a_buyer_can_accept_before_the_seller_marks_delivery(self):
        order = self.order_at("dispatched")
        self.assertEqual(self.post(path(order, "accept")).json()["status"], "settled")

    def test_only_the_buyer_can_accept(self):
        order = self.order_at("delivered")
        self.assertEqual(self.post(path(order, "accept"), user=self.seller_owner).status_code, 404)
        self.assertEqual(self.post(path(order, "accept"), user=self.outsider_owner).status_code, 404)
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("0.00"))

    def test_accepting_twice_pays_once(self):
        order = self.order_at("delivered")
        self.post(path(order, "accept"))
        self.assertEqual(self.post(path(order, "accept")).status_code, 409)
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("788000.00"))

    def test_an_unpaid_order_cannot_be_accepted(self):
        order = self.order_at("awaiting_payment")
        self.assertEqual(self.post(path(order, "accept")).status_code, 409)


class RejectingTests(MarketTestCase):
    def reject(self, order, accepted="0", reason="quality", **extra):
        return self.post(
            path(order, "reject"),
            {"accepted_quantity": accepted, "reason": reason, **extra},
        )

    def test_a_partial_rejection_pays_the_accepted_share_now(self):
        order = self.order_at("delivered")
        body = self.reject(order, "75.000", "quality", description="Bruised").json()
        self.assertEqual(body["status"], "disputed")
        self.assertEqual(Decimal(body["dispute"]["disputed_amount"]), Decimal("200000.00"))
        self.assertEqual(Decimal(body["settlement"]["released"]), Decimal("600000.00"))
        self.assertEqual(Decimal(body["settlement"]["held"]), Decimal("200000.00"))
        # 1.5% of the accepted 600,000 and nothing on the rest.
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("591000.00"))
        self.assertEqual(billing.held_for(self.buyer), Decimal("200000.00"))

    def test_rejecting_everything_pays_nothing_yet(self):
        order = self.order_at("delivered")
        body = self.reject(order).json()
        self.assertEqual(body["status"], "disputed")
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("0.00"))
        self.assertEqual(billing.held_for(self.buyer), Decimal("800000.00"))

    def test_conceding_refunds_what_was_held(self):
        order = self.order_at("delivered")
        self.reject(order, "75.000")
        body = self.post(path(order, "dispute/concede"), user=self.seller_owner).json()
        self.assertEqual(body["status"], "settled")
        self.assertEqual(body["dispute"]["status"], "conceded")
        self.assertEqual(Decimal(body["dispute"]["refund_amount"]), Decimal("200000.00"))
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("200000.00"))
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("591000.00"))
        self.assertEqual(billing.held_for(self.buyer), Decimal("0.00"))
        self.assertIsNotNone(body["deal_id"])

    def test_a_full_refund_is_not_a_completed_deal(self):
        order = self.order_at("delivered")
        self.reject(order)
        body = self.post(path(order, "dispute/concede"), user=self.seller_owner).json()
        self.assertIsNone(body["deal_id"])
        self.assertFalse(Deal.objects.exists())

    def test_only_the_seller_can_concede(self):
        order = self.order_at("delivered")
        self.reject(order)
        self.assertEqual(self.post(path(order, "dispute/concede")).status_code, 404)

    def test_a_dispute_can_only_be_conceded_once(self):
        order = self.order_at("delivered")
        self.reject(order)
        self.post(path(order, "dispute/concede"), user=self.seller_owner)
        again = self.post(path(order, "dispute/concede"), user=self.seller_owner)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("800000.00"))

    def test_a_disputed_order_cannot_then_be_accepted(self):
        order = self.order_at("delivered")
        self.reject(order)
        self.assertEqual(self.post(path(order, "accept")).status_code, 409)

    def test_accepting_everything_is_not_a_rejection(self):
        order = self.order_at("delivered")
        self.assertEqual(self.reject(order, "100.000").status_code, 400)

    def test_accepted_quantity_cannot_exceed_the_order(self):
        order = self.order_at("delivered")
        self.assertEqual(self.reject(order, "150.000").status_code, 400)
        self.assertEqual(self.reject(order, "-1").status_code, 422)

    def test_an_unknown_reason_is_refused(self):
        order = self.order_at("delivered")
        self.assertEqual(self.reject(order, reason="vibes").status_code, 422)

    def test_rejecting_after_the_window_is_refused(self):
        order = self.order_at("delivered")
        Order.objects.filter(pk=order.pk).update(
            acceptance_deadline=timezone.now() - timedelta(minutes=1)
        )
        self.assertEqual(self.reject(order).status_code, 409)

    def test_goods_that_never_arrived_can_be_rejected(self):
        order = self.order_at("dispatched")
        body = self.reject(order, reason="not_delivered").json()
        self.assertEqual(body["dispute"]["reason"], "not_delivered")

    def test_only_the_buyer_can_reject(self):
        order = self.order_at("delivered")
        response = self.post(
            path(order, "reject"), {"reason": "quality"}, self.seller_owner
        )
        self.assertEqual(response.status_code, 404)


class StaffResolutionTests(MarketTestCase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(username="staff", is_staff=True)
        self.order = self.order_at("delivered")
        self.post(
            path(self.order, "reject"), {"accepted_quantity": "0", "reason": "quality"}
        )

    def resolve(self, refund="300000.00", user=None, note="Half were fine"):
        return self.post(
            f"market/disputes/{self.order.public_id}/resolve",
            {"refund_amount": refund, "note": note},
            user or self.staff,
        )

    def test_staff_can_split_what_is_held(self):
        body = self.resolve().json()
        self.assertEqual(body["status"], "settled")
        self.assertEqual(body["dispute"]["status"], "resolved")
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("300000.00"))
        # 500,000 less 1.5%.
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("492500.00"))
        self.assertEqual(Decimal(body["accepted_quantity"]), Decimal("62.500"))

    def test_a_full_refund_or_a_full_release(self):
        self.resolve("800000.00")
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("800000.00"))

    def test_a_decision_for_the_seller(self):
        self.resolve("0")
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("788000.00"))

    def test_nobody_but_staff_can_decide_or_see_the_queue(self):
        self.assertEqual(self.resolve(user=self.buyer_owner).status_code, 404)
        self.assertEqual(self.resolve(user=self.seller_owner).status_code, 404)
        self.assertEqual(self.get("market/disputes", self.buyer_owner).status_code, 404)

    def test_the_queue_lists_open_disputes(self):
        rows = self.get("market/disputes", self.staff).json()
        self.assertEqual(rows["count"], 1)
        self.resolve()
        self.assertEqual(self.get("market/disputes", self.staff).json()["count"], 0)

    def test_more_than_is_disputed_cannot_be_refunded(self):
        self.assertEqual(self.resolve("800000.01").status_code, 400)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("0.00"))

    def test_a_decision_needs_a_reason(self):
        self.assertEqual(self.resolve(note="  ").status_code, 422)

    def test_a_dispute_can_only_be_decided_once(self):
        self.resolve()
        self.assertEqual(self.resolve().status_code, 409)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("300000.00"))

    def test_both_sides_see_the_decision_in_their_history(self):
        self.resolve()
        rows = ActivityLog.objects.filter(
            object_id=self.order.pk, changes__status__to="settled"
        )
        self.assertEqual({row.organization for row in rows}, {self.seller, self.buyer})
        self.assertEqual({row.actor for row in rows}, {self.staff})


class DeadlineTests(MarketTestCase):
    def overdue(self, order, field):
        Order.objects.filter(pk=order.pk).update(
            **{field: timezone.now() - timedelta(minutes=1)}
        )

    def test_an_unconfirmed_order_expires_and_returns_its_stock(self):
        offer = self.make_offer()
        order = self.order_at("pending_confirmation", offer=offer)
        self.overdue(order, "confirm_by")
        self.assertEqual(jobs.run_all()["expired_unconfirmed"], 1)
        order.refresh_from_db()
        self.assertEqual(order.status, Status.EXPIRED)
        self.assertEqual(order.cancelled_by, Side.PROVIDER)
        offer.refresh_from_db()
        self.assertEqual(offer.quantity_available, Decimal("500.000"))

    def test_an_unpaid_order_expires_as_the_buyers_doing(self):
        order = self.order_at("awaiting_payment")
        self.overdue(order, "pay_by")
        self.assertEqual(jobs.run_all()["expired_unpaid"], 1)
        order.refresh_from_db()
        self.assertEqual(order.status, Status.EXPIRED)
        self.assertEqual(order.cancelled_by, Side.CUSTOMER)

    def test_orders_within_their_deadlines_are_left_alone(self):
        order = self.order_at("delivered")
        counts = jobs.run_all()
        self.assertEqual(counts["auto_accepted"], 0)
        order.refresh_from_db()
        self.assertEqual(order.status, Status.DELIVERED)

    def test_a_silent_buyer_cannot_sit_on_the_sellers_money(self):
        order = self.order_at("delivered")
        self.overdue(order, "acceptance_deadline")
        self.assertEqual(jobs.run_all()["auto_accepted"], 1)
        order.refresh_from_db()
        self.assertEqual(order.status, Status.SETTLED)
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("788000.00"))
        event = order.events.last()
        self.assertEqual(event.side, Side.PLATFORM)
        self.assertIsNone(event.actor)

    def test_a_disputed_order_is_never_auto_accepted(self):
        order = self.order_at("delivered")
        self.post(path(order, "reject"), {"reason": "quality"})
        self.overdue(order, "acceptance_deadline")
        self.assertEqual(jobs.run_all()["auto_accepted"], 0)
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("0.00"))

    def test_running_the_jobs_twice_changes_nothing(self):
        order = self.order_at("delivered")
        self.overdue(order, "acceptance_deadline")
        jobs.run_all()
        self.assertEqual(jobs.run_all()["auto_accepted"], 0)
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("788000.00"))

    def test_one_bad_order_does_not_stop_the_others(self):
        good = self.order_at("delivered")
        bad = self.order_at("delivered", amount="800000.00")
        self.overdue(good, "acceptance_deadline")
        self.overdue(bad, "acceptance_deadline")
        original = services.accept_order

        def explode_on_bad(actor, order):
            if order.pk == bad.pk:
                raise RuntimeError("boom")
            return original(actor, order)

        with self.assertLogs("marketplace.jobs", level="ERROR"):
            with mock.patch("marketplace.jobs.accept_order", explode_on_bad):
                self.assertEqual(jobs.run_all()["auto_accepted"], 1)
        good.refresh_from_db()
        self.assertEqual(good.status, Status.SETTLED)

    def test_one_bad_order_fails_the_command_after_the_others_have_run(self):
        good = self.order_at("delivered")
        bad = self.order_at("delivered", amount="800000.00")
        self.overdue(good, "acceptance_deadline")
        self.overdue(bad, "acceptance_deadline")
        original = services.accept_order

        def explode_on_bad(actor, order):
            if order.pk == bad.pk:
                raise RuntimeError("boom")
            return original(actor, order)

        with self.assertLogs("marketplace.jobs", level="ERROR"):
            with mock.patch("marketplace.jobs.accept_order", explode_on_bad):
                with self.assertRaises(CommandError):
                    call_command("run_market_jobs", stdout=StringIO())
        good.refresh_from_db()
        self.assertEqual(good.status, Status.SETTLED)

    def test_a_clean_run_reports_no_failures(self):
        self.assertEqual(jobs.run_all()["failed"], 0)
        call_command("run_market_jobs", stdout=StringIO())


class NoMoneyLeftStrandedTests(MarketTestCase):
    """Whatever both sides fail to do, every order still reaches an end."""

    def run_jobs_after(self, order, **fields):
        Order.objects.filter(pk=order.pk).update(**fields)
        counts = jobs.run_all()
        order.refresh_from_db()
        return counts

    def test_goods_nobody_marks_delivered_are_settled_after_the_delivery_day(self):
        order = self.order_at("dispatched")
        self.assertIsNotNone(order.acceptance_deadline)
        counts = self.run_jobs_after(
            order, acceptance_deadline=timezone.now() - timedelta(minutes=1)
        )
        self.assertEqual(counts["auto_accepted"], 1)
        self.assertEqual(order.status, Status.SETTLED)

    def test_dispatched_goods_wait_until_the_delivery_day_is_over(self):
        order = self.order_at(
            "dispatched", delivery_date=str(self.today + timedelta(days=3))
        )
        self.assertEqual(jobs.run_all()["auto_accepted"], 0)
        day_after = order.acceptance_deadline.astimezone(ZoneInfo("Asia/Tashkent")).date()
        self.assertEqual(day_after, self.today + timedelta(days=4))

    @override_settings(ORDER_ACCEPTANCE_HOURS=6)
    def test_the_delivery_day_ends_at_tashkent_midnight(self):
        evening = datetime(2026, 3, 10, 18, 0, tzinfo=UTC)
        with mock.patch("django.utils.timezone.now", return_value=evening):
            deadline = services._deadline_after_dispatch(date(2026, 3, 11))
        self.assertEqual(deadline, datetime(2026, 3, 12, 1, 0, tzinfo=UTC))

    @override_settings(ORDER_CONFIRM_HOURS=6)
    def test_a_delivery_must_be_confirmed_before_its_tashkent_day_starts(self):
        # 01:00 on the 11th in Tashkent, still the 10th in UTC.
        evening = datetime(2026, 3, 10, 20, 0, tzinfo=UTC)
        with mock.patch("django.utils.timezone.now", return_value=evening):
            tomorrow = services.confirm_deadline(date(2026, 3, 12))
            today = services.confirm_deadline(date(2026, 3, 11))
        self.assertEqual(tomorrow, datetime(2026, 3, 11, 19, 0, tzinfo=UTC))
        self.assertEqual(today, evening + timedelta(hours=6))

    def test_pressing_delivered_early_cannot_rush_the_buyer(self):
        order = self.order_at(
            "dispatched", delivery_date=str(self.today + timedelta(days=7))
        )
        promised = order.acceptance_deadline
        self.step(order, "deliver")
        order.refresh_from_db()
        self.assertEqual(order.acceptance_deadline, promised)
        self.assertEqual(jobs.run_all()["auto_accepted"], 0)
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("0.00"))

    def test_a_late_delivery_still_gets_the_full_window(self):
        order = self.order_at("dispatched")
        Order.objects.filter(pk=order.pk).update(
            acceptance_deadline=timezone.now() - timedelta(days=1)
        )
        self.step(order, "deliver")
        order.refresh_from_db()
        self.assertGreater(
            order.acceptance_deadline, timezone.now() + timedelta(hours=5)
        )

    def test_goods_never_sent_are_refunded_against_the_seller(self):
        offer = self.make_offer(available_from=self.today - timedelta(days=5))
        order = self.order_at("paid", offer=offer)
        counts = self.run_jobs_after(order, delivery_date=self.today - timedelta(days=2))
        self.assertEqual(counts["refunded_undispatched"], 1)
        self.assertEqual(order.status, Status.CANCELLED)
        self.assertEqual(order.cancelled_by, Side.PROVIDER)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("800000.00"))
        offer.refresh_from_db()
        self.assertEqual(offer.quantity_available, Decimal("500.000"))

    def test_a_seller_has_until_the_day_after_delivery_to_send(self):
        order = self.order_at("paid")
        counts = self.run_jobs_after(order, delivery_date=self.today - timedelta(days=1))
        self.assertEqual(counts["refunded_undispatched"], 0)
        self.assertEqual(order.status, Status.PAID)


class SellerProfileTests(MarketTestCase):
    def test_a_produce_deal_shows_on_the_profile_with_no_listing(self):
        order = self.order_at("delivered")
        self.post(path(order, "accept"))
        rows = self.get(f"organizations/{self.seller.public_id}/deals", user=False).json()
        self.assertEqual(rows["count"], 1)
        self.assertIsNone(rows["items"][0]["listing"])
        profile = self.get(f"organizations/{self.seller.public_id}", user=False).json()
        self.assertEqual(profile["stats"]["completed_deals"], 1)


class ContactDetailsTests(MarketTestCase):
    def contacts(self, user):
        return self.get(f"organizations/{self.seller.public_id}/contacts", user)

    def test_contact_details_come_with_payment_and_not_before(self):
        order = self.order_at("awaiting_payment")
        self.assertEqual(self.contacts(self.buyer_owner).status_code, 403)
        self.step(order, "pay")
        self.assertEqual(self.contacts(self.buyer_owner).status_code, 200)

    def test_a_signed_contract_shares_contact_details(self):
        offer = self.make_offer()
        self.assertEqual(self.contacts(self.buyer_owner).status_code, 403)
        self.active_contract(offer=offer)
        self.assertEqual(self.contacts(self.buyer_owner).status_code, 200)
        self.assertEqual(self.contacts(self.outsider_owner).status_code, 403)


class ReliabilityTests(MarketTestCase):
    def test_a_seller_with_no_history(self):
        data = provider_reliability(self.seller)
        self.assertEqual(data["orders"], 0)
        self.assertIsNone(data["fill_rate"])

    def test_good_delivery_scores_well(self):
        order = self.order_at("delivered")
        self.post(path(order, "accept"))
        data = provider_reliability(self.seller)
        self.assertEqual(data["orders"], 1)
        self.assertEqual(data["fill_rate"], Decimal("1.0000"))
        self.assertEqual(data["on_time_rate"], Decimal("1.0000"))
        self.assertEqual(data["dispute_rate"], Decimal("0.0000"))

    def test_rejected_goods_lower_the_fill_rate(self):
        order = self.order_at("delivered")
        self.post(path(order, "reject"), {"accepted_quantity": "75.000", "reason": "quality"})
        self.post(path(order, "dispute/concede"), user=self.seller_owner)
        data = provider_reliability(self.seller)
        self.assertEqual(data["fill_rate"], Decimal("0.7500"))
        self.assertEqual(data["dispute_rate"], Decimal("1.0000"))

    def test_a_seller_who_lets_buyers_down_scores_badly(self):
        settled = self.order_at("delivered")
        self.post(path(settled, "accept"))
        dropped = self.order_at("awaiting_payment")
        self.post(path(dropped, "cancel"), user=self.seller_owner)
        data = provider_reliability(self.seller)
        self.assertEqual(data["orders"], 2)
        self.assertEqual(data["fill_rate"], Decimal("0.5000"))

    def test_orders_the_seller_never_confirmed_cost_nothing(self):
        settled = self.order_at("delivered")
        self.post(path(settled, "accept"))
        declined = self.order_at("pending_confirmation")
        self.post(path(declined, "decline"), user=self.seller_owner)
        services.expire_unconfirmed(self.order_at("pending_confirmation"))
        data = provider_reliability(self.seller)
        self.assertEqual(data["orders"], 1)
        self.assertEqual(data["fill_rate"], Decimal("1.0000"))

    def test_a_buyer_walking_away_is_not_the_sellers_fault(self):
        settled = self.order_at("delivered")
        self.post(path(settled, "accept"))
        cancelled = self.order_at("pending_confirmation")
        self.post(path(cancelled, "cancel"))
        data = provider_reliability(self.seller)
        self.assertEqual(data["orders"], 1)
        self.assertEqual(data["fill_rate"], Decimal("1.0000"))

    def test_the_score_is_public(self):
        response = self.get(
            f"market/organizations/{self.seller.public_id}/reliability", user=False
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["window_days"], 90)


class DatabaseGuardTests(MarketTestCase):
    def values(self, **overrides) -> dict:
        offer = self.make_offer()
        values = {
            "provider_organization": self.seller,
            "customer_organization": self.buyer,
            "offer": offer,
            "product": self.tomato,
            "unit": "kg",
            "unit_price": Decimal("10.00"),
            "ordered_quantity": Decimal("1.000"),
            "quantity": Decimal("1.000"),
            "subtotal": Decimal("10.00"),
            "delivery_method": "pickup",
            "delivery_date": self.today,
        }
        values.update(overrides)
        return values

    def refused(self, constraint, **overrides):
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                Order.objects.create(**self.values(**overrides))
        self.assertIn(constraint, str(caught.exception))

    def test_an_order_cannot_name_the_wrong_seller(self):
        self.refused(
            "order_offer_same_provider_fk", provider_organization=self.outsider
        )

    def test_an_organization_cannot_buy_from_itself(self):
        self.refused("order_not_own_org", customer_organization=self.seller)

    def test_more_cannot_be_accepted_than_was_ordered(self):
        self.refused("order_accepted_within_quantity", accepted_quantity=Decimal("2.000"))

    def test_stock_cannot_go_negative(self):
        offer = self.make_offer()
        offer.quantity_available = Decimal("-1.000")
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                offer.save()
        self.assertIn("offer_stock_not_negative", str(caught.exception))

    def test_a_dispute_cannot_refund_more_than_is_disputed(self):
        order = self.order_at("delivered")
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                OrderDispute.objects.create(
                    order=order,
                    reason="quality",
                    disputed_amount=Decimal("10.00"),
                    refund_amount=Decimal("11.00"),
                )
        self.assertIn("dispute_refund_within_amount", str(caught.exception))


@override_settings(BILLING_MOCK_ENABLED=True)
class ConcurrencyTests(TransactionTestCase):
    """Real transactions and threads: here the locks are what is under test."""

    def setUp(self):
        self.region = Region.objects.create(name="Race", code="RCE2", soato="1795")
        self.seller, self.seller_owner = make_org("race-seller", self.region)
        self.buyer, self.buyer_owner = make_org("race-buyer", self.region)
        category = ProductCategory.objects.create(name="Vegetables", slug="race-veg")
        self.product = Product.objects.create(category=category, name="Tomato", slug="race-tom")
        today = market_today()
        self.today = today
        self.offer = Offer.objects.create(
            organization=self.seller,
            product=self.product,
            title="Tomatoes",
            status=Offer.Status.ACTIVE,
            price=Decimal("100.00"),
            quantity_available=Decimal("50.000"),
            min_order_quantity=Decimal("1.000"),
            available_from=today,
            available_until=today + timedelta(days=3),
        )
        self.actor = services.Actor(self.buyer_owner, self.buyer, Side.CUSTOMER)
        self.seller_actor = services.Actor(self.seller_owner, self.seller, Side.PROVIDER)

    def attempt(self, *functions):
        def run(function):
            try:
                return function()
            except Exception as exc:
                return exc
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run, functions))

    def place(self):
        return services.place_order(
            self.actor,
            self.offer.public_id,
            quantity=Decimal("30.000"),
            delivery_date=self.today,
            delivery_method="pickup",
        )

    def test_two_buyers_cannot_take_the_same_stock(self):
        results = self.attempt(self.place, self.place, self.place)
        placed = [result for result in results if isinstance(result, Order)]
        self.assertEqual(len(placed), 1)
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.quantity_available, Decimal("20.000"))

    def delivered(self) -> Order:
        fund(self.buyer, "3000.00")
        order = self.place()
        services.confirm_order(self.seller_actor, order)
        services.pay_from_wallet(self.actor, order)
        services.dispatch_order(self.seller_actor, order)
        services.deliver_order(self.seller_actor, order)
        return order

    def test_the_buyer_and_the_job_cannot_both_settle(self):
        order = self.delivered()
        results = self.attempt(
            lambda: services.accept_order(self.actor, order),
            lambda: services.accept_order(services.SYSTEM, order),
        )
        self.assertEqual(len([r for r in results if isinstance(r, Order)]), 1)
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("2955.00"))
        self.assertEqual(reconcile(), [])

    def test_accepting_and_rejecting_cannot_both_land(self):
        order = self.delivered()
        results = self.attempt(
            lambda: services.accept_order(self.actor, order),
            lambda: services.reject_order(
                self.actor, order, accepted_quantity=Decimal("0"), reason="quality"
            ),
        )
        self.assertEqual(len([r for r in results if isinstance(r, Order)]), 1)
        self.assertEqual(reconcile(), [])

    def signed_contract(self):
        contract = contract_services.propose_contract(
            self.actor,
            offer=self.offer,
            terms={
                "unit_price": Decimal("100.00"),
                "quantity_per_delivery": Decimal("10.000"),
                "delivery_weekdays": [0, 1, 2, 3, 4, 5, 6],
                "start_date": self.today,
                "end_date": self.today + timedelta(days=30),
                "delivery_method": "pickup",
                "notice_days": 0,
            },
        )
        return contract_services.accept_terms(
            self.seller_actor, contract, contract_services.pending_terms(contract).version
        )

    def dispatched_deliveries(self, contract, count=2):
        fund(self.buyer, "10000.00")
        orders = list(contract.orders.order_by("delivery_date")[:count])
        for order in orders:
            services.confirm_order(self.seller_actor, order)
            services.dispatch_order(self.seller_actor, order)
        return orders

    def test_two_deliveries_settling_together_make_one_deal(self):
        first, second = self.dispatched_deliveries(self.signed_contract())
        results = self.attempt(
            lambda: services.accept_order(self.actor, first),
            lambda: services.accept_order(self.actor, second),
        )
        self.assertEqual(len([r for r in results if isinstance(r, Order)]), 2)
        self.assertEqual(Deal.objects.count(), 1)
        self.assertEqual(reconcile(), [])

    def test_settling_and_terminating_cannot_deadlock(self):
        contract = self.signed_contract()
        first, second = self.dispatched_deliveries(contract)
        results = self.attempt(
            lambda: services.accept_order(self.actor, first),
            lambda: contract_services.terminate_contract(self.actor, contract, "Closing"),
            lambda: services.accept_order(self.actor, second),
        )
        self.assertEqual(
            [r for r in results if isinstance(r, Exception)], []
        )
        self.assertEqual(reconcile(), [])

    def test_paying_and_cancelling_cannot_both_land(self):
        fund(self.buyer, "3000.00")
        order = self.place()
        services.confirm_order(self.seller_actor, order)
        self.attempt(
            lambda: services.pay_from_wallet(self.actor, order),
            lambda: services.cancel_order(self.seller_actor, order),
        )
        order.refresh_from_db()
        if order.status == Status.CANCELLED:
            self.assertEqual(billing.wallet_balance(self.buyer), Decimal("3000.00"))
        self.assertEqual(reconcile(), [])

