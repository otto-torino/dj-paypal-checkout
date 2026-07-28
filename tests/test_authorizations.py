"""The AUTHORIZE-intent flow: hold the money, capture it later."""

import json
from datetime import timezone
from decimal import Decimal

import httpx
from django.test import TestCase

from paypal_checkout.exceptions import PayPalError
from paypal_checkout.models import (
    Authorization,
    Capture,
    PayPalOrder,
    authorization_request_id,
)
from paypal_checkout.orders import (
    AUTHORIZATIONS_PATH,
    ORDERS_PATH,
    authorize_order,
    capture_authorization,
)
from paypal_checkout.signals import payment_captured

from .support import catch_signal
from .test_orders import CAPTURE_ID, PAYPAL_ID, ClientMixin, order_response

AUTH_ID = "0VF52814937998046"


def authorize_response(status="CREATED", value="10.00", auth_id=AUTH_ID, expiration=True):
    authorization = {
        "id": auth_id,
        "status": status,
        "amount": {"currency_code": "EUR", "value": value},
    }
    if expiration:
        authorization["expiration_time"] = "2026-08-26T12:00:00Z"
    return {
        "id": PAYPAL_ID,
        "status": "COMPLETED",
        "purchase_units": [{"payments": {"authorizations": [authorization]}}],
    }


def authorization_capture_response(status="COMPLETED", value="10.00", capture_id=CAPTURE_ID):
    """Capturing an authorization answers with the capture itself."""
    return {
        "id": capture_id,
        "status": status,
        "final_capture": True,
        "amount": {"currency_code": "EUR", "value": value},
    }


class RequestIdSchemeTests(TestCase):
    def test_authorization_key_is_per_attempt(self):
        self.assertEqual(authorization_request_id(42, 3), "order:42:authorize:3")
        self.assertNotEqual(
            authorization_request_id(42, 3), authorization_request_id(42, 4)
        )


class AuthorizeOrderTests(ClientMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.order = PayPalOrder.objects.start(
            amount=Decimal("10.00"),
            currency="EUR",
            live=False,
            intent=PayPalOrder.Intent.AUTHORIZE,
        )
        self.order.update_from_payload(order_response("APPROVED"))
        self.path = f"{ORDERS_PATH}/{PAYPAL_ID}/authorize"

    def test_authorization_row_is_created_and_updated(self):
        self.fake.queue(self.path, httpx.Response(201, json=authorize_response()))

        authorization = authorize_order(self.client, self.order)

        self.assertEqual(authorization.paypal_id, AUTH_ID)
        self.assertEqual(authorization.status, Authorization.Status.CREATED)
        self.assertEqual(authorization.amount, Decimal("10.00"))
        self.assertFalse(authorization.is_unconfirmed)

    def test_expiration_time_is_parsed(self):
        self.fake.queue(self.path, httpx.Response(201, json=authorize_response()))

        authorization = authorize_order(self.client, self.order)

        self.assertIsNotNone(authorization.expires_at)
        self.assertEqual(authorization.expires_at.tzinfo, timezone.utc)
        self.assertEqual(authorization.expires_at.year, 2026)

    def test_missing_expiration_is_tolerated(self):
        self.fake.queue(
            self.path, httpx.Response(201, json=authorize_response(expiration=False))
        )

        authorization = authorize_order(self.client, self.order)

        self.assertIsNone(authorization.expires_at)

    def test_unparseable_expiration_is_ignored(self):
        payload = authorize_response()
        payload["purchase_units"][0]["payments"]["authorizations"][0][
            "expiration_time"
        ] = "not-a-date"
        self.fake.queue(self.path, httpx.Response(201, json=payload))

        authorization = authorize_order(self.client, self.order)

        self.assertIsNone(authorization.expires_at)
        self.assertEqual(authorization.raw["expiration_time"], "not-a-date")

    def test_persisted_key_is_sent(self):
        self.fake.queue(self.path, httpx.Response(201, json=authorize_response()))

        authorization = authorize_order(self.client, self.order)

        request = self.fake.api_requests(self.path)[0]
        self.assertEqual(request.headers["paypal-request-id"], authorization.request_id)
        self.assertEqual(
            request.headers["paypal-request-id"],
            f"order:{self.order.pk}:authorize:{authorization.pk}",
        )

    def test_recovery_reuses_the_interrupted_attempt(self):
        interrupted = self.order.start_authorization()
        self.fake.queue(self.path, httpx.Response(201, json=authorize_response()))

        authorization = authorize_order(self.client, self.order)

        self.assertEqual(authorization.pk, interrupted.pk)
        self.assertEqual(self.order.authorizations.count(), 1)

    def test_a_new_attempt_after_a_denial_uses_a_new_key(self):
        self.fake.queue(
            self.path,
            httpx.Response(201, json=authorize_response("DENIED", auth_id="AUTH-1")),
            httpx.Response(201, json=authorize_response("CREATED", auth_id="AUTH-2")),
        )

        denied = authorize_order(self.client, self.order)
        retried = authorize_order(self.client, self.order)

        self.assertEqual(denied.status, Authorization.Status.DENIED)
        self.assertNotEqual(denied.request_id, retried.request_id)
        self.assertEqual(self.order.authorizations.count(), 2)

    def test_authorize_needs_a_confirmed_order(self):
        order = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=False)

        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            authorize_order(self.client, order)

        self.assertEqual(self.fake.requests, [])
        self.assertEqual(Authorization.objects.count(), 0)

    def test_response_without_an_authorization_stays_unconfirmed(self):
        self.fake.queue(
            self.path,
            httpx.Response(201, json={"id": PAYPAL_ID, "status": "COMPLETED"}),
        )

        with self.assertLogs("paypal_checkout.orders", level="WARNING") as logs:
            authorization = authorize_order(self.client, self.order)

        self.assertTrue(authorization.is_unconfirmed)
        self.assertEqual(logs.records[0].paypal_issue, "authorization_not_in_response")
        self.assertIsNotNone(self.order.pending_authorization())

    def test_authorization_is_found_past_units_that_do_not_hold_it(self):
        payload = {
            "id": PAYPAL_ID,
            "status": "COMPLETED",
            "purchase_units": [
                "not-a-dict",
                {"reference_id": "no-payments"},
                {"payments": {"authorizations": []}},
                {"payments": {"authorizations": [{"id": AUTH_ID, "status": "CREATED"}]}},
            ],
        }
        self.fake.queue(self.path, httpx.Response(201, json=payload))

        authorization = authorize_order(self.client, self.order)

        self.assertEqual(authorization.paypal_id, AUTH_ID)


class AuthorizationUpdateTests(TestCase):
    def setUp(self):
        self.order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False
        )
        self.authorization = self.order.start_authorization()

    def test_missing_id_does_not_blank_an_existing_one(self):
        self.authorization.update_from_payload({"id": AUTH_ID, "status": "CREATED"})
        self.authorization.update_from_payload({"status": "CAPTURED"})

        self.authorization.refresh_from_db()
        self.assertEqual(self.authorization.paypal_id, AUTH_ID)
        self.assertEqual(self.authorization.status, Authorization.Status.CAPTURED)

    def test_unknown_status_is_ignored_but_kept_in_raw(self):
        self.authorization.update_from_payload({"id": AUTH_ID, "status": "WEIRD"})

        self.authorization.refresh_from_db()
        self.assertEqual(self.authorization.status, Authorization.Status.INITIATED)
        self.assertEqual(self.authorization.raw["status"], "WEIRD")

    def test_save_can_be_deferred(self):
        self.authorization.update_from_payload({"id": AUTH_ID}, save=False)

        self.assertEqual(self.authorization.paypal_id, AUTH_ID)
        self.authorization.refresh_from_db()
        self.assertIsNone(self.authorization.paypal_id)

    def test_str_before_and_after_confirmation(self):
        self.assertIn("Initiated locally", str(self.authorization))

        self.authorization.update_from_payload({"id": AUTH_ID, "status": "CREATED"})
        self.assertEqual(str(self.authorization), AUTH_ID)


class CaptureAuthorizationTests(ClientMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.order = PayPalOrder.objects.start(
            amount=Decimal("10.00"),
            currency="EUR",
            live=False,
            intent=PayPalOrder.Intent.AUTHORIZE,
        )
        self.order.update_from_payload(order_response("APPROVED"))
        self.authorization = self.order.start_authorization()
        self.authorization.update_from_payload({"id": AUTH_ID, "status": "CREATED"})
        self.path = f"{AUTHORIZATIONS_PATH}/{AUTH_ID}/capture"

    def test_successful_capture(self):
        self.fake.queue(self.path, httpx.Response(201, json=authorization_capture_response()))

        capture = capture_authorization(self.client, self.authorization)

        self.assertTrue(capture.is_successful)
        self.assertEqual(capture.paypal_id, CAPTURE_ID)
        self.assertEqual(capture.authorization, self.authorization)
        self.assertEqual(capture.order, self.order)

    def test_persisted_key_is_sent(self):
        self.fake.queue(self.path, httpx.Response(201, json=authorization_capture_response()))

        capture = capture_authorization(self.client, self.authorization)

        self.assertEqual(
            self.fake.api_requests(self.path)[0].headers["paypal-request-id"],
            capture.request_id,
        )

    def test_partial_capture_sends_amount(self):
        self.fake.queue(
            self.path,
            httpx.Response(201, json=authorization_capture_response(value="4.00")),
        )

        capture = capture_authorization(
            self.client, self.authorization, amount=Decimal("4.00"), final_capture=False
        )

        body = json.loads(self.fake.api_requests(self.path)[0].read())
        self.assertEqual(body["amount"], {"currency_code": "EUR", "value": "4.00"})
        self.assertIs(body["final_capture"], False)
        self.assertEqual(capture.amount, Decimal("4.00"))

    def test_full_capture_sends_no_body(self):
        self.fake.queue(self.path, httpx.Response(201, json=authorization_capture_response()))

        capture_authorization(self.client, self.authorization)

        self.assertEqual(self.fake.api_requests(self.path)[0].read(), b"")

    def test_signal_is_sent(self):
        self.fake.queue(self.path, httpx.Response(201, json=authorization_capture_response()))

        with catch_signal(payment_captured) as received:
            capture = capture_authorization(self.client, self.authorization)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["capture"], capture)
        self.assertEqual(received[0]["order"], self.order)

    def test_recovery_reuses_the_interrupted_attempt(self):
        interrupted = self.authorization.start_capture()
        self.fake.queue(self.path, httpx.Response(201, json=authorization_capture_response()))

        capture = capture_authorization(self.client, self.authorization)

        self.assertEqual(capture.pk, interrupted.pk)
        self.assertEqual(self.authorization.captures.count(), 1)

    def test_capture_needs_a_confirmed_authorization(self):
        unconfirmed = PayPalOrder.objects.start(
            amount=Decimal("1.00"), currency="EUR", live=False
        ).start_authorization()

        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            capture_authorization(self.client, unconfirmed)

        self.assertEqual(self.fake.requests, [])

    def test_order_and_authorization_capture_pools_are_separate(self):
        """A direct order capture must not be mistaken for an authorization one."""
        direct = self.order.start_capture()
        via_authorization = self.authorization.start_capture()

        self.assertNotEqual(direct.pk, via_authorization.pk)
        self.assertIsNone(direct.authorization)
        self.assertEqual(self.order.pending_capture(), direct)
        self.assertEqual(self.authorization.pending_capture(), via_authorization)

    def test_authorizations_and_captures_die_with_the_order(self):
        self.authorization.start_capture()
        self.order.delete()

        self.assertEqual(Authorization.objects.count(), 0)
        self.assertEqual(Capture.objects.count(), 0)
