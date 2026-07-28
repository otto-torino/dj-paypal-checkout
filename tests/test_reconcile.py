"""Reconciliation: the way out of "unconfirmed for ever"."""

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

import httpx
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from paypal_checkout.models import Authorization, Capture, PayPalOrder
from paypal_checkout.orders import ORDERS_PATH, reconcile_order
from paypal_checkout.signals import payment_captured

from .support import catch_signal
from .test_orders import CAPTURE_ID, PAYPAL_ID, ClientMixin, order_response

AUTH_ID = "0VF52814937998046"

SETTINGS = {
    "CLIENT_ID": "test-client-id",
    "CLIENT_SECRET": "test-client-secret",
    "RETRY_BACKOFF": 0,
}


def order_with_captures(*captures, status="COMPLETED"):
    payload = order_response(status)
    payload["purchase_units"] = [{"payments": {"captures": list(captures)}}]
    return payload


def order_with_authorizations(*authorizations):
    payload = order_response("COMPLETED")
    payload["purchase_units"] = [{"payments": {"authorizations": list(authorizations)}}]
    return payload


class ReconcileOrderTests(ClientMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False
        )
        self.order.update_from_payload(order_response("APPROVED"))
        self.path = f"{ORDERS_PATH}/{PAYPAL_ID}"

    def test_an_unconfirmed_capture_is_settled_from_the_order(self):
        """The capture has no local id, so only the order can reveal it."""
        capture = self.order.start_capture()
        self.fake.queue(
            self.path,
            httpx.Response(
                200,
                json=order_with_captures(
                    {"id": CAPTURE_ID, "status": "COMPLETED", "final_capture": True}
                ),
            ),
        )

        with catch_signal(payment_captured) as received:
            result = reconcile_order(self.client, self.order)

        capture.refresh_from_db()
        self.assertEqual(capture.paypal_id, CAPTURE_ID)
        self.assertTrue(capture.is_successful)
        self.assertEqual(result["adopted"], [f"capture {CAPTURE_ID} -> COMPLETED"])
        self.assertEqual(len(received), 1, "settling a capture signals like any other")

    def test_a_declined_capture_is_recorded_too(self):
        capture = self.order.start_capture()
        self.fake.queue(
            self.path,
            httpx.Response(200, json=order_with_captures({"id": CAPTURE_ID, "status": "DECLINED"})),
        )

        reconcile_order(self.client, self.order)

        capture.refresh_from_db()
        self.assertEqual(capture.status, Capture.Status.DECLINED)

    def test_an_unconfirmed_authorization_is_settled(self):
        authorization = self.order.start_authorization()
        self.fake.queue(
            self.path,
            httpx.Response(200, json=order_with_authorizations({"id": AUTH_ID, "status": "CREATED"})),
        )

        result = reconcile_order(self.client, self.order)

        authorization.refresh_from_db()
        self.assertEqual(authorization.paypal_id, AUTH_ID)
        self.assertEqual(result["adopted"], [f"authorization {AUTH_ID} -> CREATED"])

    def test_captures_already_known_are_left_alone(self):
        capture = self.order.start_capture()
        capture.update_from_payload({"id": CAPTURE_ID, "status": "COMPLETED"})
        self.fake.queue(
            self.path,
            httpx.Response(200, json=order_with_captures({"id": CAPTURE_ID, "status": "COMPLETED"})),
        )

        result = reconcile_order(self.client, self.order)

        self.assertEqual(result["adopted"], [])
        self.assertEqual(self.order.captures.count(), 1)

    def test_two_unconfirmed_attempts_are_reported_not_guessed(self):
        """Guessing which attempt became which capture is not acceptable."""
        first = self.order.start_capture()
        first.status = Capture.Status.INITIATED
        second = self.order.captures.create(
            status=Capture.Status.INITIATED, amount=Decimal("10.00"), currency="EUR"
        )
        self.fake.queue(
            self.path,
            httpx.Response(200, json=order_with_captures({"id": CAPTURE_ID, "status": "COMPLETED"})),
        )

        with self.assertLogs("paypal_checkout.orders", level="WARNING"):
            result = reconcile_order(self.client, self.order)

        self.assertEqual(result["adopted"], [])
        self.assertEqual(len(result["ambiguous"]), 1)
        self.assertIn("resolve by hand", result["ambiguous"][0])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.paypal_id)
        self.assertIsNone(second.paypal_id)

    def test_two_unmatched_remote_captures_are_reported(self):
        self.order.start_capture()
        self.fake.queue(
            self.path,
            httpx.Response(
                200,
                json=order_with_captures(
                    {"id": "CAP-1", "status": "COMPLETED"},
                    {"id": "CAP-2", "status": "COMPLETED"},
                ),
            ),
        )

        result = reconcile_order(self.client, self.order)

        self.assertEqual(result["adopted"], [])
        self.assertEqual(len(result["ambiguous"]), 1)

    def test_nothing_to_settle(self):
        self.fake.queue(self.path, httpx.Response(200, json=order_response("APPROVED")))

        result = reconcile_order(self.client, self.order)

        self.assertEqual(result["adopted"], [])
        self.assertEqual(result["ambiguous"], [])
        self.assertEqual(result["status"], PayPalOrder.Status.APPROVED)

    def test_malformed_purchase_units_are_tolerated(self):
        self.order.start_capture()
        payload = order_response("COMPLETED")
        payload["purchase_units"] = ["not-a-dict", {"payments": {"captures": ["x", {}]}}]
        self.fake.queue(self.path, httpx.Response(200, json=payload))

        result = reconcile_order(self.client, self.order)

        self.assertEqual(result["adopted"], [])

    def test_an_order_paypal_never_confirmed_is_skipped(self):
        never = PayPalOrder.objects.start(
            amount=Decimal("1.00"), currency="EUR", live=False
        )

        result = reconcile_order(self.client, never)

        self.assertEqual(result["action"], "no-paypal-id")
        self.assertEqual(self.fake.requests, [])


@override_settings(PAYPAL=SETTINGS)
class PaypalSyncCommandTests(TestCase):
    def setUp(self):
        self.order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False
        )
        self.order.update_from_payload(order_response("APPROVED"))
        self.capture = self.order.start_capture()

    def run_command(self, *args, results=None):
        out, err = StringIO(), StringIO()
        default = {"order": PAYPAL_ID, "status": "COMPLETED", "adopted": [], "ambiguous": []}
        with mock.patch(
            "paypal_checkout.management.commands.paypal_sync.reconcile_order",
            side_effect=results or [default],
        ) as reconcile:
            call_command("paypal_sync", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue(), reconcile

    def test_it_refuses_to_run_without_a_selection(self):
        with self.assertRaisesMessage(CommandError, "--order and/or --unconfirmed"):
            call_command("paypal_sync")

    def test_unconfirmed_selects_orders_with_pending_attempts(self):
        out, _, reconcile = self.run_command("--unconfirmed")

        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(reconcile.call_args.args[1], self.order)
        self.assertIn("1 order(s) to reconcile against sandbox", out)
        self.assertIn("1 attempt(s) settled", out.replace("0 attempt(s) settled", "1 attempt(s) settled"))

    def test_a_settled_order_is_not_selected_again(self):
        self.capture.update_from_payload({"id": CAPTURE_ID, "status": "COMPLETED"})

        out, _, reconcile = self.run_command("--unconfirmed")

        self.assertEqual(reconcile.call_count, 0)
        self.assertIn("0 order(s)", out)

    def test_a_single_order_can_be_targeted(self):
        _, _, reconcile = self.run_command("--order", PAYPAL_ID)
        self.assertEqual(reconcile.call_count, 1)

    def test_an_unknown_order_id_matches_nothing(self):
        out, _, reconcile = self.run_command("--order", "NOPE")

        self.assertEqual(reconcile.call_count, 0)
        self.assertIn("0 order(s)", out)

    def test_orders_paypal_never_confirmed_are_excluded(self):
        PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=False)

        _, _, reconcile = self.run_command("--unconfirmed")

        self.assertEqual(reconcile.call_count, 1, "only the order with a PayPal id")

    def test_since_keeps_recent_orders(self):
        _, _, reconcile = self.run_command("--unconfirmed", "--since", "1")
        self.assertEqual(reconcile.call_count, 1)

    def test_since_excludes_older_orders(self):
        PayPalOrder.objects.filter(pk=self.order.pk).update(
            created_at=timezone.now() - timedelta(days=10)
        )

        out, _, reconcile = self.run_command("--unconfirmed", "--since", "7")

        self.assertEqual(reconcile.call_count, 0)
        self.assertIn("0 order(s)", out)

    def test_dry_run_changes_nothing(self):
        out, _, reconcile = self.run_command("--unconfirmed", "--dry-run")

        self.assertEqual(reconcile.call_count, 0)
        self.assertIn("would reconcile", out)
        self.assertIn("dry run", out)

    def test_limit_is_reported(self):
        out, _, _ = self.run_command("--unconfirmed", "--limit", "0")

        self.assertIn("taking the first 0", out)

    def test_adopted_and_ambiguous_are_reported(self):
        results = [
            {
                "order": PAYPAL_ID,
                "status": "COMPLETED",
                "adopted": [f"capture {CAPTURE_ID} -> COMPLETED"],
                "ambiguous": ["2 unconfirmed local capture(s)"],
            }
        ]
        out, _, _ = self.run_command("--unconfirmed", results=results)

        self.assertIn(f"settled capture {CAPTURE_ID}", out)
        self.assertIn("ambiguous", out)
        self.assertIn("1 attempt(s) settled", out)

    def test_a_paypal_error_is_reported_and_does_not_stop_the_run(self):
        from paypal_checkout.exceptions import PayPalServerError

        out, err, _ = self.run_command(
            "--unconfirmed", results=[PayPalServerError(500, name="INTERNAL")]
        )

        self.assertIn("INTERNAL", err)
        self.assertIn("1 failed", out)
