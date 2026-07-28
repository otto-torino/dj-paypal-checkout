"""Handler dispatch and the webhook endpoint."""

import json
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings

from paypal_checkout.exceptions import PayPalWebhookNotReady
from paypal_checkout.models import Authorization, Capture, PayPalOrder, WebhookEvent
from paypal_checkout.signals import payment_captured, payment_denied, payment_refunded
from paypal_checkout.webhooks.handlers import (
    dispatch,
    get_handlers,
    register_handler,
    registered_event_types,
)

from .support import WebhookSigner, catch_signal, make_config
from .test_app.models import ShopOrder

ORDER_ID = "5O190127TN364715T"
CAPTURE_ID = "3C679366HH908993F"
AUTH_ID = "0VF52814937998046"

WEBHOOK_SETTINGS = {
    "CLIENT_ID": "test-client-id",
    "CLIENT_SECRET": "test-client-secret",
    "WEBHOOK_ID": "WH-TEST-1",
}


def event_payload(
    event_type="PAYMENT.CAPTURE.COMPLETED",
    *,
    event_id="WH-EVT-1",
    resource=None,
    resource_type="capture",
    create_time="2026-07-28T10:00:00Z",
):
    if resource is None:
        resource = {
            "id": CAPTURE_ID,
            "status": "COMPLETED",
            "amount": {"currency_code": "EUR", "value": "10.00"},
            "supplementary_data": {"related_ids": {"order_id": ORDER_ID}},
        }
    return {
        "id": event_id,
        "event_type": event_type,
        "resource_type": resource_type,
        "summary": "test event",
        "create_time": create_time,
        "resource": resource,
    }


def store_event(payload, **kwargs):
    defaults = {
        "event_type": payload["event_type"],
        "resource_type": payload.get("resource_type", ""),
        "payload": payload,
        "transmission_id": "TR-1",
    }
    defaults.update(kwargs)
    return WebhookEvent.objects.create(event_id=payload["id"], **defaults)


class OrderFixtureMixin:
    def make_order(self, *, with_capture=True, target=None):
        order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False, target=target
        )
        order.update_from_payload({"id": ORDER_ID, "status": "APPROVED"})
        capture = None
        if with_capture:
            capture = order.start_capture()
            capture.update_from_payload({"id": CAPTURE_ID, "status": "PENDING"})
        return order, capture


class RegistryTests(TestCase):
    def test_builtin_event_types_are_registered(self):
        for event_type in (
            "PAYMENT.CAPTURE.COMPLETED",
            "PAYMENT.CAPTURE.DENIED",
            "PAYMENT.CAPTURE.REFUNDED",
            "CHECKOUT.ORDER.APPROVED",
            "PAYMENT.AUTHORIZATION.CREATED",
        ):
            with self.subTest(event_type=event_type):
                self.assertTrue(get_handlers(event_type))

    def test_registered_event_types_is_sorted(self):
        types = registered_event_types()
        self.assertEqual(types, sorted(types))

    def test_a_custom_handler_can_be_added(self):
        seen = []

        @register_handler("TEST.CUSTOM.EVENT")
        def handler(event):
            seen.append(event.event_id)

        self.addCleanup(get_handlers("TEST.CUSTOM.EVENT").clear)
        event = store_event(event_payload("TEST.CUSTOM.EVENT"))

        self.assertEqual(dispatch(event), 1)
        self.assertEqual(seen, ["WH-EVT-1"])

    def test_an_unhandled_event_is_not_an_error(self):
        event = store_event(event_payload("SOMETHING.WE.DO.NOT.HANDLE"))
        self.assertEqual(dispatch(event), 0)


class CaptureHandlerTests(OrderFixtureMixin, TestCase):
    def test_completed_updates_the_capture_and_sends_the_signal(self):
        shop_order = ShopOrder.objects.create(reference="ORD-1", total=Decimal("10.00"))
        order, capture = self.make_order(target=shop_order)
        event = store_event(event_payload())

        with catch_signal(payment_captured) as received:
            dispatch(event)

        capture.refresh_from_db()
        self.assertTrue(capture.is_successful)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["capture"], capture)
        self.assertEqual(received[0]["target"], shop_order)

    def test_denied_sends_payment_denied(self):
        order, capture = self.make_order()
        resource = {"id": CAPTURE_ID, "status": "DECLINED"}
        event = store_event(event_payload("PAYMENT.CAPTURE.DENIED", resource=resource))

        with catch_signal(payment_denied) as received:
            dispatch(event)

        capture.refresh_from_db()
        self.assertEqual(capture.status, Capture.Status.DECLINED)
        self.assertEqual(len(received), 1)

    def test_pending_updates_without_signalling(self):
        order, capture = self.make_order()
        resource = {"id": CAPTURE_ID, "status": "PENDING"}
        event = store_event(event_payload("PAYMENT.CAPTURE.PENDING", resource=resource))

        with catch_signal(payment_captured) as captured:
            with catch_signal(payment_denied) as denied:
                dispatch(event)

        self.assertEqual(captured, [])
        self.assertEqual(denied, [])

    def test_refunded_sends_payment_refunded(self):
        order, capture = self.make_order()
        resource = {"id": CAPTURE_ID, "status": "REFUNDED"}
        event = store_event(event_payload("PAYMENT.CAPTURE.REFUNDED", resource=resource))

        with catch_signal(payment_refunded) as received:
            dispatch(event)

        capture.refresh_from_db()
        self.assertEqual(capture.status, Capture.Status.REFUNDED)
        self.assertEqual(len(received), 1)

    def test_processing_the_same_event_twice_is_harmless(self):
        """Handlers have to be idempotent: PayPal retries."""
        order, capture = self.make_order()
        event = store_event(event_payload())

        with catch_signal(payment_captured) as received:
            dispatch(event)
            dispatch(event)

        capture.refresh_from_db()
        self.assertTrue(capture.is_successful)
        self.assertEqual(len(received), 2, "receivers see it twice; they must cope")

    def test_a_capture_we_do_not_know_is_ignored(self):
        """Another integration on the same PayPal account is not our business."""
        event = store_event(event_payload())

        self.assertEqual(dispatch(event), 1)
        self.assertEqual(Capture.objects.count(), 0)

    def test_a_capture_of_a_known_order_asks_for_a_retry(self):
        """The webhook overtook our own capture response — do not drop it."""
        self.make_order(with_capture=False)
        event = store_event(event_payload())

        with self.assertRaises(PayPalWebhookNotReady):
            dispatch(event)

    def test_a_resource_without_an_id_is_ignored(self):
        event = store_event(event_payload(resource={"status": "COMPLETED"}))
        self.assertEqual(dispatch(event), 1)

    def test_unknown_captures_are_ignored_for_every_outcome(self):
        """No local row and no known order: someone else's payment."""
        cases = {
            "PAYMENT.CAPTURE.DENIED": "DECLINED",
            "PAYMENT.CAPTURE.PENDING": "PENDING",
            "PAYMENT.CAPTURE.REFUNDED": "REFUNDED",
        }
        for event_type, status in cases.items():
            with self.subTest(event_type=event_type):
                event = store_event(
                    event_payload(
                        event_type,
                        event_id=f"WH-{event_type}",
                        resource={"id": "CAP-FOREIGN", "status": status},
                    )
                )
                with catch_signal(payment_denied) as denied:
                    with catch_signal(payment_refunded) as refunded:
                        dispatch(event)
                self.assertEqual(denied, [])
                self.assertEqual(refunded, [])
        self.assertEqual(Capture.objects.count(), 0)

    def test_a_non_dict_resource_is_tolerated(self):
        event = store_event(event_payload(resource=None))
        event.payload["resource"] = "not-a-dict"
        event.save(update_fields=["payload"])

        self.assertEqual(event.resource, {})
        dispatch(event)


class OrderHandlerTests(OrderFixtureMixin, TestCase):
    def test_approved_updates_the_order(self):
        order, _ = self.make_order(with_capture=False)
        resource = {"id": ORDER_ID, "status": "APPROVED"}
        event = store_event(
            event_payload("CHECKOUT.ORDER.APPROVED", resource=resource, resource_type="checkout-order")
        )

        dispatch(event)

        order.refresh_from_db()
        self.assertEqual(order.status, PayPalOrder.Status.APPROVED)

    def test_an_unknown_order_is_ignored(self):
        event = store_event(
            event_payload("CHECKOUT.ORDER.APPROVED", resource={"id": "UNKNOWN", "status": "APPROVED"})
        )

        dispatch(event)

        self.assertEqual(PayPalOrder.objects.count(), 0)

    def test_a_resource_without_an_id_is_ignored(self):
        event = store_event(event_payload("CHECKOUT.ORDER.APPROVED", resource={"status": "APPROVED"}))
        dispatch(event)


class AuthorizationHandlerTests(OrderFixtureMixin, TestCase):
    def test_voided_updates_the_authorization(self):
        order, _ = self.make_order(with_capture=False)
        authorization = order.start_authorization()
        authorization.update_from_payload({"id": AUTH_ID, "status": "CREATED"})
        event = store_event(
            event_payload(
                "PAYMENT.AUTHORIZATION.VOIDED",
                resource={"id": AUTH_ID, "status": "VOIDED"},
                resource_type="authorization",
            )
        )

        dispatch(event)

        authorization.refresh_from_db()
        self.assertEqual(authorization.status, Authorization.Status.VOIDED)

    def test_an_unknown_authorization_is_ignored(self):
        event = store_event(
            event_payload("PAYMENT.AUTHORIZATION.CREATED", resource={"id": "UNKNOWN"})
        )
        dispatch(event)
        self.assertEqual(Authorization.objects.count(), 0)

    def test_a_resource_without_an_id_is_ignored(self):
        event = store_event(event_payload("PAYMENT.AUTHORIZATION.CREATED", resource={}))
        dispatch(event)


@override_settings(PAYPAL=WEBHOOK_SETTINGS)
class ProcessWebhookViewTests(OrderFixtureMixin, TestCase):
    url = "/paypal/webhook/"

    def setUp(self):
        cache.clear()
        self.signer = WebhookSigner(webhook_id="WH-TEST-1")
        self.signer.prime_certificate_cache(make_config())

    def deliver(self, payload=None, *, body=None, headers=None, **header_kwargs):
        if body is None:
            body = json.dumps(payload or event_payload()).encode()
        if headers is None:
            headers = self.signer.headers(body, **header_kwargs)
        return self.client.post(
            self.url, data=body, content_type="application/json", headers=headers
        )

    def test_a_valid_webhook_is_stored_and_processed(self):
        order, capture = self.make_order()

        response = self.deliver()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "processed")
        event = WebhookEvent.objects.get(event_id="WH-EVT-1")
        self.assertTrue(event.is_processed)
        self.assertEqual(event.event_type, "PAYMENT.CAPTURE.COMPLETED")
        self.assertEqual(event.transmission_id, "TR-1")
        self.assertIsNotNone(event.occurred_at)
        capture.refresh_from_db()
        self.assertTrue(capture.is_successful)

    def test_no_csrf_token_is_required(self):
        self.make_order()
        client = self.client_class(enforce_csrf_checks=True)
        body = json.dumps(event_payload()).encode()

        response = client.post(
            self.url,
            data=body,
            content_type="application/json",
            headers=self.signer.headers(body),
        )

        self.assertEqual(response.status_code, 200)

    def test_a_duplicate_delivery_is_not_processed_again(self):
        order, capture = self.make_order()

        first = self.deliver()
        with catch_signal(payment_captured) as received:
            second = self.deliver()

        self.assertEqual(first.json()["status"], "processed")
        self.assertEqual(second.json()["status"], "duplicate")
        self.assertEqual(received, [], "a duplicate must not re-run handlers")
        self.assertEqual(WebhookEvent.objects.count(), 1)

    def test_an_unprocessed_event_is_retried_not_skipped(self):
        """Stored-but-unfinished is unfinished work, not a duplicate."""
        order, capture = self.make_order()
        store_event(event_payload(), processed_at=None, last_error="boom")

        response = self.deliver()

        self.assertEqual(response.json()["status"], "processed")
        capture.refresh_from_db()
        self.assertTrue(capture.is_successful)
        event = WebhookEvent.objects.get(event_id="WH-EVT-1")
        self.assertEqual(event.last_error, "")

    def test_missing_signature_headers_are_rejected(self):
        body = json.dumps(event_payload()).encode()

        response = self.client.post(self.url, data=body, content_type="application/json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(WebhookEvent.objects.count(), 0)

    def test_a_forged_signature_is_rejected_and_stored_nowhere(self):
        genuine = self.signer.headers(json.dumps(event_payload()).encode())
        forged = dict(genuine)
        forged["PAYPAL-TRANSMISSION-SIG"] = "Zm9yZ2Vk"

        response = self.deliver(headers=forged)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(WebhookEvent.objects.count(), 0)

    def test_a_tampered_body_is_rejected(self):
        payload = event_payload()
        body = json.dumps(payload).encode()
        headers = self.signer.headers(body)
        tampered = json.dumps({**payload, "summary": "changed"}).encode()

        response = self.deliver(body=tampered, headers=headers)

        self.assertEqual(response.status_code, 400)

    def test_a_foreign_certificate_url_is_rejected(self):
        body = json.dumps(event_payload()).encode()
        headers = self.signer.headers(body)
        headers["PAYPAL-CERT-URL"] = "https://evil.example/cert"

        response = self.deliver(body=body, headers=headers)

        self.assertEqual(response.status_code, 400)

    def test_an_unreadable_body_is_rejected(self):
        response = self.deliver(body=b"not json")

        self.assertEqual(response.status_code, 400)

    def test_a_json_array_body_is_rejected(self):
        response = self.deliver(body=b"[1, 2, 3]")

        self.assertEqual(response.status_code, 400)

    def test_an_event_without_an_id_is_rejected(self):
        payload = event_payload()
        del payload["id"]

        response = self.deliver(payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(WebhookEvent.objects.count(), 0)

    def test_a_handler_failure_asks_paypal_to_retry(self):
        """500 keeps the event unprocessed so the retry actually re-runs it."""
        self.make_order(with_capture=False)  # order known, capture missing

        response = self.deliver()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["status"], "retry")
        event = WebhookEvent.objects.get(event_id="WH-EVT-1")
        self.assertFalse(event.is_processed)
        self.assertIn("not stored yet", event.last_error)

    def test_a_retry_after_a_failure_succeeds_once_the_row_exists(self):
        order, _ = self.make_order(with_capture=False)
        self.assertEqual(self.deliver().status_code, 500)

        capture = order.start_capture()
        capture.update_from_payload({"id": CAPTURE_ID, "status": "PENDING"})
        response = self.deliver()

        self.assertEqual(response.status_code, 200)
        capture.refresh_from_db()
        self.assertTrue(capture.is_successful)

    def test_an_unhandled_event_type_is_stored_and_acknowledged(self):
        response = self.deliver(event_payload("BILLING.SUBSCRIPTION.ACTIVATED"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["handlers"], 0)
        self.assertTrue(WebhookEvent.objects.get(event_id="WH-EVT-1").is_processed)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_the_environment_is_recorded_on_the_event(self):
        self.make_order()

        self.deliver()

        self.assertFalse(WebhookEvent.objects.get(event_id="WH-EVT-1").live)


class WebhookEventModelTests(TestCase):
    def test_mark_processed_and_failed(self):
        event = store_event(event_payload())

        self.assertFalse(event.is_processed)
        event.mark_failed(ValueError("nope"))
        self.assertFalse(event.is_processed)
        self.assertEqual(event.last_error, "nope")

        event.mark_processed()
        self.assertTrue(event.is_processed)
        self.assertEqual(event.last_error, "")

    def test_long_errors_are_truncated(self):
        event = store_event(event_payload())
        event.mark_failed("x" * 5000)
        self.assertEqual(len(event.last_error), 2000)

    def test_str(self):
        event = store_event(event_payload())
        self.assertEqual(str(event), "PAYMENT.CAPTURE.COMPLETED WH-EVT-1")
