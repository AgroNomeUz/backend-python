"""
marketplace/test_contracts.py
Signing, schedules, changing terms, funding deliveries, and ending a contract.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from billing import services as billing
from billing.models import LedgerAccount
from core.models import ActivityLog
from deals.models import Deal
from marketplace import contract_services as services
from marketplace import jobs
from marketplace.models import ContractTerms, Order, Product, Quote, Side, SupplyContract
from marketplace.reliability import provider_reliability
from users.models import OrgPermission, Organization

from .test_support import MarketTestCase, fund

Status = Order.Status
MONDAY = date(2026, 1, 5)


def contract_path(contract, action="") -> str:
    return f"market/contracts/{contract.public_id}" + (f"/{action}" if action else "")


def order_path(order, action="") -> str:
    return f"market/orders/{order.public_id}" + (f"/{action}" if action else "")


class ScheduleTests(MarketTestCase):
    def test_weekly(self):
        self.assertTrue(services.is_delivery_day([0], 1, MONDAY, MONDAY))
        self.assertFalse(services.is_delivery_day([0], 1, MONDAY, MONDAY + timedelta(days=1)))
        self.assertTrue(services.is_delivery_day([0], 1, MONDAY, MONDAY + timedelta(days=7)))

    def test_fortnightly_skips_the_week_between(self):
        self.assertTrue(services.is_delivery_day([2], 2, MONDAY, MONDAY + timedelta(days=2)))
        self.assertFalse(services.is_delivery_day([2], 2, MONDAY, MONDAY + timedelta(days=9)))
        self.assertTrue(services.is_delivery_day([2], 2, MONDAY, MONDAY + timedelta(days=16)))

    def test_fortnightly_counts_from_the_starting_week_not_the_starting_day(self):
        friday = MONDAY + timedelta(days=4)
        next_monday = MONDAY + timedelta(days=7)
        self.assertFalse(services.is_delivery_day([0], 2, friday, next_monday))
        self.assertTrue(services.is_delivery_day([0], 2, friday, next_monday + timedelta(days=7)))

    def test_planned_dates_respect_the_end_date(self):
        contract = self.active_contract(end_date=str(self.today + timedelta(days=1)))
        planned = list(
            services.planned_dates(contract, today=self.today, horizon_days=10)
        )
        self.assertEqual(
            [day for day, _ in planned], [self.today, self.today + timedelta(days=1)]
        )


class ProposingTests(MarketTestCase):
    def propose(self, offer=None, user=None, **overrides):
        offer = offer or self.make_offer()
        body = {"offer_id": str(offer.public_id), **self.contract_terms(**overrides)}
        return self.post("market/contracts", body, user)

    def test_proposing_binds_nobody_yet(self):
        response = self.propose()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["status"], "proposed")
        self.assertIsNone(body["current_terms"])
        self.assertEqual(body["pending_terms"]["proposed_by_side"], "customer")
        self.assertEqual(Decimal(body["pending_terms"]["unit_price"]), Decimal("8000.00"))
        self.assertFalse(Order.objects.exists())

    def test_the_buyer_can_propose_their_own_price(self):
        body = self.propose(unit_price="7000.00").json()
        self.assertEqual(Decimal(body["pending_terms"]["unit_price"]), Decimal("7000.00"))

    def test_contract_terms_come_from_the_offer(self):
        offer = self.make_offer(variety="Pink", grade="b")
        contract = SupplyContract.objects.get(public_id=self.propose(offer).json()["id"])
        self.assertEqual(contract.provider_organization, self.seller)
        self.assertEqual(contract.customer_organization, self.buyer)
        self.assertEqual((contract.variety, contract.grade, contract.unit), ("Pink", "b", "kg"))

    def test_it_needs_an_offer_or_a_quote(self):
        response = self.post("market/contracts", self.contract_terms(unit_price="1.00"))
        self.assertEqual(response.status_code, 400)

    def test_you_cannot_contract_with_yourself(self):
        self.assertEqual(self.propose(user=self.seller_owner).status_code, 400)

    def test_bad_schedules_are_refused(self):
        self.assertEqual(self.propose(delivery_weekdays=[]).status_code, 422)
        self.assertEqual(self.propose(delivery_weekdays=[7]).status_code, 422)
        self.assertEqual(self.propose(every_n_weeks=0).status_code, 422)
        self.assertEqual(self.propose(tolerance_percent="60").status_code, 422)

    def test_dates_must_make_sense(self):
        past = str(self.today - timedelta(days=1))
        self.assertEqual(self.propose(start_date=past).status_code, 400)
        backwards = str(self.today - timedelta(days=1))
        self.assertEqual(self.propose(end_date=backwards).status_code, 400)

    def test_a_schedule_with_no_deliveries_is_refused(self):
        tomorrow = self.today + timedelta(days=1)
        other_day = (tomorrow.weekday() + 3) % 7
        response = self.propose(
            start_date=str(tomorrow),
            end_date=str(tomorrow),
            delivery_weekdays=[other_day],
        )
        self.assertEqual(response.status_code, 400)

    def test_a_delivery_contract_needs_an_address(self):
        self.assertEqual(self.propose(delivery_address="  ").status_code, 400)

    def test_signing_needs_the_contract_permission(self):
        market = self.member(OrgPermission.MANAGE_MARKET)
        self.assertEqual(self.propose(user=market).status_code, 403)
        contracts = self.member(OrgPermission.MANAGE_CONTRACTS)
        self.assertEqual(self.propose(user=contracts).status_code, 201)

    def test_a_contract_from_a_quote(self):
        request = self.post(
            "market/requests",
            {
                "product_id": str(self.tomato.public_id),
                "quantity": "200.000",
                "needed_from": str(self.today),
                "needed_until": str(self.today + timedelta(days=90)),
                "is_recurring": True,
            },
        ).json()
        quote = self.post(
            f"market/requests/{request['id']}/quotes",
            {
                "unit_price": "7000.00",
                "quantity": "200.000",
                "valid_until": str(self.today + timedelta(days=5)),
                "grade": "a",
            },
            self.seller_owner,
        ).json()
        response = self.post(
            "market/contracts", {"quote_id": quote["id"], **self.contract_terms()}
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(Decimal(body["pending_terms"]["unit_price"]), Decimal("7000.00"))
        self.assertEqual(body["provider"]["id"], str(self.seller.public_id))

    def test_a_contract_for_regulated_goods_needs_a_seller_still_verified(self):
        beef = Product.objects.create(
            category=self.vegetables, name="Beef", slug="beef", is_regulated=True
        )
        offer = self.make_offer(product=beef)
        request = self.post(
            "market/requests",
            {
                "product_id": str(beef.public_id),
                "quantity": "200.000",
                "needed_from": str(self.today),
                "needed_until": str(self.today + timedelta(days=90)),
                "is_recurring": True,
            },
        ).json()
        quote = self.post(
            f"market/requests/{request['id']}/quotes",
            {
                "unit_price": "7000.00",
                "quantity": "200.000",
                "valid_until": str(self.today + timedelta(days=5)),
                "grade": "a",
            },
            self.seller_owner,
        ).json()
        Organization.objects.filter(pk=self.seller.pk).update(is_verified=False)

        from_quote = self.post(
            "market/contracts", {"quote_id": quote["id"], **self.contract_terms()}
        )
        self.assertEqual(from_quote.status_code, 403)
        self.assertEqual(Quote.objects.get(public_id=quote["id"]).status, "submitted")
        self.assertEqual(self.propose(offer=offer).status_code, 404)

    def test_another_buyers_quote_is_a_404(self):
        request = self.post(
            "market/requests",
            {
                "product_id": str(self.tomato.public_id),
                "quantity": "1.000",
                "needed_from": str(self.today),
                "needed_until": str(self.today + timedelta(days=9)),
            },
            self.outsider_owner,
        ).json()
        quote = self.post(
            f"market/requests/{request['id']}/quotes",
            {
                "unit_price": "1.00",
                "quantity": "1.000",
                "valid_until": str(self.today + timedelta(days=5)),
            },
            self.seller_owner,
        ).json()
        response = self.post(
            "market/contracts", {"quote_id": quote["id"], **self.contract_terms()}
        )
        self.assertEqual(response.status_code, 404)


class SigningTests(MarketTestCase):
    def proposed(self) -> SupplyContract:
        offer = self.make_offer()
        body = {"offer_id": str(offer.public_id), **self.contract_terms()}
        return SupplyContract.objects.get(
            public_id=self.post("market/contracts", body).json()["id"]
        )

    def test_the_other_side_accepts_and_deliveries_appear(self):
        contract = self.proposed()
        body = self.post(contract_path(contract, "accept"), user=self.seller_owner).json()
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["current_terms"]["version"], 1)
        self.assertIsNone(body["pending_terms"])
        self.assertEqual(contract.orders.count(), 4)
        order = contract.orders.order_by("delivery_date").first()
        self.assertEqual(order.status, Status.PENDING_CONFIRMATION)
        self.assertEqual(order.subtotal, Decimal("1600000.00"))
        self.assertEqual(order.events.get().side, Side.PLATFORM)

    def test_you_cannot_sign_your_own_proposal(self):
        contract = self.proposed()
        self.assertEqual(self.post(contract_path(contract, "accept")).status_code, 403)
        self.assertEqual(self.post(contract_path(contract, "reject")).status_code, 403)
        contract.refresh_from_db()
        self.assertEqual(contract.status, SupplyContract.Status.PROPOSED)

    def test_an_outsider_cannot_touch_it(self):
        contract = self.proposed()
        path = contract_path(contract, "accept")
        self.assertEqual(self.post(path, user=self.outsider_owner).status_code, 404)
        self.assertEqual(self.get(contract_path(contract), self.outsider_owner).status_code, 404)

    def test_rejecting_ends_a_proposal(self):
        contract = self.proposed()
        body = self.post(contract_path(contract, "reject"), user=self.seller_owner).json()
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(
            self.post(contract_path(contract, "accept"), user=self.seller_owner).status_code,
            409,
        )

    def test_a_counter_offer_passes_the_decision_back(self):
        contract = self.proposed()
        counter = self.post(
            contract_path(contract, "amend"),
            self.contract_terms(unit_price="9000.00"),
            self.seller_owner,
        ).json()
        self.assertEqual(counter["pending_terms"]["version"], 2)
        self.assertEqual(counter["pending_terms"]["proposed_by_side"], "provider")
        self.assertEqual(
            self.post(contract_path(contract, "accept"), user=self.seller_owner).status_code,
            403,
        )
        body = self.post(contract_path(contract, "accept")).json()
        self.assertEqual(body["status"], "active")
        self.assertEqual(Decimal(body["current_terms"]["unit_price"]), Decimal("9000.00"))
        statuses = {row["version"]: row["status"] for row in body and self.get(contract_path(contract)).json()["versions"]}
        self.assertEqual(statuses, {1: "superseded", 2: "accepted"})

    def test_signing_needs_the_permission(self):
        contract = self.proposed()
        market = self.member(OrgPermission.MANAGE_MARKET, org=self.seller)
        response = self.post(contract_path(contract, "accept"), user=market)
        self.assertEqual(response.status_code, 403)
        signer = self.member(OrgPermission.MANAGE_CONTRACTS, org=self.seller)
        response = self.post(contract_path(contract, "accept"), user=signer)
        self.assertEqual(response.status_code, 200)

    def test_signing_is_audited_for_the_signer(self):
        contract = self.proposed()
        self.post(contract_path(contract, "accept"), user=self.seller_owner)
        row = ActivityLog.objects.get(object_id=contract.pk, action="status_changed")
        self.assertEqual(row.organization, self.seller)
        self.assertEqual(row.actor, self.seller_owner)
        self.assertEqual(row.changes["status"]["to"], "active")

    def test_reading_needs_the_contract_view_code(self):
        contract = self.proposed()
        market = self.member(OrgPermission.VIEW_MARKET)
        self.assertEqual(self.get(contract_path(contract), market).status_code, 403)
        viewer = self.member(OrgPermission.VIEW_CONTRACTS)
        self.assertEqual(self.get(contract_path(contract), viewer).status_code, 200)

    def test_listing(self):
        self.proposed()
        self.assertEqual(self.get("market/contracts").json()["count"], 1)
        self.assertEqual(self.get("market/contracts", self.seller_owner).json()["count"], 1)
        self.assertEqual(self.get("market/contracts", self.outsider_owner).json()["count"], 0)
        self.assertEqual(self.get("market/contracts?status=active").json()["count"], 0)


class GeneratingTests(MarketTestCase):
    def test_generating_again_creates_nothing_new(self):
        contract = self.active_contract()
        count = contract.orders.count()
        self.assertEqual(jobs.run_all()["contract_orders"], 0)
        self.assertEqual(contract.orders.count(), count)

    def test_a_contract_that_cannot_generate_counts_as_a_failure(self):
        self.active_contract()
        with self.assertLogs("marketplace.contract_services", level="ERROR"):
            with mock.patch(
                "marketplace.contract_services.generate_orders",
                side_effect=RuntimeError("boom"),
            ):
                self.assertEqual(jobs.run_all()["failed"], 1)

    def test_a_contract_that_cannot_sync_counts_as_a_failure(self):
        self.active_contract()
        with self.assertLogs("marketplace.contract_services", level="ERROR"):
            with mock.patch(
                "marketplace.contract_services.sync_orders_with_terms",
                side_effect=RuntimeError("boom"),
            ):
                self.assertEqual(jobs.run_all()["failed"], 1)

    def test_the_horizon_moves_forward(self):
        contract = self.active_contract()
        with override_settings(CONTRACT_GENERATION_HORIZON_DAYS=6):
            self.assertEqual(jobs.run_all()["contract_orders"], 3)
        self.assertEqual(contract.orders.count(), 7)

    def test_a_cancelled_delivery_stays_cancelled(self):
        contract = self.active_contract()
        order = contract.orders.order_by("delivery_date").first()
        self.post(order_path(order, "cancel"), {"reason": "Factory closed that day"})
        jobs.run_all()
        self.assertEqual(contract.orders.filter(delivery_date=order.delivery_date).count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, Status.CANCELLED)

    def test_a_contract_draws_nothing_from_the_offers_stock(self):
        offer = self.make_offer()
        contract = self.active_contract(offer=offer)
        order = contract.orders.first()
        self.assertIsNone(order.offer)
        offer.refresh_from_db()
        self.assertEqual(offer.quantity_available, Decimal("500.000"))

    def test_listing_a_contracts_deliveries(self):
        contract = self.active_contract()
        rows = self.get(contract_path(contract, "orders")).json()
        self.assertEqual(rows["count"], 4)
        dates = [row["delivery_date"] for row in rows["items"]]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(rows["items"][0]["contract_id"], str(contract.public_id))
        filtered = self.get(f"market/orders?contract_id={contract.public_id}").json()
        self.assertEqual(filtered["count"], 4)

    def test_the_database_refuses_a_second_delivery_on_a_date(self):
        contract = self.active_contract()
        order = contract.orders.first()
        order.pk = None
        order.public_id = None
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                Order.objects.bulk_create(
                    [
                        Order(
                            provider_organization=contract.provider_organization,
                            customer_organization=contract.customer_organization,
                            contract=contract,
                            product=contract.product,
                            unit="kg",
                            unit_price=Decimal("1.00"),
                            ordered_quantity=Decimal("1.000"),
                            quantity=Decimal("1.000"),
                            subtotal=Decimal("1.00"),
                            delivery_method="pickup",
                            delivery_date=order.delivery_date,
                        )
                    ]
                )
        self.assertIn("order_one_per_contract_date", str(caught.exception))

    def test_the_database_refuses_an_order_outside_the_contracts_parties(self):
        contract = self.active_contract()
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                Order.objects.create(
                    provider_organization=self.outsider,
                    customer_organization=self.buyer,
                    contract=contract,
                    product=contract.product,
                    unit="kg",
                    unit_price=Decimal("1.00"),
                    ordered_quantity=Decimal("1.000"),
                    quantity=Decimal("1.000"),
                    subtotal=Decimal("1.00"),
                    delivery_method="pickup",
                    delivery_date=self.today + timedelta(days=50),
                )
        self.assertIn("order_contract_same_provider_fk", str(caught.exception))

    def test_a_delivery_nobody_confirms_expires_without_touching_the_score(self):
        contract = self.active_contract()
        contract.orders.update(confirm_by=timezone.now() - timedelta(minutes=1))
        jobs.run_all()
        order = contract.orders.order_by("delivery_date").first()
        self.assertEqual(order.status, Status.EXPIRED)
        self.assertEqual(order.cancelled_by, Side.PROVIDER)
        self.assertEqual(provider_reliability(self.seller)["orders"], 0)


class FundingTests(MarketTestCase):
    def first_delivery(self, **overrides) -> Order:
        contract = self.active_contract(**overrides)
        return contract.orders.order_by("delivery_date").first()

    def confirm(self, order, **data):
        return self.post(order_path(order, "confirm"), data, self.seller_owner)

    def test_confirming_a_delivery_charges_the_wallet(self):
        fund(self.buyer, "2000000.00")
        order = self.first_delivery()
        body = self.confirm(order).json()
        self.assertEqual(body["status"], "paid")
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("400000.00"))
        self.assertEqual(billing.held_for(self.buyer), Decimal("1600000.00"))

    def test_a_short_wallet_leaves_the_delivery_waiting(self):
        fund(self.buyer, "100.00")
        order = self.first_delivery()
        body = self.confirm(order).json()
        self.assertEqual(body["status"], "awaiting_payment")
        self.assertIsNotNone(body["pay_by"])
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("100.00"))

    def test_a_top_up_in_time_keeps_the_delivery(self):
        order = self.first_delivery()
        self.confirm(order)
        fund(self.buyer, "1600000.00")
        self.assertEqual(jobs.run_all()["contract_retries"], 1)
        order.refresh_from_db()
        self.assertEqual(order.status, Status.PAID)

    def test_no_top_up_in_time_loses_the_delivery_as_the_buyers_doing(self):
        order = self.first_delivery()
        self.confirm(order)
        Order.objects.filter(pk=order.pk).update(pay_by=timezone.now() - timedelta(minutes=1))
        jobs.run_all()
        order.refresh_from_db()
        self.assertEqual(order.status, Status.EXPIRED)
        self.assertEqual(order.cancelled_by, Side.CUSTOMER)

    def test_a_contract_delivery_pays_the_lower_commission(self):
        fund(self.buyer, "1600000.00")
        order = self.first_delivery()
        self.confirm(order)
        self.post(order_path(order, "dispatch"), user=self.seller_owner)
        self.post(order_path(order, "deliver"), user=self.seller_owner)
        self.post(order_path(order, "accept"))
        commission = billing.platform_account(LedgerAccount.Kind.COMMISSION)
        self.assertEqual(billing.balance_of(commission), Decimal("16000.00"))
        self.assertEqual(billing.wallet_balance(self.seller), Decimal("1584000.00"))

    def test_a_contract_earns_one_deal_however_many_deliveries_settle(self):
        fund(self.buyer, "3200000.00")
        contract = self.active_contract()
        for order in contract.orders.order_by("delivery_date")[:2]:
            self.confirm(order)
            self.post(order_path(order, "dispatch"), user=self.seller_owner)
            self.post(order_path(order, "accept"))
        self.assertEqual(Deal.objects.count(), 1)
        contract.refresh_from_db()
        self.assertIsNotNone(contract.deal)
        self.assertEqual(contract.orders.filter(deal__isnull=False).count(), 0)

    def test_the_seller_can_adjust_within_the_tolerance(self):
        fund(self.buyer, "2000000.00")
        order = self.first_delivery(tolerance_percent="10")
        body = self.confirm(order, quantity="185.000").json()
        self.assertEqual(Decimal(body["quantity"]), Decimal("185.000"))
        self.assertEqual(Decimal(body["ordered_quantity"]), Decimal("200.000"))
        self.assertEqual(Decimal(body["subtotal"]), Decimal("1480000.00"))
        self.assertEqual(billing.held_for(self.buyer), Decimal("1480000.00"))

    def test_the_seller_cannot_go_beyond_the_tolerance(self):
        order = self.first_delivery(tolerance_percent="10")
        self.assertEqual(self.confirm(order, quantity="179.000").status_code, 400)
        self.assertEqual(self.confirm(order, quantity="221.000").status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.quantity, Decimal("200.000"))

    def test_with_no_tolerance_the_quantity_is_the_quantity(self):
        order = self.first_delivery()
        self.assertEqual(self.confirm(order, quantity="199.000").status_code, 400)


class AmendingTests(MarketTestCase):
    def amend(self, contract, user=None, **overrides):
        terms = self.contract_terms(
            unit_price="9000.00",
            start_date=str(self.today + timedelta(days=10)),
            **overrides,
        )
        return self.post(contract_path(contract, "amend"), terms, user or self.seller_owner)

    def test_a_change_needs_the_other_side(self):
        contract = self.active_contract()
        body = self.amend(contract).json()
        self.assertEqual(Decimal(body["current_terms"]["unit_price"]), Decimal("8000.00"))
        self.assertEqual(Decimal(body["pending_terms"]["unit_price"]), Decimal("9000.00"))
        response = self.post(contract_path(contract, "accept"), user=self.seller_owner)
        self.assertEqual(response.status_code, 403)

    def test_the_new_price_applies_from_its_date_only(self):
        contract = self.active_contract()
        self.amend(contract)
        self.post(contract_path(contract, "accept"))
        with override_settings(CONTRACT_GENERATION_HORIZON_DAYS=12):
            services.generate_orders(SupplyContract.objects.get(pk=contract.pk))
        before = contract.orders.get(delivery_date=self.today + timedelta(days=9))
        after = contract.orders.get(delivery_date=self.today + timedelta(days=10))
        self.assertEqual(before.unit_price, Decimal("8000.00"))
        self.assertEqual(after.unit_price, Decimal("9000.00"))
        self.assertEqual(after.contract_terms.version, 2)

    def change_from_tomorrow(self, contract, **overrides):
        terms = {
            "unit_price": "9000.00",
            "start_date": str(self.today + timedelta(days=1)),
            **overrides,
        }
        self.post(
            contract_path(contract, "amend"), self.contract_terms(**terms), self.seller_owner
        )
        return self.post(contract_path(contract, "accept"))

    def delivery(self, contract, days):
        return contract.orders.get(delivery_date=self.today + timedelta(days=days))

    def test_unconfirmed_deliveries_take_the_new_terms(self):
        contract = self.active_contract()
        self.change_from_tomorrow(contract, quantity_per_delivery="100.000")
        today_order = self.delivery(contract, 0)
        self.assertEqual(today_order.unit_price, Decimal("8000.00"))
        later = self.delivery(contract, 2)
        self.assertEqual(later.unit_price, Decimal("9000.00"))
        self.assertEqual(later.quantity, Decimal("100.000"))
        self.assertEqual(later.subtotal, Decimal("900000.00"))
        self.assertEqual(later.contract_terms.version, 2)
        self.assertEqual(later.events.last().note, "Updated to version 2.")

    def test_a_confirmed_delivery_keeps_the_price_both_sides_committed_to(self):
        fund(self.buyer, "1600000.00")
        contract = self.active_contract()
        confirmed = self.delivery(contract, 2)
        self.post(order_path(confirmed, "confirm"), user=self.seller_owner)
        self.change_from_tomorrow(contract)
        confirmed.refresh_from_db()
        self.assertEqual(confirmed.status, Status.PAID)
        self.assertEqual(confirmed.unit_price, Decimal("8000.00"))
        self.assertEqual(confirmed.hold.amount, Decimal("1600000.00"))

    def test_a_delivery_dropped_from_the_schedule_is_called_off(self):
        contract = self.active_contract()
        dropped = self.delivery(contract, 2)
        kept = self.delivery(contract, 3)
        self.change_from_tomorrow(contract, delivery_weekdays=[kept.delivery_date.weekday()])
        dropped.refresh_from_db()
        kept.refresh_from_db()
        self.assertEqual(dropped.status, Status.CANCELLED)
        self.assertEqual(dropped.cancelled_by, Side.PLATFORM)
        self.assertEqual(kept.status, Status.PENDING_CONFIRMATION)

    def test_a_shortened_contract_refunds_a_paid_delivery_past_its_end(self):
        fund(self.buyer, "1600000.00")
        contract = self.active_contract()
        late = self.delivery(contract, 3)
        self.post(order_path(late, "confirm"), user=self.seller_owner)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("0.00"))
        self.change_from_tomorrow(contract, end_date=str(self.today + timedelta(days=1)))
        late.refresh_from_db()
        self.assertEqual(late.status, Status.CANCELLED)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("1600000.00"))

    def test_goods_already_on_the_road_are_left_alone(self):
        fund(self.buyer, "1600000.00")
        contract = self.active_contract()
        late = self.delivery(contract, 3)
        self.post(order_path(late, "confirm"), user=self.seller_owner)
        self.post(order_path(late, "dispatch"), user=self.seller_owner)
        self.change_from_tomorrow(contract, end_date=str(self.today + timedelta(days=1)))
        late.refresh_from_db()
        self.assertEqual(late.status, Status.DISPATCHED)

    def test_the_job_finishes_a_change_interrupted_by_a_crash(self):
        contract = self.active_contract()
        with mock.patch(
            "marketplace.contract_services.sync_orders_with_terms", return_value=0
        ):
            self.change_from_tomorrow(contract)
        stale = self.delivery(contract, 2)
        self.assertEqual(stale.unit_price, Decimal("8000.00"))
        self.assertEqual(jobs.run_all()["contract_orders_synced"], 3)
        stale.refresh_from_db()
        self.assertEqual(stale.unit_price, Decimal("9000.00"))
        self.assertEqual(jobs.run_all()["contract_orders_synced"], 0)

    def test_rejecting_a_change_leaves_the_contract_as_it_was(self):
        contract = self.active_contract()
        self.amend(contract)
        body = self.post(contract_path(contract, "reject")).json()
        self.assertEqual(body["status"], "active")
        self.assertEqual(Decimal(body["current_terms"]["unit_price"]), Decimal("8000.00"))
        self.assertIsNone(body["pending_terms"])

    def test_a_change_cannot_reach_back_over_today(self):
        contract = self.active_contract()
        response = self.post(
            contract_path(contract, "amend"),
            self.contract_terms(unit_price="9000.00", start_date=str(self.today)),
            self.seller_owner,
        )
        self.assertEqual(response.status_code, 400)

    def test_a_new_proposal_replaces_the_one_waiting(self):
        contract = self.active_contract()
        self.amend(contract)
        body = self.amend(contract, tolerance_percent="5").json()
        self.assertEqual(body["pending_terms"]["version"], 3)
        self.assertEqual(ContractTerms.objects.filter(contract=contract, status="proposed").count(), 1)
        self.assertTrue(ContractTerms.objects.filter(contract=contract, version=2, status="superseded").exists())

    def test_a_shorter_contract_really_is_shorter(self):
        contract = self.active_contract()
        last_day = self.today + timedelta(days=12)
        self.amend(contract, end_date=str(last_day))
        self.post(contract_path(contract, "accept"))
        contract.refresh_from_db()
        planned = list(services.planned_dates(contract, today=self.today, horizon_days=60))
        self.assertEqual(planned[-1][0], last_day)

    def test_every_version_stays_readable(self):
        contract = self.active_contract()
        self.amend(contract)
        versions = self.get(contract_path(contract)).json()["versions"]
        self.assertEqual([row["version"] for row in versions], [2, 1])

    def test_a_finished_contract_cannot_be_changed(self):
        contract = self.active_contract()
        contract.status = SupplyContract.Status.COMPLETED
        contract.save(update_fields=["status"])
        self.assertEqual(self.amend(contract).status_code, 409)


class TerminatingTests(MarketTestCase):
    def terminate(self, contract, user=None, reason="Closing the line"):
        return self.post(
            contract_path(contract, "terminate"), {"reason": reason}, user or self.buyer_owner
        )

    def test_notice_keeps_the_near_deliveries_and_stops_the_far_ones(self):
        contract = self.active_contract(notice_days=1)
        body = self.terminate(contract).json()
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["terminated_by"], "customer")
        last_day = self.today + timedelta(days=1)
        self.assertEqual(body["termination_date"], str(last_day))

        kept = contract.orders.filter(delivery_date__lte=last_day)
        self.assertEqual(kept.exclude(status=Status.PENDING_CONFIRMATION).count(), 0)
        dropped = contract.orders.filter(delivery_date__gt=last_day)
        self.assertGreater(dropped.count(), 0)
        self.assertEqual(dropped.exclude(status=Status.CANCELLED).count(), 0)

    def test_paid_deliveries_past_the_notice_are_refunded(self):
        fund(self.buyer, "1600000.00")
        contract = self.active_contract(notice_days=0)
        later = contract.orders.order_by("delivery_date").last()
        self.post(order_path(later, "confirm"), user=self.seller_owner)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("0.00"))
        self.terminate(contract)
        later.refresh_from_db()
        self.assertEqual(later.status, Status.CANCELLED)
        self.assertEqual(billing.wallet_balance(self.buyer), Decimal("1600000.00"))

    def test_no_deliveries_are_generated_past_the_notice(self):
        contract = self.active_contract(notice_days=1)
        self.terminate(contract)
        with override_settings(CONTRACT_GENERATION_HORIZON_DAYS=30):
            self.assertEqual(jobs.run_all()["contract_orders"], 0)

    def test_giving_notice_properly_is_not_a_failure_to_deliver(self):
        contract = self.active_contract(notice_days=1)
        self.terminate(contract, self.seller_owner)
        self.assertEqual(provider_reliability(self.seller)["orders"], 0)

    def test_with_no_notice_period_it_ends_at_once(self):
        contract = self.active_contract(notice_days=0)
        body = self.terminate(contract).json()
        self.assertEqual(body["status"], "terminated")

    def test_the_contract_closes_when_the_notice_runs_out(self):
        contract = self.active_contract(notice_days=3)
        self.terminate(contract)
        SupplyContract.objects.filter(pk=contract.pk).update(
            termination_date=self.today - timedelta(days=1)
        )
        self.assertEqual(jobs.run_all()["contracts_closed"], 1)
        contract.refresh_from_db()
        self.assertEqual(contract.status, SupplyContract.Status.TERMINATED)

    def test_a_contract_that_runs_its_course_completes(self):
        contract = self.active_contract()
        ContractTerms.objects.filter(contract=contract).update(
            start_date=self.today - timedelta(days=30),
            end_date=self.today - timedelta(days=1),
        )
        self.assertEqual(jobs.run_all()["contracts_closed"], 1)
        contract.refresh_from_db()
        self.assertEqual(contract.status, SupplyContract.Status.COMPLETED)

    def test_notice_can_only_be_given_once(self):
        contract = self.active_contract()
        self.terminate(contract)
        self.assertEqual(self.terminate(contract).status_code, 409)

    def test_a_proposal_cannot_be_terminated(self):
        offer = self.make_offer()
        body = {"offer_id": str(offer.public_id), **self.contract_terms()}
        contract = SupplyContract.objects.get(
            public_id=self.post("market/contracts", body).json()["id"]
        )
        self.assertEqual(self.terminate(contract).status_code, 409)

    def test_a_contract_giving_notice_cannot_be_amended(self):
        contract = self.active_contract()
        self.terminate(contract)
        response = self.post(
            contract_path(contract, "amend"),
            self.contract_terms(
                unit_price="9000.00", start_date=str(self.today + timedelta(days=1))
            ),
            self.seller_owner,
        )
        self.assertEqual(response.status_code, 409)

    def test_a_reason_is_required(self):
        contract = self.active_contract()
        self.assertEqual(self.terminate(contract, reason="  ").status_code, 422)

    def test_terminating_needs_the_permission(self):
        contract = self.active_contract()
        market = self.member(OrgPermission.MANAGE_MARKET)
        self.assertEqual(self.terminate(contract, market).status_code, 403)

    def test_an_outsider_cannot_terminate(self):
        contract = self.active_contract()
        self.assertEqual(self.terminate(contract, self.outsider_owner).status_code, 404)
