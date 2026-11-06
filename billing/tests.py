"""
billing/tests.py
The ledger's guarantees, escrow arithmetic, provider events and payouts.

Most of what can go wrong with money is a second, quiet effect, so these tests
replay and race things and then look at the balances. Every test ends by asking
`reconcile()` whether the books still agree, through `tearDown`.
"""

import json
import os
import runpy
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from core.test_support import auth
from billing import gateway as gateways
from billing import services
from billing.models import (
    EscrowHold,
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
    Payment,
    Payout,
    PayoutDestination,
    WebhookEvent,
)
from billing.reconciliation import _holds_match_entries, needs_a_person, reconcile
from core.models import ActivityLog
from users.models import OrgPermission, Organization, Region, User

PHONE = "+998901112233"


def make_org(slug: str, region: Region, phone: str | None = None) -> tuple[Organization, User]:
    owner = User.objects.create_user(username=f"{slug}-owner", password="x", phone=phone)
    org = Organization.objects.create(name=f"{slug} org", owner=owner, region=region)
    owner.organization = org
    owner.save(update_fields=["organization"])
    return org, owner


def deposit(org: Organization, amount: str) -> Payment:
    """Put money in a wallet the way a real payment does."""
    payment = services.start_payment(
        organization=org, amount=Decimal(amount), purpose=Payment.Purpose.TOP_UP
    )
    send_event(gateways.PAYMENT_SUCCEEDED, payment.provider_reference, payment.amount)
    payment.refresh_from_db()
    return payment


def send_event(event_type, reference, amount, event_id=None, reason=""):
    event = gateways.GatewayEvent(
        event_id=event_id or uuid.uuid4().hex,
        event_type=event_type,
        provider_reference=reference,
        amount=Decimal(amount),
        reason=reason,
    )
    return services.process_event("mock", event)


@override_settings(BILLING_MOCK_ENABLED=True)
class BillingTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.region = Region.objects.create(name="Billing Region", code="BLR", soato="1798")
        cls.buyer, cls.buyer_owner = make_org("buyer", cls.region, PHONE)
        cls.seller, cls.seller_owner = make_org("seller", cls.region)
        cls.outsider, cls.outsider_owner = make_org("outsider", cls.region)

    def tearDown(self):
        self.assertEqual(reconcile(), [])

    def hold(self, amount="1000.00", rate="0.0150", payer=None, payee=None):
        return services.create_hold(
            payer=payer or self.buyer,
            payee=payee or self.seller,
            amount=Decimal(amount),
            commission_rate=Decimal(rate),
            reference_type="test.thing",
            reference_id=uuid.uuid4(),
        )

    def post(self, path, data=None, user=None):
        return self.client.post(
            f"/api/v1/billing/{path}",
            data=json.dumps(data or {}),
            content_type="application/json",
            **auth(user or self.buyer_owner),
        )

    def get(self, path, user=None):
        return self.client.get(f"/api/v1/billing/{path}", **auth(user or self.buyer_owner))


class LedgerInvariantTests(BillingTestCase):
    def test_a_deposit_is_balanced_and_shows_on_the_wallet(self):
        deposit(self.buyer, "5000.00")
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("5000.00"))
        gateway_account = services.platform_account(LedgerAccount.Kind.GATEWAY)
        self.assertEqual(services.balance_of(gateway_account), Decimal("-5000.00"))

    def _unbalanced(self):
        ledger_transaction = LedgerTransaction.objects.create(
            kind=LedgerTransaction.Kind.DEPOSIT, idempotency_key="unbalanced"
        )
        LedgerEntry.objects.create(
            transaction=ledger_transaction,
            account=services.wallet_for(self.buyer),
            amount=Decimal("10.00"),
        )

    def test_the_database_refuses_an_unbalanced_transaction(self):
        with self.assertRaises(DatabaseError) as caught:
            with transaction.atomic():
                self._unbalanced()
                connection.check_constraints()
        self.assertIn("unbalanced", str(caught.exception))

    def test_the_helper_refuses_an_unbalanced_transaction_before_the_database(self):
        with self.assertRaises(ValueError):
            services._post(
                LedgerTransaction.Kind.DEPOSIT,
                [(services.wallet_for(self.buyer), Decimal("10"))],
                "one-legged",
            )

    def test_entries_cannot_be_changed(self):
        deposit(self.buyer, "100.00")
        with self.assertRaises(DatabaseError) as caught:
            with transaction.atomic():
                LedgerEntry.objects.update(amount=Decimal("999.00"))
        self.assertIn("append-only", str(caught.exception))

    def test_entries_cannot_be_deleted(self):
        deposit(self.buyer, "100.00")
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                LedgerEntry.objects.all().delete()

    def test_transactions_cannot_be_changed(self):
        deposit(self.buyer, "100.00")
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                LedgerTransaction.objects.update(kind="edited")

    def test_the_same_idempotency_key_posts_once(self):
        wallet = services.wallet_for(self.buyer)
        gateway_account = services.platform_account(LedgerAccount.Kind.GATEWAY)
        legs = [(gateway_account, Decimal("-5")), (wallet, Decimal("5"))]
        first = services._post(LedgerTransaction.Kind.REFUND, legs, "same-key")
        second = services._post(LedgerTransaction.Kind.REFUND, legs, "same-key")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(services.balance_of(wallet), Decimal("5.00"))


class EscrowTests(BillingTestCase):
    def test_a_hold_moves_money_out_of_the_wallet(self):
        deposit(self.buyer, "1500.00")
        self.hold("1000.00")
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("500.00"))
        self.assertEqual(services.held_for(self.buyer), Decimal("1000.00"))

    def test_a_hold_cannot_exceed_the_wallet(self):
        deposit(self.buyer, "999.99")
        with self.assertRaises(services.InsufficientFunds):
            self.hold("1000.00")
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("999.99"))
        self.assertFalse(EscrowHold.objects.exists())

    def test_one_reference_cannot_be_held_for_twice(self):
        deposit(self.buyer, "5000.00")
        reference = uuid.uuid4()
        kwargs = dict(
            payer=self.buyer,
            payee=self.seller,
            amount=Decimal("1000.00"),
            commission_rate=Decimal("0.015"),
            reference_type="test.thing",
            reference_id=reference,
        )
        first = services.create_hold(**kwargs)
        second = services.create_hold(**kwargs)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("4000.00"))

    def test_a_release_pays_the_seller_less_commission(self):
        deposit(self.buyer, "1000.00")
        hold = services.release_hold(self.hold("1000.00", "0.0150"), Decimal("1000.00"))
        self.assertEqual(hold.status, EscrowHold.Status.SETTLED)
        self.assertEqual(hold.commission_amount, Decimal("15.00"))
        self.assertEqual(services.wallet_balance(self.seller), Decimal("985.00"))
        commission = services.platform_account(LedgerAccount.Kind.COMMISSION)
        self.assertEqual(services.balance_of(commission), Decimal("15.00"))

    def test_commission_is_charged_only_on_what_is_released(self):
        deposit(self.buyer, "1000.00")
        hold = self.hold("1000.00", "0.0150")
        services.release_hold(hold, Decimal("600.00"))
        hold = services.refund_hold(hold, Decimal("400.00"))
        self.assertEqual(hold.commission_amount, Decimal("9.00"))
        self.assertEqual(hold.status, EscrowHold.Status.SETTLED)
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("400.00"))
        self.assertEqual(services.wallet_balance(self.seller), Decimal("591.00"))

    def test_a_commission_that_rounds_to_nothing_still_releases(self):
        deposit(self.buyer, "0.10")
        hold = services.release_hold(self.hold("0.10", "0.0100"), Decimal("0.10"))
        self.assertEqual(hold.commission_amount, Decimal("0.00"))
        self.assertEqual(services.wallet_balance(self.seller), Decimal("0.10"))

    def test_a_hold_cannot_be_overspent(self):
        deposit(self.buyer, "1000.00")
        hold = self.hold("1000.00")
        services.release_hold(hold, Decimal("700.00"))
        with self.assertRaises(services.BillingError):
            services.refund_hold(hold, Decimal("300.01"))

    def test_the_database_also_refuses_overspending(self):
        deposit(self.buyer, "1000.00")
        hold = self.hold("1000.00")
        hold.released_amount = Decimal("1000.01")
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                hold.save(update_fields=["released_amount"])

    def test_a_negative_amount_cannot_reverse_a_hold(self):
        deposit(self.buyer, "1000.00")
        hold = self.hold("1000.00")
        with self.assertRaises(services.BillingError):
            services.release_hold(hold, Decimal("-1.00"))

    def test_the_rate_is_the_one_snapshotted_on_the_hold(self):
        deposit(self.buyer, "1000.00")
        hold = self.hold("1000.00", "0.0100")
        with override_settings(BILLING_COMMISSION_RATE=Decimal("0.5")):
            hold = services.release_hold(hold, Decimal("1000.00"))
        self.assertEqual(hold.commission_amount, Decimal("10.00"))


class HoldCheckTests(BillingTestCase):
    def test_the_check_is_one_query_however_many_holds_there_are(self):
        deposit(self.buyer, "1000.00")
        for _ in range(3):
            self.hold("100.00")
        with self.assertNumQueries(1):
            self.assertEqual(_holds_match_entries(), [])

    def test_each_total_that_disagrees_is_named(self):
        deposit(self.buyer, "1000.00")
        hold = self.hold("100.00")
        services.release_hold(hold, Decimal("40.00"))
        services.refund_hold(hold, Decimal("10.00"))
        holds = EscrowHold.objects.filter(pk=hold.pk)

        holds.update(
            amount=Decimal("200.00"),
            released_amount=Decimal("41.00"),
            refunded_amount=Decimal("11.00"),
        )
        self.assertEqual(
            _holds_match_entries(),
            [
                f"Hold {hold.pk}: amount disagrees with the ledger",
                f"Hold {hold.pk}: released_amount disagrees with the ledger",
                f"Hold {hold.pk}: refunded_amount disagrees with the ledger",
            ],
        )
        holds.update(
            amount=Decimal("100.00"),
            released_amount=Decimal("40.00"),
            refunded_amount=Decimal("10.00"),
        )

    def test_a_hold_with_no_entries_at_all_is_reported(self):
        hold = EscrowHold.objects.create(
            payer_organization=self.buyer,
            payee_organization=self.seller,
            status=EscrowHold.Status.SETTLED,
            amount=Decimal("100.00"),
            commission_rate=Decimal("0"),
            reference_type="test.thing",
            reference_id=uuid.uuid4(),
        )
        self.assertEqual(
            _holds_match_entries(), [f"Hold {hold.pk}: amount disagrees with the ledger"]
        )
        hold.delete()


class ProviderEventTests(BillingTestCase):
    def webhook(self, body: bytes, signature: str):
        return self.client.post(
            "/api/v1/billing/webhooks/mock",
            data=body,
            content_type="application/json",
            HTTP_X_MOCK_SIGNATURE=signature,
        )

    def pending(self, amount="1000.00") -> Payment:
        return services.start_payment(
            organization=self.buyer, amount=Decimal(amount), purpose=Payment.Purpose.TOP_UP
        )

    def signed(self, payment, event_type=gateways.PAYMENT_SUCCEEDED, amount=None):
        gateway = gateways.get_gateway("mock")
        body = gateway.build_event(
            event_type, payment.provider_reference, amount or payment.amount
        )
        return body, gateway.sign(body)

    def test_a_signed_event_credits_the_wallet(self):
        payment = self.pending()
        response = self.webhook(*self.signed(payment))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], "succeeded")
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("1000.00"))

    def test_a_bad_signature_changes_nothing(self):
        payment = self.pending()
        body, _ = self.signed(payment)
        response = self.webhook(body, "0" * 64)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("0.00"))
        self.assertFalse(WebhookEvent.objects.exists())

    def test_a_tampered_body_is_refused(self):
        payment = self.pending()
        body, signature = self.signed(payment)
        tampered = body.replace(b"1000.00", b"9000.00")
        self.assertEqual(self.webhook(tampered, signature).status_code, 400)

    def test_a_replayed_event_credits_once(self):
        payment = self.pending()
        signed = self.signed(payment)
        self.webhook(*signed)
        replay = self.webhook(*signed)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["duplicate"])
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("1000.00"))

    def test_a_second_event_for_the_same_payment_credits_once(self):
        payment = self.pending()
        self.webhook(*self.signed(payment))
        self.webhook(*self.signed(payment))
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("1000.00"))

    def test_a_mismatched_amount_is_not_credited(self):
        payment = self.pending("1000.00")
        response = self.webhook(*self.signed(payment, amount="1.00"))
        self.assertIn("rejected", response.json()["outcome"])
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("0.00"))
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_a_failure_followed_by_success_is_credited(self):
        payment = self.pending()
        self.webhook(*self.signed(payment, gateways.PAYMENT_FAILED))
        self.webhook(*self.signed(payment))
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCEEDED)
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("1000.00"))

    def test_a_success_followed_by_failure_is_not_reversed(self):
        payment = self.pending()
        self.webhook(*self.signed(payment))
        response = self.webhook(*self.signed(payment, gateways.PAYMENT_FAILED))
        self.assertIn("ignored", response.json()["outcome"])
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("1000.00"))

    def test_an_event_that_beats_our_own_write_is_not_lost(self):
        gateway = gateways.get_gateway("mock")
        payment = self.pending()
        reference = payment.provider_reference
        Payment.objects.filter(pk=payment.pk).update(provider_reference="")
        body = gateway.build_event(gateways.PAYMENT_SUCCEEDED, reference, payment.amount)

        early = self.webhook(body, gateway.sign(body))
        self.assertEqual(early.status_code, 503)
        self.assertFalse(WebhookEvent.objects.exists())

        Payment.objects.filter(pk=payment.pk).update(provider_reference=reference)
        retried = self.webhook(body, gateway.sign(body))
        self.assertEqual(retried.status_code, 200)
        self.assertFalse(retried.json()["duplicate"])
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("1000.00"))

    def test_a_refused_event_is_put_in_front_of_a_person(self):
        payment = self.pending("1000.00")
        self.assertEqual(needs_a_person(), [])
        self.webhook(*self.signed(payment, amount="1.00"))
        problems = needs_a_person()
        self.assertEqual(len(problems), 1)
        self.assertIn("does not match", problems[0])

    def test_a_chargeback_is_put_in_front_of_a_person(self):
        payment = self.pending()
        self.webhook(*self.signed(payment))
        self.webhook(*self.signed(payment, gateways.PAYMENT_FAILED))
        self.assertEqual(len(needs_a_person()), 1)

    def test_a_refused_event_stops_nagging_eventually(self):
        payment = self.pending("1000.00")
        self.webhook(*self.signed(payment, amount="1.00"))
        later = timezone.now() + timedelta(days=3)
        self.assertEqual(needs_a_person(now=later), [])

    def test_a_crashed_checkout_does_not_block_the_retry(self):
        def start():
            return services.start_payment(
                organization=self.buyer,
                amount=Decimal("100.00"),
                purpose=Payment.Purpose.ORDER,
                reference_type="test.thing",
                reference_id=reference,
            )

        reference = uuid.uuid4()
        with mock.patch.object(
            gateways.MockGateway, "create_checkout", side_effect=TimeoutError("slow")
        ):
            with self.assertRaises(TimeoutError):
                start()
        self.assertEqual(Payment.objects.get().status, Payment.Status.FAILED)
        self.assertTrue(start().checkout_url)

    def row_left_by_a_killed_process(self, **fields):
        return Payment.objects.create(
            organization=self.buyer,
            amount=Decimal("100.00"),
            purpose=Payment.Purpose.ORDER,
            provider="mock",
            **fields,
        )

    def age(self, payment):
        Payment.objects.filter(pk=payment.pk).update(
            created_at=timezone.now() - timedelta(minutes=2)
        )

    def test_a_killed_checkout_is_finished_by_the_next_attempt_for_the_order(self):
        reference = uuid.uuid4()
        payment = self.row_left_by_a_killed_process(
            reference_type="test.thing", reference_id=reference
        )

        def start():
            return services.start_payment(
                organization=self.buyer,
                amount=Decimal("100.00"),
                purpose=Payment.Purpose.ORDER,
                reference_type="test.thing",
                reference_id=reference,
            )

        with mock.patch.object(gateways.MockGateway, "create_checkout") as create:
            self.assertEqual(start().pk, payment.pk)
        create.assert_not_called()

        self.age(payment)
        finished = start()
        self.assertEqual(finished.pk, payment.pk)
        self.assertTrue(finished.checkout_url)
        self.assertTrue(finished.provider_reference)

    def test_a_killed_checkout_is_finished_by_a_replayed_top_up(self):
        payment = self.row_left_by_a_killed_process(idempotency_key="once")
        self.age(payment)
        finished = services.start_payment(
            organization=self.buyer,
            amount=Decimal("100.00"),
            purpose=Payment.Purpose.TOP_UP,
            idempotency_key="once",
        )
        self.assertEqual(finished.pk, payment.pk)
        self.assertTrue(finished.checkout_url)

    def test_a_failed_payment_is_not_reopened_by_a_replay(self):
        payment = self.row_left_by_a_killed_process(
            idempotency_key="once", status=Payment.Status.FAILED
        )
        self.age(payment)
        replayed = services.start_payment(
            organization=self.buyer,
            amount=Decimal("100.00"),
            purpose=Payment.Purpose.TOP_UP,
            idempotency_key="once",
        )
        self.assertEqual(replayed.status, Payment.Status.FAILED)
        self.assertFalse(replayed.checkout_url)

    def test_two_retries_of_a_killed_checkout_keep_the_first_reference(self):
        payment = self.row_left_by_a_killed_process(idempotency_key="once")
        self.age(payment)

        def beaten_to_it(row):
            Payment.objects.filter(pk=row.pk).update(
                provider_reference="first", checkout_url="/first"
            )
            return gateways.Checkout("second", "/second")

        with mock.patch.object(
            gateways.MockGateway, "create_checkout", side_effect=beaten_to_it
        ):
            finished = services.start_payment(
                organization=self.buyer,
                amount=Decimal("100.00"),
                purpose=Payment.Purpose.TOP_UP,
                idempotency_key="once",
            )
        self.assertEqual(finished.provider_reference, "first")
        self.assertEqual(finished.checkout_url, "/first")

    def test_a_failed_effect_leaves_the_event_retryable(self):
        payment = self.pending()
        event_id = uuid.uuid4().hex
        with mock.patch(
            "billing.services.payment_succeeded.send", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                send_event(
                    gateways.PAYMENT_SUCCEEDED,
                    payment.provider_reference,
                    payment.amount,
                    event_id=event_id,
                )
        self.assertFalse(WebhookEvent.objects.exists())
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("0.00"))

        send_event(
            gateways.PAYMENT_SUCCEEDED,
            payment.provider_reference,
            payment.amount,
            event_id=event_id,
        )
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("1000.00"))

    def test_an_unknown_provider_is_a_404(self):
        response = self.client.post(
            "/api/v1/billing/webhooks/paypal", data=b"{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 404)


@override_settings(BILLING_MOCK_ENABLED=False)
class MockRefusedInProductionTests(BillingTestCase):
    def test_no_top_up(self):
        response = self.post("top-ups", {"amount": "100.00"})
        self.assertEqual(response.status_code, 503)

    def test_no_mock_complete(self):
        with override_settings(BILLING_MOCK_ENABLED=True):
            payment = services.start_payment(
                organization=self.buyer,
                amount=Decimal("100.00"),
                purpose=Payment.Purpose.TOP_UP,
            )
        response = self.post(f"payments/{payment.public_id}/mock-complete")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("0.00"))

    def test_no_webhook(self):
        response = self.client.post(
            "/api/v1/billing/webhooks/mock", data=b"{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 404)


class SettingsFileTests(SimpleTestCase):
    """Reads the real settings file: a test override could not catch this."""

    def settings_with(self, **env):
        clean = {"SECRET_KEY": "x", **env}
        with mock.patch.dict(os.environ, clean):
            for name in ("ALLOWED_HOSTS", "BILLING_MOCK_ENABLED", "ENVIRONMENT"):
                if name not in env:
                    os.environ.pop(name, None)
            return runpy.run_path(str(django_settings.BASE_DIR / "agro/settings.py"))

    def test_off_when_nobody_asked_for_it(self):
        self.assertFalse(self.settings_with()["BILLING_MOCK_ENABLED"])

    def test_on_only_when_asked_for(self):
        self.assertTrue(self.settings_with(BILLING_MOCK_ENABLED="true")["BILLING_MOCK_ENABLED"])

    def test_never_in_production(self):
        loaded = self.settings_with(
            ALLOWED_HOSTS="api.agronome.uz",
            BILLING_MOCK_ENABLED="true",
            ENVIRONMENT="production",
        )
        self.assertFalse(loaded["BILLING_MOCK_ENABLED"])

    def test_a_commission_rate_above_one_stops_the_process(self):
        with self.assertRaises(ImproperlyConfigured):
            self.settings_with(BILLING_COMMISSION_RATE="1.5")

    def test_any_host_is_allowed_outside_production(self):
        loaded = self.settings_with(ALLOWED_HOSTS="api.agronome.uz")
        self.assertEqual(loaded["ALLOWED_HOSTS"], ["*"])

    def test_production_takes_its_hosts_from_the_environment(self):
        loaded = self.settings_with(
            ALLOWED_HOSTS="api.agronome.uz, agronome.uz ,", ENVIRONMENT="production"
        )
        self.assertEqual(loaded["ALLOWED_HOSTS"], ["api.agronome.uz", "agronome.uz"])

    def test_production_with_no_hosts_stops_the_process(self):
        with self.assertRaises(ImproperlyConfigured):
            self.settings_with(ENVIRONMENT="production")


class WalletApiTests(BillingTestCase):
    def test_a_top_up_through_the_api(self):
        payment = self.post("top-ups", {"amount": "2500.50"}).json()
        self.assertEqual(payment["status"], "pending")
        self.assertTrue(payment["checkout_url"])

        done = self.post(f"payments/{payment['id']}/mock-complete")
        self.assertEqual(done.json()["status"], "succeeded")

        wallet = self.get("wallet").json()
        self.assertEqual(Decimal(wallet["balance"]), Decimal("2500.50"))
        self.assertEqual(wallet["currency"], "UZS")

        entries = self.get("wallet/entries").json()
        self.assertEqual(entries["count"], 1)
        self.assertEqual(entries["items"][0]["kind"], "deposit")

    def test_a_failed_checkout_credits_nothing(self):
        payment = self.post("top-ups", {"amount": "100.00"}).json()
        done = self.post(
            f"payments/{payment['id']}/mock-complete", {"outcome": "failed"}
        ).json()
        self.assertEqual(done["status"], "failed")
        self.assertEqual(Decimal(self.get("wallet").json()["balance"]), 0)

    def test_the_same_idempotency_key_is_one_payment(self):
        data = {"amount": "100.00", "idempotency_key": "double-tap"}
        first = self.post("top-ups", data).json()
        second = self.post("top-ups", data).json()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(Payment.objects.count(), 1)

    def test_a_retry_writes_one_audit_row(self):
        data = {"amount": "100.00", "idempotency_key": "double-tap"}
        self.post("top-ups", data)
        self.post("top-ups", data)
        self.assertEqual(ActivityLog.objects.filter(organization=self.buyer).count(), 1)

    def test_no_payment_exists_without_its_audit_row(self):
        self.client.raise_request_exception = False
        with mock.patch("billing.views.log_activity", side_effect=RuntimeError("boom")):
            response = self.post("top-ups", {"amount": "100.00"})
        self.assertEqual(response.status_code, 500)
        self.assertFalse(Payment.objects.exists())

    def test_a_reused_key_with_another_amount_is_refused(self):
        self.post("top-ups", {"amount": "100.00", "idempotency_key": "k"})
        response = self.post("top-ups", {"amount": "200.00", "idempotency_key": "k"})
        self.assertEqual(response.status_code, 409)

    def test_money_with_more_precision_than_the_column_is_refused(self):
        self.assertEqual(self.post("top-ups", {"amount": "1.001"}).status_code, 422)
        self.assertEqual(self.post("top-ups", {"amount": "-5"}).status_code, 422)
        self.assertEqual(self.post("top-ups", {"amount": "0"}).status_code, 422)

    def test_a_top_up_is_audited(self):
        self.post("top-ups", {"amount": "100.00"})
        row = ActivityLog.objects.get(organization=self.buyer)
        self.assertEqual(row.actor, self.buyer_owner)
        self.assertEqual(row.changes["amount"]["to"], "100.00")

    def test_another_organizations_payment_is_a_404(self):
        payment = deposit(self.buyer, "100.00")
        response = self.get(f"payments/{payment.public_id}", self.outsider_owner)
        self.assertEqual(response.status_code, 404)
        response = self.post(
            f"payments/{payment.public_id}/mock-complete", user=self.outsider_owner
        )
        self.assertEqual(response.status_code, 404)

    def test_permissions(self):
        blind = User.objects.create_user(username="blind", organization=self.buyer)
        viewer = User.objects.create_user(
            username="viewer",
            organization=self.buyer,
            permissions=[OrgPermission.VIEW_BILLING.value],
        )
        self.assertEqual(self.get("wallet", blind).status_code, 403)
        self.assertEqual(self.get("wallet", viewer).status_code, 200)
        response = self.post("top-ups", {"amount": "100.00"}, viewer)
        self.assertEqual(response.status_code, 403)

    def test_an_anonymous_caller_is_refused(self):
        self.assertEqual(self.client.get("/api/v1/billing/wallet").status_code, 401)


@override_settings(OTP_TEST_PHONES=[PHONE], BILLING_PAYOUT_COOLDOWN_HOURS=24)
class PayoutTests(BillingTestCase):
    ACCOUNT = "20208000123456789012"

    def add_destination(self, code="000000", account=None):
        self.post("payout-destinations/code")
        return self.post(
            "payout-destinations",
            {
                "kind": "bank_account",
                "label": "Main account",
                "holder_name": "Buyer LLC",
                "account_number": account or self.ACCOUNT,
                "code": code,
            },
        )

    def ready_destination(self) -> PayoutDestination:
        response = self.add_destination()
        destination = PayoutDestination.objects.get(public_id=response.json()["id"])
        destination.usable_from = timezone.now() - timedelta(minutes=1)
        destination.save(update_fields=["usable_from"])
        return destination

    def payout(self, destination, amount="400.00", **extra):
        return self.post(
            "payouts",
            {"destination_id": str(destination.public_id), "amount": amount, **extra},
        )

    def test_the_account_number_is_never_stored_or_returned(self):
        response = self.add_destination()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["last4"], "9012")
        self.assertNotIn("provider_token", body)
        self.assertNotIn(self.ACCOUNT, json.dumps(body))
        destination = PayoutDestination.objects.get()
        self.assertNotIn(self.ACCOUNT, str(vars(destination)))

    def test_adding_an_account_needs_a_correct_code(self):
        self.assertEqual(self.add_destination(code="999999").status_code, 400)
        self.assertFalse(PayoutDestination.objects.exists())

    def test_adding_an_account_without_a_phone_is_refused(self):
        response = self.post("payout-destinations/code", user=self.seller_owner)
        self.assertEqual(response.status_code, 400)

    def test_a_new_account_cannot_receive_money_straight_away(self):
        deposit(self.buyer, "1000.00")
        destination = PayoutDestination.objects.get(
            public_id=self.add_destination().json()["id"]
        )
        response = self.payout(destination)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("1000.00"))

    def test_a_payout_leaves_the_wallet_and_settles(self):
        deposit(self.buyer, "1000.00")
        payout = self.payout(self.ready_destination()).json()
        self.assertEqual(payout["status"], "processing")
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("600.00"))

        done = self.post(f"payouts/{payout['id']}/mock-complete").json()
        self.assertEqual(done["status"], "paid")
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("600.00"))
        in_transit = services.platform_account(LedgerAccount.Kind.PAYOUTS_IN_TRANSIT)
        self.assertEqual(services.balance_of(in_transit), Decimal("0.00"))

    def test_a_failed_payout_returns_the_money(self):
        deposit(self.buyer, "1000.00")
        payout = self.payout(self.ready_destination()).json()
        done = self.post(
            f"payouts/{payout['id']}/mock-complete", {"outcome": "failed"}
        ).json()
        self.assertEqual(done["status"], "failed")
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("1000.00"))

    def test_a_provider_refusal_returns_the_money(self):
        deposit(self.buyer, "1000.00")
        destination = self.ready_destination()
        with mock.patch.object(
            gateways.MockGateway, "send_payout", side_effect=gateways.GatewayError("no")
        ):
            response = self.payout(destination)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("1000.00"))
        self.assertEqual(Payout.objects.get().status, Payout.Status.FAILED)

    def test_a_timeout_does_not_return_money_that_may_have_left(self):
        deposit(self.buyer, "1000.00")
        destination = self.ready_destination()
        self.client.raise_request_exception = False
        with mock.patch.object(
            gateways.MockGateway, "send_payout", side_effect=TimeoutError("slow")
        ):
            response = self.payout(destination)
        self.assertEqual(response.status_code, 500)
        payout = Payout.objects.get()
        self.assertEqual(payout.status, Payout.Status.REQUESTED)
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("600.00"))

        self.assertEqual(needs_a_person(), [])
        later = timezone.now() + timedelta(hours=2)
        self.assertIn("requested", needs_a_person(now=later)[0])

    def test_a_completed_payout_cannot_be_failed_afterwards(self):
        deposit(self.buyer, "1000.00")
        payout = self.payout(self.ready_destination()).json()
        self.post(f"payouts/{payout['id']}/mock-complete")
        done = self.post(f"payouts/{payout['id']}/mock-complete", {"outcome": "failed"})
        self.assertEqual(done.json()["status"], "paid")
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("600.00"))

    def test_a_failed_payout_that_turns_out_paid_is_put_in_front_of_a_person(self):
        deposit(self.buyer, "1000.00")
        self.payout(self.ready_destination())
        payout = Payout.objects.get()
        send_event(gateways.PAYOUT_FAILED, payout.provider_reference, payout.amount)
        self.assertEqual(needs_a_person(), [])

        send_event(gateways.PAYOUT_PAID, payout.provider_reference, payout.amount)
        problems = needs_a_person()
        self.assertEqual(len(problems), 1)
        self.assertIn("ignored: payout is failed", problems[0])

    def test_a_payout_event_with_the_wrong_amount_changes_nothing(self):
        deposit(self.buyer, "1000.00")
        self.payout(self.ready_destination())
        payout = Payout.objects.get()
        send_event(gateways.PAYOUT_PAID, payout.provider_reference, "1.00")

        payout.refresh_from_db()
        self.assertEqual(payout.status, Payout.Status.PROCESSING)
        in_transit = services.platform_account(LedgerAccount.Kind.PAYOUTS_IN_TRANSIT)
        self.assertEqual(services.balance_of(in_transit), Decimal("400.00"))
        problems = needs_a_person()
        self.assertEqual(len(problems), 1)
        self.assertIn("does not match", problems[0])

    def test_a_retried_payout_writes_one_audit_row(self):
        deposit(self.buyer, "1000.00")
        destination = self.ready_destination()
        self.payout(destination, idempotency_key="once")
        self.payout(destination, idempotency_key="once")
        rows = ActivityLog.objects.filter(
            organization=self.buyer, content_type__model="payout"
        )
        self.assertEqual(rows.count(), 1)

    def test_a_payout_cannot_exceed_the_wallet(self):
        deposit(self.buyer, "100.00")
        response = self.payout(self.ready_destination())
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Payout.objects.exists())

    def test_held_money_cannot_be_withdrawn(self):
        deposit(self.buyer, "1000.00")
        self.hold("800.00")
        response = self.payout(self.ready_destination())
        self.assertEqual(response.status_code, 409)

    def test_the_same_idempotency_key_withdraws_once(self):
        deposit(self.buyer, "1000.00")
        destination = self.ready_destination()
        first = self.payout(destination, idempotency_key="once").json()
        second = self.payout(destination, idempotency_key="once").json()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("600.00"))

    def test_a_removed_account_cannot_be_used(self):
        deposit(self.buyer, "1000.00")
        destination = self.ready_destination()
        removed = self.client.delete(
            f"/api/v1/billing/payout-destinations/{destination.public_id}",
            **auth(self.buyer_owner),
        )
        self.assertEqual(removed.status_code, 204)
        self.assertEqual(self.payout(destination).status_code, 400)

    def test_another_organizations_account_is_a_404(self):
        destination = self.ready_destination()
        deposit(self.outsider, "1000.00")
        response = self.post(
            "payouts",
            {"destination_id": str(destination.public_id), "amount": "1.00"},
            self.outsider_owner,
        )
        self.assertEqual(response.status_code, 404)


class ReconcileCommandTests(TransactionTestCase):
    """Outside a test transaction: SET TRANSACTION has to be its first statement."""

    def test_every_check_reads_one_read_only_snapshot(self):
        seen = []

        def record_transaction():
            with connection.cursor() as cursor:
                cursor.execute("SHOW transaction_isolation")
                seen.append(cursor.fetchone()[0])
                cursor.execute("SHOW transaction_read_only")
                seen.append(cursor.fetchone()[0])
            return []

        with mock.patch(
            "billing.management.commands.reconcile_ledger.reconcile", record_transaction
        ):
            call_command("reconcile_ledger", stdout=StringIO())
        self.assertEqual(seen, ["repeatable read", "on"])

    def test_the_real_checks_run_inside_it(self):
        out = StringIO()
        call_command("reconcile_ledger", stdout=out)
        self.assertIn("The ledger reconciles.", out.getvalue())


@override_settings(BILLING_MOCK_ENABLED=True)
class ConcurrencyTests(TransactionTestCase):
    """Real transactions and real threads: the locks are what is under test."""

    def setUp(self):
        region = Region.objects.create(name="Race Region", code="RCE", soato="1797")
        self.buyer, _ = make_org("race-buyer", region)
        self.seller, _ = make_org("race-seller", region)
        deposit(self.buyer, "1000.00")

    def attempt(self, function):
        def run(_):
            try:
                return function()
            except services.BillingError:
                return None
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(run, range(4)))

    def test_concurrent_holds_cannot_overdraw_a_wallet(self):
        def take():
            return services.create_hold(
                payer=self.buyer,
                payee=self.seller,
                amount=Decimal("400.00"),
                commission_rate=Decimal("0.01"),
                reference_type="test.race",
                reference_id=uuid.uuid4(),
            )

        results = self.attempt(take)
        self.assertEqual(len([hold for hold in results if hold]), 2)
        self.assertEqual(services.wallet_balance(self.buyer), Decimal("200.00"))
        self.assertEqual(reconcile(), [])

    def test_concurrent_releases_cannot_overspend_a_hold(self):
        hold = services.create_hold(
            payer=self.buyer,
            payee=self.seller,
            amount=Decimal("1000.00"),
            commission_rate=Decimal("0"),
            reference_type="test.race",
            reference_id=uuid.uuid4(),
        )
        results = self.attempt(lambda: services.release_hold(hold, Decimal("400.00")))
        self.assertEqual(len([done for done in results if done]), 2)
        self.assertEqual(services.wallet_balance(self.seller), Decimal("800.00"))
        self.assertEqual(reconcile(), [])
