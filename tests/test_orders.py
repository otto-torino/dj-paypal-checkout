import json
from decimal import Decimal

import httpx
from django.core.cache import cache
from django.test import TestCase

from paypal_checkout.exceptions import (
    PayPalAmountError,
    PayPalError,
    PayPalServerError,
)
from paypal_checkout.models import Capture, PayPalOrder
from paypal_checkout.orders import (
    ORDERS_PATH,
    capture_order,
    create_order,
    fetch_order,
    refresh_order,
)
from paypal_checkout.signals import payment_captured, payment_denied
from paypal_checkout.client import PayPalClient

from .support import FakePayPal, catch_signal, make_config
from .test_app.models import ShopOrder

PAYPAL_ID = "5O190127TN364715T"


CAPTURE_ID = "3C679366HH908993F"


def order_response(status="CREATED", value="10.00", currency="EUR", paypal_id=PAYPAL_ID):
    return {
        "id": paypal_id,
        "status": status,
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": currency, "value": value}}],
        "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow"}],
    }


def capture_response(
    status="COMPLETED", value="10.00", currency="EUR", final_capture=True, capture_id=CAPTURE_ID
):
    """The capture endpoint answers with the *order*, capture nested inside."""
    return {
        "id": PAYPAL_ID,
        "status": "COMPLETED",
        "purchase_units": [
            {
                "payments": {
                    "captures": [
                        {
                            "id": capture_id,
                            "status": status,
                            "final_capture": final_capture,
                            "amount": {"currency_code": currency, "value": value},
                        }
                    ]
                }
            }
        ],
    }


def sent_body(request):
    return json.loads(request.read() or b"null")


class ClientMixin:
    def setUp(self):
        cache.clear()
        self.fake = FakePayPal()
        self.config = make_config()
        self.client = PayPalClient(self.config, transport=self.fake.transport)
        self.addCleanup(self.client.close)


class CreateOrderTests(ClientMixin, TestCase):
    def test_row_is_created_and_updated_from_the_response(self):
        self.fake.queue(ORDERS_PATH, httpx.Response(201, json=order_response()))

        order = create_order(self.client, amount=Decimal("10.00"))

        self.assertEqual(order.paypal_id, PAYPAL_ID)
        self.assertEqual(order.status, PayPalOrder.Status.CREATED)
        self.assertEqual(order.amount, Decimal("10.00"))
        self.assertEqual(order.currency, "EUR")
        self.assertFalse(order.live)

    def test_persisted_key_is_sent_as_the_idempotency_header(self):
        self.fake.queue(ORDERS_PATH, httpx.Response(201, json=order_response()))

        order = create_order(self.client, amount=Decimal("10.00"))

        request = self.fake.api_requests()[0]
        self.assertEqual(request.headers["paypal-request-id"], order.request_id)
        self.assertEqual(request.headers["paypal-request-id"], f"order:{order.pk}:create")

    def test_amount_is_built_by_the_library_not_the_caller(self):
        self.fake.queue(ORDERS_PATH, httpx.Response(201, json=order_response()))

        create_order(self.client, amount=Decimal("10.5"), currency="eur")

        body = sent_body(self.fake.api_requests()[0])
        self.assertEqual(
            body["purchase_units"][0]["amount"],
            {"currency_code": "EUR", "value": "10.50"},
        )

    def test_currency_defaults_to_the_configured_one(self):
        self.fake.queue(ORDERS_PATH, httpx.Response(201, json=order_response()))
        client = PayPalClient(make_config(currency="USD"), transport=self.fake.transport)
        self.addCleanup(client.close)

        order = create_order(client, amount=Decimal("10.00"))

        self.assertEqual(order.currency, "USD")

    def test_intent_and_application_context_are_forwarded(self):
        self.fake.queue(ORDERS_PATH, httpx.Response(201, json=order_response()))

        create_order(
            self.client,
            amount=Decimal("10.00"),
            intent=PayPalOrder.Intent.AUTHORIZE,
            application_context={"return_url": "https://example.com/ok"},
        )

        body = sent_body(self.fake.api_requests()[0])
        self.assertEqual(body["intent"], "AUTHORIZE")
        self.assertEqual(body["application_context"]["return_url"], "https://example.com/ok")

    def test_payment_source_is_forwarded(self):
        self.fake.queue(ORDERS_PATH, httpx.Response(201, json=order_response()))

        create_order(
            self.client,
            amount=Decimal("10.00"),
            payment_source={"paypal": {"experience_context": {"locale": "it-IT"}}},
        )

        body = sent_body(self.fake.api_requests()[0])
        self.assertEqual(body["payment_source"]["paypal"]["experience_context"]["locale"], "it-IT")

    def test_target_is_linked(self):
        self.fake.queue(ORDERS_PATH, httpx.Response(201, json=order_response()))
        shop_order = ShopOrder.objects.create(reference="ORD-1", total=Decimal("10.00"))

        order = create_order(self.client, amount=Decimal("10.00"), target=shop_order)

        order.refresh_from_db()
        self.assertEqual(order.target, shop_order)

    def test_live_flag_comes_from_the_config(self):
        self.fake.queue(ORDERS_PATH, httpx.Response(201, json=order_response()))
        client = PayPalClient(make_config(live=True), transport=self.fake.transport)
        self.addCleanup(client.close)

        order = create_order(client, amount=Decimal("10.00"))

        self.assertTrue(order.live)

    def test_failure_leaves_the_row_discoverable(self):
        """The outcome is unknown, so the row must not vanish."""
        self.fake.queue(ORDERS_PATH, *[httpx.Response(500, json={"name": "INTERNAL"})] * 3)

        with self.assertRaises(PayPalServerError):
            create_order(self.client, amount=Decimal("10.00"))

        pending = PayPalOrder.objects.pending()
        self.assertEqual(len(pending), 1)
        self.assertIsNone(pending[0].paypal_id)
        self.assertIsNotNone(pending[0].request_id)

    def test_the_persisted_key_makes_the_create_retryable(self):
        """1 attempt + 2 retries, all carrying the same key — PayPal dedupes."""
        self.fake.queue(
            ORDERS_PATH,
            httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}),
            httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}),
            httpx.Response(201, json=order_response()),
        )

        order = create_order(self.client, amount=Decimal("10.00"))

        keys = [r.headers["paypal-request-id"] for r in self.fake.api_requests()]
        self.assertEqual(keys, [order.request_id] * 3)
        self.assertEqual(order.paypal_id, PAYPAL_ID)

    def test_float_amounts_are_refused_before_any_call(self):
        with self.assertRaises(PayPalAmountError):
            create_order(self.client, amount=10.0)

        self.assertEqual(self.fake.requests, [])
        self.assertEqual(PayPalOrder.objects.count(), 0)


class PurchaseUnitsTests(ClientMixin, TestCase):
    def test_custom_units_are_forwarded(self):
        self.fake.queue(ORDERS_PATH, httpx.Response(201, json=order_response()))
        units = [
            {"reference_id": "a", "amount": {"currency_code": "EUR", "value": "4.00"}},
            {"reference_id": "b", "amount": {"currency_code": "EUR", "value": "6.00"}},
        ]

        create_order(self.client, amount=Decimal("10.00"), purchase_units=units)

        body = sent_body(self.fake.api_requests()[0])
        self.assertEqual([u["reference_id"] for u in body["purchase_units"]], ["a", "b"])

    def test_total_mismatch_is_refused_before_any_call(self):
        """Otherwise the row says EUR 10 while the buyer is charged EUR 100."""
        units = [{"amount": {"currency_code": "EUR", "value": "100.00"}}]

        with self.assertRaisesMessage(PayPalAmountError, "does not match the recorded amount"):
            create_order(self.client, amount=Decimal("10.00"), purchase_units=units)

        self.assertEqual(self.fake.requests, [])
        self.assertEqual(PayPalOrder.objects.count(), 0)

    def test_validation_is_skipped_for_units_it_cannot_read(self):
        """A partial check must not become a false rejection."""
        cases = [
            [{"reference_id": "no-amount"}],
            [{"amount": {"currency_code": "USD", "value": "10.00"}}],
            [{"amount": {"currency_code": "EUR", "value": "oops"}}],
            ["not-a-dict"],
        ]
        for index, units in enumerate(cases):
            with self.subTest(units=units):
                # A distinct PayPal id per iteration: they are unique per row.
                self.fake.queue(
                    ORDERS_PATH,
                    httpx.Response(201, json=order_response(paypal_id=f"5O-{index}")),
                )
                order = create_order(
                    self.client, amount=Decimal("10.00"), purchase_units=units
                )
                self.assertEqual(order.paypal_id, f"5O-{index}")


class RefreshAndFetchTests(ClientMixin, TestCase):
    def test_refresh_updates_the_row(self):
        order = PayPalOrder.objects.start(amount=Decimal("10.00"), currency="EUR", live=False)
        order.update_from_payload(order_response())
        self.fake.queue(
            f"{ORDERS_PATH}/{PAYPAL_ID}", httpx.Response(200, json=order_response("APPROVED"))
        )

        refresh_order(self.client, order)

        order.refresh_from_db()
        self.assertEqual(order.status, PayPalOrder.Status.APPROVED)

    def test_refresh_needs_a_confirmed_order(self):
        order = PayPalOrder.objects.start(amount=Decimal("10.00"), currency="EUR", live=False)

        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            refresh_order(self.client, order)

        self.assertEqual(self.fake.requests, [])

    def test_fetch_returns_the_raw_payload(self):
        self.fake.queue(f"{ORDERS_PATH}/{PAYPAL_ID}", httpx.Response(200, json=order_response()))

        self.assertEqual(fetch_order(self.client, PAYPAL_ID)["id"], PAYPAL_ID)


class CaptureOrderTests(ClientMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False
        )
        self.order.update_from_payload(order_response("APPROVED"))
        self.capture_path = f"{ORDERS_PATH}/{PAYPAL_ID}/capture"

    def test_successful_capture(self):
        self.fake.queue(self.capture_path, httpx.Response(201, json=capture_response()))

        capture = capture_order(self.client, self.order)

        self.assertTrue(capture.is_successful)
        self.assertEqual(capture.paypal_id, "3C679366HH908993F")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, PayPalOrder.Status.COMPLETED)

    def test_persisted_capture_key_is_sent(self):
        self.fake.queue(self.capture_path, httpx.Response(201, json=capture_response()))

        capture = capture_order(self.client, self.order)

        request = self.fake.api_requests(self.capture_path)[0]
        self.assertEqual(request.headers["paypal-request-id"], capture.request_id)
        self.assertEqual(
            request.headers["paypal-request-id"],
            f"order:{self.order.pk}:capture:{capture.pk}",
        )

    def test_recovery_reuses_the_key_of_the_interrupted_attempt(self):
        """Simulates a crash after the row was written but before/while calling."""
        interrupted = self.order.start_capture()
        self.fake.queue(self.capture_path, httpx.Response(201, json=capture_response()))

        capture = capture_order(self.client, self.order)

        self.assertEqual(capture.pk, interrupted.pk)
        self.assertEqual(
            self.fake.api_requests(self.capture_path)[0].headers["paypal-request-id"],
            interrupted.request_id,
        )
        self.assertEqual(self.order.captures.count(), 1)

    def test_a_new_attempt_after_a_decline_uses_a_new_key(self):
        self.fake.queue(
            self.capture_path,
            httpx.Response(201, json=capture_response("DECLINED", capture_id="CAP-1")),
            httpx.Response(201, json=capture_response("COMPLETED", capture_id="CAP-2")),
        )

        declined = capture_order(self.client, self.order)
        retried = capture_order(self.client, self.order)

        self.assertNotEqual(declined.request_id, retried.request_id)
        keys = [r.headers["paypal-request-id"] for r in self.fake.api_requests(self.capture_path)]
        self.assertEqual(len(set(keys)), 2)

    def test_partial_capture_sends_amount_and_final_capture(self):
        self.fake.queue(
            self.capture_path, httpx.Response(201, json=capture_response(value="4.00"))
        )

        capture = capture_order(
            self.client, self.order, amount=Decimal("4.00"), final_capture=False
        )

        body = sent_body(self.fake.api_requests(self.capture_path)[0])
        self.assertEqual(body["amount"], {"currency_code": "EUR", "value": "4.00"})
        self.assertIs(body["final_capture"], False)
        self.assertEqual(capture.amount, Decimal("4.00"))

    def test_full_capture_sends_no_body(self):
        self.fake.queue(self.capture_path, httpx.Response(201, json=capture_response()))

        capture_order(self.client, self.order)

        self.assertEqual(self.fake.api_requests(self.capture_path)[0].read(), b"")

    def test_capture_needs_a_confirmed_order(self):
        order = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=False)

        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            capture_order(self.client, order)

        self.assertEqual(self.fake.requests, [])
        self.assertEqual(Capture.objects.count(), 0)

    def test_capture_is_found_past_units_that_do_not_hold_it(self):
        """Multi-unit orders: only one unit carries the payments block."""
        payload = {
            "id": PAYPAL_ID,
            "status": "COMPLETED",
            "purchase_units": [
                "not-a-dict",
                {"reference_id": "no-payments"},
                {"payments": {}},
                {"payments": {"captures": []}},
                {
                    "payments": {
                        "captures": [{"id": CAPTURE_ID, "status": "COMPLETED"}]
                    }
                },
            ],
        }
        self.fake.queue(self.capture_path, httpx.Response(201, json=payload))

        capture = capture_order(self.client, self.order)

        self.assertEqual(capture.paypal_id, CAPTURE_ID)
        self.assertTrue(capture.is_successful)

    def test_response_without_a_capture_stays_unconfirmed(self):
        """Money may have moved: guessing success would be worse than not knowing."""
        self.fake.queue(self.capture_path, httpx.Response(201, json={"id": PAYPAL_ID, "status": "COMPLETED"}))

        with self.assertLogs("paypal_checkout.orders", level="WARNING") as logs:
            capture = capture_order(self.client, self.order)

        self.assertEqual(capture.status, Capture.Status.INITIATED)
        self.assertEqual(capture.raw["id"], PAYPAL_ID)
        self.assertEqual(logs.records[0].paypal_issue, "capture_not_in_response")
        self.assertIsNotNone(self.order.pending_capture())


class CaptureSignalTests(ClientMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.shop_order = ShopOrder.objects.create(reference="ORD-1", total=Decimal("10.00"))
        self.order = PayPalOrder.objects.start(
            amount=Decimal("10.00"),
            currency="EUR",
            live=False,
            target=self.shop_order,
        )
        self.order.update_from_payload(order_response("APPROVED"))
        self.capture_path = f"{ORDERS_PATH}/{PAYPAL_ID}/capture"

    def test_completed_capture_sends_payment_captured(self):
        self.fake.queue(self.capture_path, httpx.Response(201, json=capture_response()))

        with catch_signal(payment_captured) as received:
            capture = capture_order(self.client, self.order)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["sender"], Capture)
        self.assertEqual(received[0]["capture"], capture)
        self.assertEqual(received[0]["order"], self.order)
        self.assertEqual(received[0]["target"], self.shop_order)

    def test_declined_capture_sends_payment_denied(self):
        self.fake.queue(self.capture_path, httpx.Response(201, json=capture_response("DECLINED")))

        with catch_signal(payment_denied) as denied:
            with catch_signal(payment_captured) as captured:
                capture_order(self.client, self.order)

        self.assertEqual(len(denied), 1)
        self.assertEqual(captured, [])

    def test_failed_capture_sends_payment_denied(self):
        self.fake.queue(self.capture_path, httpx.Response(201, json=capture_response("FAILED")))

        with catch_signal(payment_denied) as denied:
            capture_order(self.client, self.order)

        self.assertEqual(len(denied), 1)

    def test_pending_capture_sends_nothing(self):
        """PENDING is not an outcome yet — the webhook will settle it (M3)."""
        self.fake.queue(self.capture_path, httpx.Response(201, json=capture_response("PENDING")))

        with catch_signal(payment_captured) as captured:
            with catch_signal(payment_denied) as denied:
                capture = capture_order(self.client, self.order)

        self.assertEqual(capture.status, Capture.Status.PENDING)
        self.assertEqual(captured, [])
        self.assertEqual(denied, [])


class StrictModeTests(ClientMixin, TestCase):
    """The wrappers must satisfy strict mode by construction."""

    def setUp(self):
        super().setUp()
        self.strict = PayPalClient(
            make_config(strict_idempotency=True), transport=self.fake.transport
        )
        self.addCleanup(self.strict.close)

    def test_create_and_capture_pass_strict_mode_silently(self):
        self.fake.queue(ORDERS_PATH, httpx.Response(201, json=order_response("APPROVED")))
        self.fake.queue(
            f"{ORDERS_PATH}/{PAYPAL_ID}/capture", httpx.Response(201, json=capture_response())
        )

        with self.assertNoLogs("paypal_checkout.client", level="WARNING"):
            order = create_order(self.strict, amount=Decimal("10.00"))
            capture = capture_order(self.strict, order)

        self.assertTrue(capture.is_successful)
