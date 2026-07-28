"""Handler dispatch and the webhook endpoint."""

import json
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError
from django.utils import timezone
from django.test import TestCase, TransactionTestCase, override_settings

from paypal_checkout.exceptions import PayPalWebhookNotReady
from paypal_checkout.models import (
    Authorization,
    Capture,
    PayPalOrder,
    Refund,
    WebhookEvent,
)
from paypal_checkout.signals import payment_captured, payment_denied, payment_refunded
from paypal_checkout.webhooks.views import ProcessWebhookView
from paypal_checkout.webhooks.handlers import (
    _HANDLERS,
    dispatch,
    get_handlers,
    register_handler,
    registered_event_types,
    unregister_handlers,
)

from .support import WebhookSigner, catch_signal, make_config
from .test_app.models import ShopOrder

ORDER_ID = "5O190127TN364715T"
CAPTURE_ID = "3C679366HH908993F"
AUTH_ID = "0VF52814937998046"
REFUND_ID = "1JU08902781691411"

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


def refund_resource(*, refund_id=None, value="10.00", status="COMPLETED"):
    """A PAYMENT.CAPTURE.REFUNDED resource: the *refund*, not the capture."""
    return {
        "id": refund_id or REFUND_ID,
        "status": status,
        "amount": {"currency_code": "EUR", "value": value},
        "supplementary_data": {
            "related_ids": {"order_id": ORDER_ID, "capture_id": CAPTURE_ID}
        },
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
        self.addCleanup(unregister_handlers, "TEST.CUSTOM.EVENT")

        @register_handler("TEST.CUSTOM.EVENT")
        def handler(event):
            seen.append(event.event_id)

        event = store_event(event_payload("TEST.CUSTOM.EVENT"))

        self.assertEqual(dispatch(event), 1)
        self.assertEqual(seen, ["WH-EVT-1"])

    def test_handlers_can_be_unregistered(self):
        removed = unregister_handlers("PAYMENT.CAPTURE.PENDING")
        self.addCleanup(_HANDLERS.__setitem__, "PAYMENT.CAPTURE.PENDING", removed)

        self.assertTrue(removed)
        self.assertEqual(get_handlers("PAYMENT.CAPTURE.PENDING"), [])
        self.assertEqual(unregister_handlers("NOT.REGISTERED"), [])

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

    def test_refunded_adopts_the_refund_and_signals(self):
        """The resource of this event is the *refund*, not the capture."""
        order, capture = self.make_order()
        capture.update_from_payload({"id": CAPTURE_ID, "status": "COMPLETED"})
        event = store_event(event_payload("PAYMENT.CAPTURE.REFUNDED", resource=refund_resource()))

        with catch_signal(payment_refunded) as received:
            dispatch(event)

        capture.refresh_from_db()
        self.assertEqual(capture.status, Capture.Status.REFUNDED)
        refund = Refund.objects.get(paypal_id=REFUND_ID)
        self.assertTrue(refund.is_successful)
        self.assertIsNone(refund.request_id, "we did not initiate this one")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["refund"], refund)

    def test_a_partial_refund_marks_the_capture_partially_refunded(self):
        order, capture = self.make_order()
        capture.update_from_payload({"id": CAPTURE_ID, "status": "COMPLETED"})
        event = store_event(
            event_payload("PAYMENT.CAPTURE.REFUNDED", resource=refund_resource(value="4.00"))
        )

        dispatch(event)

        capture.refresh_from_db()
        self.assertEqual(capture.status, Capture.Status.PARTIALLY_REFUNDED)
        self.assertEqual(capture.refunded_amount, Decimal("4.00"))

    def test_a_refund_we_initiated_is_updated_not_duplicated(self):
        order, capture = self.make_order()
        capture.update_from_payload({"id": CAPTURE_ID, "status": "COMPLETED"})
        refund = capture.start_refund()
        refund.update_from_payload({"id": REFUND_ID, "status": "PENDING"})
        event = store_event(event_payload("PAYMENT.CAPTURE.REFUNDED", resource=refund_resource()))

        dispatch(event)

        refund.refresh_from_db()
        self.assertTrue(refund.is_successful)
        self.assertEqual(capture.refunds.count(), 1)
        self.assertIsNotNone(refund.request_id, "ours keeps its key")

    def test_the_capture_can_be_found_through_the_up_link(self):
        """Older payloads may not carry related_ids."""
        order, capture = self.make_order()
        capture.update_from_payload({"id": CAPTURE_ID, "status": "COMPLETED"})
        resource = refund_resource()
        del resource["supplementary_data"]
        resource["links"] = [
            {"rel": "self", "href": f"https://api.paypal.com/v2/payments/refunds/{REFUND_ID}"},
            {"rel": "up", "href": f"https://api.paypal.com/v2/payments/captures/{CAPTURE_ID}/"},
        ]
        event = store_event(event_payload("PAYMENT.CAPTURE.REFUNDED", resource=resource))

        dispatch(event)

        self.assertTrue(Refund.objects.get(paypal_id=REFUND_ID).is_successful)

    def test_links_without_a_capture_url_are_skipped(self):
        order, capture = self.make_order()
        resource = refund_resource()
        del resource["supplementary_data"]
        resource["links"] = [
            {"rel": "self", "href": "https://api.paypal.com/v2/payments/refunds/X"},
            "not-a-dict",
            {"rel": "up", "href": "https://api.paypal.com/v2/checkout/orders/Y"},
        ]
        event = store_event(event_payload("PAYMENT.CAPTURE.REFUNDED", resource=resource))

        with catch_signal(payment_refunded) as received:
            dispatch(event)

        self.assertEqual(received, [], "no capture could be identified")
        self.assertEqual(Refund.objects.count(), 0)

    def test_a_refund_amount_that_is_not_an_object_falls_back(self):
        order, capture = self.make_order()
        resource = refund_resource()
        resource["amount"] = "10.00"
        event = store_event(event_payload("PAYMENT.CAPTURE.REFUNDED", resource=resource))

        dispatch(event)

        self.assertEqual(Refund.objects.get(paypal_id=REFUND_ID).amount, capture.amount)

    def test_an_unreadable_refund_amount_falls_back_to_the_capture(self):
        order, capture = self.make_order()
        resource = refund_resource()
        resource["amount"] = {"currency_code": "EUR", "value": "not-a-number"}
        event = store_event(event_payload("PAYMENT.CAPTURE.REFUNDED", resource=resource))

        with self.assertLogs("paypal_checkout.webhooks.handlers", level="WARNING"):
            dispatch(event)

        self.assertEqual(Refund.objects.get(paypal_id=REFUND_ID).amount, capture.amount)

    def test_a_refund_without_an_id_is_not_adopted(self):
        order, capture = self.make_order()
        resource = refund_resource()
        del resource["id"]
        event = store_event(event_payload("PAYMENT.CAPTURE.REFUNDED", resource=resource))

        with catch_signal(payment_refunded) as received:
            dispatch(event)

        self.assertEqual(Refund.objects.count(), 0)
        self.assertIsNone(received[0]["refund"])

    def test_a_foreign_refund_is_ignored(self):
        event = store_event(event_payload("PAYMENT.CAPTURE.REFUNDED", resource=refund_resource()))

        with catch_signal(payment_refunded) as received:
            dispatch(event)

        self.assertEqual(received, [])
        self.assertEqual(Refund.objects.count(), 0)

    def test_a_refund_of_a_known_order_asks_for_a_retry(self):
        self.make_order(with_capture=False)
        event = store_event(event_payload("PAYMENT.CAPTURE.REFUNDED", resource=refund_resource()))

        with self.assertRaises(PayPalWebhookNotReady):
            dispatch(event)

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

    def test_a_stored_payload_is_refreshed_when_the_retry_differs(self):
        """A retry can carry a newer resource state than the first delivery."""
        order, capture = self.make_order()
        stale = event_payload()
        stale["resource"] = {**stale["resource"], "status": "PENDING"}
        stale["summary"] = "older"
        store_event(stale)

        self.deliver()

        event = WebhookEvent.objects.get(event_id="WH-EVT-1")
        self.assertEqual(event.summary, "test event")
        self.assertEqual(event.resource["status"], "COMPLETED")

    def test_an_identical_redelivery_is_not_rewritten(self):
        order, capture = self.make_order()
        payload = event_payload()
        event = store_event(payload)
        before = WebhookEvent.objects.values_list("payload", flat=True).get(pk=event.pk)

        self.deliver(payload)

        self.assertEqual(
            WebhookEvent.objects.values_list("payload", flat=True).get(pk=event.pk), before
        )

    def test_losing_the_claim_after_reading_the_row_is_a_duplicate(self):
        """The row was claimed by a rival between our read and our UPDATE."""
        self.make_order()
        event = store_event(event_payload())
        WebhookEvent.objects.filter(pk=event.pk).update(processed_at=timezone.now())
        # A stale in-memory copy, exactly what a rival's commit leaves us holding.
        stale = WebhookEvent.objects.get(pk=event.pk)
        stale.processed_at = None

        with mock.patch.object(ProcessWebhookView, "_store", return_value=stale):
            with catch_signal(payment_captured) as received:
                response = self.deliver()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "duplicate")
        self.assertEqual(received, [], "the handlers must not run")

    def test_losing_the_race_to_create_the_row_is_survivable(self):
        self.make_order()
        payload = event_payload()
        store_event(payload)

        with mock.patch.object(
            WebhookEvent.objects, "get_or_create", side_effect=IntegrityError("dup")
        ):
            response = self.deliver(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "processed")
        self.assertEqual(WebhookEvent.objects.count(), 1)

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


@override_settings(PAYPAL=WEBHOOK_SETTINGS)
class ConcurrentDeliveryTests(OrderFixtureMixin, TransactionTestCase):
    """Two deliveries of one event must not both run the handlers.

    ``TransactionTestCase`` because the threaded test needs committed rows to be
    visible across connections.
    """

    url = "/paypal/webhook/"
    reset_sequences = True

    def setUp(self):
        cache.clear()
        self.signer = WebhookSigner(webhook_id="WH-TEST-1")
        self.signer.prime_certificate_cache(make_config())
        self.calls = []
        self.addCleanup(unregister_handlers, "TEST.CONCURRENT")

    def register_counting_handler(self, before_return=None):
        @register_handler("TEST.CONCURRENT")
        def handler(event):
            self.calls.append(event.event_id)
            if before_return is not None:
                before_return()

        return handler

    def deliver(self, client=None, payload=None):
        body = json.dumps(payload or event_payload("TEST.CONCURRENT")).encode()
        return (client or self.client).post(
            self.url,
            data=body,
            content_type="application/json",
            headers=self.signer.headers(body),
        )

    def test_the_claim_is_taken_before_the_handlers_run(self):
        """A delivery arriving mid-handler already sees the event claimed.

        Deterministic stand-in for the interleaving: the nested delivery runs
        while the first one holds its transaction open.
        """
        nested = {}

        def deliver_again():
            nested["response"] = self.deliver(client=self.client_class())

        self.register_counting_handler(before_return=deliver_again)

        first = self.deliver()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "processed")
        self.assertEqual(nested["response"].json()["status"], "duplicate")
        self.assertEqual(self.calls, ["WH-EVT-1"], "the handler ran exactly once")

    def test_two_threads_delivering_at_once_run_the_handler_once(self):
        import threading

        from django.db import connection

        self.register_counting_handler()
        barrier = threading.Barrier(2)
        outcomes = []

        def worker():
            try:
                barrier.wait(timeout=5)
                response = self.deliver(client=self.client_class())
                outcomes.append(response.json().get("status"))
            except Exception as exc:  # a lock timeout is a safe loser too
                outcomes.append(f"error:{type(exc).__name__}")
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        # The invariant that matters: the handler body ran exactly once, and
        # exactly one delivery was told it had processed the event. The loser is
        # allowed to be either "duplicate" or a lock error — asserting which
        # would make this a test of SQLite's locking rather than of the claim.
        self.assertEqual(len(self.calls), 1, f"handler ran {len(self.calls)}x: {outcomes}")
        self.assertEqual(outcomes.count("processed"), 1, outcomes)
        self.assertEqual(len(outcomes), 2, outcomes)
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertTrue(WebhookEvent.objects.get(event_id="WH-EVT-1").is_processed)

    def test_a_failed_handler_leaves_the_event_claimable_again(self):
        """The rollback must undo the claim, or the retry could never run."""
        state = {"fail": True}

        @register_handler("TEST.CONCURRENT")
        def handler(event):
            self.calls.append(event.event_id)
            if state["fail"]:
                raise RuntimeError("transient")

        self.assertEqual(self.deliver().status_code, 500)
        event = WebhookEvent.objects.get(event_id="WH-EVT-1")
        self.assertFalse(event.is_processed, "the claim must have been rolled back")
        self.assertEqual(event.last_error, "transient")

        state["fail"] = False
        self.assertEqual(self.deliver().json()["status"], "processed")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(WebhookEvent.objects.get(event_id="WH-EVT-1").last_error, "")

    def test_handler_effects_and_processed_commit_together(self):
        """No window where the work is applied but the event looks unprocessed."""
        observed = {}

        def observe():
            # Inside the handler's transaction the claim is already in place.
            observed["claimed"] = WebhookEvent.objects.filter(
                event_id="WH-EVT-1", processed_at__isnull=False
            ).exists()

        self.register_counting_handler(before_return=observe)

        self.deliver()

        self.assertTrue(observed["claimed"])
        self.assertTrue(WebhookEvent.objects.get(event_id="WH-EVT-1").is_processed)
