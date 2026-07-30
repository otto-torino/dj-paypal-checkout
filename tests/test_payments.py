"""Refunds and voids."""

import json
from decimal import Decimal

import httpx
from django.test import TestCase

from paypal_checkout.exceptions import (
    PayPalAmountError,
    PayPalError,
    PayPalServerError,
    PayPalValidationError,
)
from paypal_checkout.models import (
    Authorization,
    Capture,
    PayPalOrder,
    Refund,
    refund_request_id,
    void_request_id,
)
from paypal_checkout.payments import (
    AUTHORIZATIONS_PATH,
    CAPTURES_PATH,
    _mark_refund_failed,
    _settle_refund,
    adopt_remote_refund,
    capture_id_from_refund,
    merge_refund_attempt,
    refund_capture,
    retry_refund,
    void_authorization,
)
from paypal_checkout.signals import payment_refunded, refund_attempt_merged

from .support import catch_signal, make_config
from .test_app.models import ShopOrder
from .test_orders import CAPTURE_ID, PAYPAL_ID, ClientMixin, order_response

REFUND_ID = "1JU08902781691411"
AUTH_ID = "0VF52814937998046"


def refund_response(status="COMPLETED", value="10.00", refund_id=REFUND_ID):
    return {
        "id": refund_id,
        "status": status,
        "amount": {"currency_code": "EUR", "value": value},
    }


class RequestIdSchemeTests(TestCase):
    def test_refund_key_is_per_refund(self):
        self.assertEqual(refund_request_id(42, 7), "order:42:refund:7")
        self.assertNotEqual(refund_request_id(42, 7), refund_request_id(42, 8))

    def test_void_key_is_per_authorization(self):
        """Voiding is single-shot, so this key is deliberately not per attempt."""
        self.assertEqual(void_request_id(42, 3), "order:42:void:3")
        self.assertEqual(void_request_id(42, 3), void_request_id(42, 3))

    def test_refund_parent_can_come_from_related_ids(self):
        self.assertEqual(
            capture_id_from_refund(
                {
                    "supplementary_data": {
                        "related_ids": {"capture_id": CAPTURE_ID}
                    }
                }
            ),
            CAPTURE_ID,
        )


class CapturedOrderMixin(ClientMixin):
    def setUp(self):
        super().setUp()
        self.shop_order = ShopOrder.objects.create(reference="ORD-1", total=Decimal("10.00"))
        self.order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False, target=self.shop_order
        )
        self.order.update_from_payload(order_response("COMPLETED"))
        self.capture = self.order.start_capture()
        self.capture.update_from_payload({"id": CAPTURE_ID, "status": "COMPLETED"})
        self.path = f"{CAPTURES_PATH}/{CAPTURE_ID}/refund"


class RefundCaptureTests(CapturedOrderMixin, TestCase):
    def test_a_full_refund(self):
        self.fake.queue(self.path, httpx.Response(201, json=refund_response()))

        refund = refund_capture(self.client, self.capture)

        self.assertTrue(refund.is_successful)
        self.assertEqual(refund.paypal_id, REFUND_ID)
        self.assertEqual(refund.amount, Decimal("10.00"))
        self.assertEqual(refund.sent_body, {})
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.status, Capture.Status.REFUNDED)

    def test_a_full_refund_sends_no_amount(self):
        """Omitting it lets PayPal refund exactly what was captured."""
        self.fake.queue(self.path, httpx.Response(201, json=refund_response()))

        refund_capture(self.client, self.capture)

        self.assertEqual(self.fake.api_requests(self.path)[0].read(), b"")

    def test_a_partial_refund(self):
        self.fake.queue(self.path, httpx.Response(201, json=refund_response(value="4.00")))

        refund = refund_capture(self.client, self.capture, amount=Decimal("4.00"))

        body = json.loads(self.fake.api_requests(self.path)[0].read())
        self.assertEqual(body["amount"], {"currency_code": "EUR", "value": "4.00"})
        self.assertEqual(refund.amount, Decimal("4.00"))
        self.assertEqual(refund.sent_body, body)
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.status, Capture.Status.PARTIALLY_REFUNDED)

    def test_the_persisted_key_is_sent(self):
        self.fake.queue(self.path, httpx.Response(201, json=refund_response()))

        refund = refund_capture(self.client, self.capture)

        self.assertEqual(
            self.fake.api_requests(self.path)[0].headers["paypal-request-id"],
            refund.request_id,
        )
        self.assertEqual(refund.request_id, f"order:{self.order.pk}:refund:{refund.pk}")

    def test_note_and_invoice_id_are_forwarded(self):
        self.fake.queue(self.path, httpx.Response(201, json=refund_response()))

        refund_capture(
            self.client, self.capture, note_to_payer="sorry", invoice_id="INV-1"
        )

        body = json.loads(self.fake.api_requests(self.path)[0].read())
        self.assertEqual(body["note_to_payer"], "sorry")
        self.assertEqual(body["invoice_id"], "INV-1")

    def test_the_signal_carries_the_refund(self):
        self.fake.queue(self.path, httpx.Response(201, json=refund_response()))

        with catch_signal(payment_refunded) as received:
            refund = refund_capture(self.client, self.capture)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["refund"], refund)
        self.assertEqual(received[0]["capture"], self.capture)
        self.assertEqual(received[0]["target"], self.shop_order)

    def test_an_unsuccessful_refund_sends_nothing(self):
        self.fake.queue(self.path, httpx.Response(201, json=refund_response("PENDING")))

        with catch_signal(payment_refunded) as received:
            refund = refund_capture(self.client, self.capture)

        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertEqual(received, [])
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.status, Capture.Status.COMPLETED)

    def test_recovery_requires_an_explicit_retry(self):
        interrupted = self.capture.start_refund(sent_body={})

        with self.assertRaisesMessage(PayPalError, "retry_refund"):
            refund_capture(self.client, self.capture)

        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.capture.refunds.count(), 1)
        self.assertEqual(self.capture.pending_refund(), interrupted)

    def test_retry_reposts_the_exact_persisted_body_and_key(self):
        body = {
            "amount": {"currency_code": "EUR", "value": "4.00"},
            "note_to_payer": "original",
        }
        interrupted = self.capture.start_refund(
            amount=Decimal("4.00"),
            note_to_payer="original",
            sent_body=body,
        )
        self.fake.queue(
            self.path, httpx.Response(201, json=refund_response(value="4.00"))
        )

        refund = retry_refund(self.client, interrupted)

        request = self.fake.api_requests(self.path)[0]
        self.assertEqual(json.loads(request.read()), body)
        self.assertEqual(request.headers["paypal-request-id"], interrupted.request_id)
        self.assertEqual(refund.pk, interrupted.pk)

    def test_a_legacy_attempt_without_a_body_cannot_be_retried(self):
        interrupted = self.capture.start_refund()

        with self.assertRaisesMessage(PayPalError, "predates persisted request bodies"):
            retry_refund(self.client, interrupted)

        self.assertEqual(self.fake.requests, [])

    def test_a_known_terminal_retry_error_marks_the_attempt_failed(self):
        interrupted = self.capture.start_refund(
            amount=Decimal("4.00"),
            sent_body={"amount": {"currency_code": "EUR", "value": "4.00"}},
        )
        self.fake.queue(
            self.path,
            httpx.Response(
                422,
                json={
                    "name": "UNPROCESSABLE_ENTITY",
                    "details": [{"issue": "REFUND_AMOUNT_EXCEEDED"}],
                },
            ),
        )

        with self.assertRaises(PayPalValidationError):
            retry_refund(self.client, interrupted)

        interrupted.refresh_from_db()
        self.assertEqual(interrupted.status, Refund.Status.FAILED)
        self.assertEqual(interrupted.raw["error"]["name"], "UNPROCESSABLE_ENTITY")

    def test_an_unknown_422_leaves_the_attempt_unconfirmed(self):
        interrupted = self.capture.start_refund(sent_body={})
        self.fake.queue(
            self.path,
            httpx.Response(
                422,
                json={
                    "name": "UNPROCESSABLE_ENTITY",
                    "details": [{"issue": "NEW_PAYPAL_ISSUE"}],
                },
            ),
        )

        with self.assertRaises(PayPalValidationError):
            retry_refund(self.client, interrupted)

        interrupted.refresh_from_db()
        self.assertEqual(interrupted.status, Refund.Status.INITIATED)

    def test_a_retry_requires_an_unresolved_status(self):
        completed = self.capture.start_refund(sent_body={})
        completed.update_from_payload(refund_response())

        with self.assertRaisesMessage(PayPalError, "not an unresolved attempt"):
            retry_refund(self.client, completed)

    def test_a_terminal_error_does_not_overwrite_a_concurrent_resolution(self):
        completed = self.capture.start_refund(sent_body={})
        completed.update_from_payload(refund_response())
        error = PayPalValidationError(
            422,
            name="UNPROCESSABLE_ENTITY",
            payload={"name": "UNPROCESSABLE_ENTITY"},
        )

        _mark_refund_failed(completed, error)

        completed.refresh_from_db()
        self.assertEqual(completed.status, Refund.Status.COMPLETED)

    def test_a_response_without_an_id_updates_without_trying_to_merge(self):
        interrupted = self.capture.start_refund(sent_body={})
        self.fake.queue(
            self.path, httpx.Response(201, json={"status": "PENDING"})
        )

        refund = retry_refund(self.client, interrupted)

        self.assertEqual(refund.pk, interrupted.pk)
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertIsNone(refund.paypal_id)

    def test_a_webhook_adoption_marks_the_local_attempt_unresolved(self):
        interrupted = self.capture.start_refund(
            amount=Decimal("4.00"),
            sent_body={"amount": {"currency_code": "EUR", "value": "4.00"}},
        )

        adopted = adopt_remote_refund(
            self.capture,
            {
                "id": REFUND_ID,
                "status": "COMPLETED",
                "amount": {"currency_code": "EUR", "value": "4.00"},
            },
        )

        interrupted.refresh_from_db()
        self.assertEqual(interrupted.status, Refund.Status.UNRESOLVED)
        self.assertEqual(adopted.status, Refund.Status.COMPLETED)
        self.assertEqual(self.capture.reserved_refund_amount, Decimal("8.00"))

        with self.assertRaisesMessage(PayPalError, "retry_refund"):
            refund_capture(self.client, self.capture, amount=Decimal("1.00"))

    def test_retry_merges_a_proved_webhook_duplicate_without_resignalling(self):
        body = {"amount": {"currency_code": "EUR", "value": "4.00"}}
        interrupted = self.capture.start_refund(
            amount=Decimal("4.00"), sent_body=body
        )
        request_id = interrupted.request_id
        adopted = adopt_remote_refund(
            self.capture,
            {
                "id": REFUND_ID,
                "status": "COMPLETED",
                "amount": {"currency_code": "EUR", "value": "4.00"},
            },
        )
        self.fake.queue(
            self.path,
            httpx.Response(200, json=refund_response(value="4.00")),
        )

        with self.assertLogs("paypal_checkout.payments", level="WARNING"):
            with catch_signal(payment_refunded) as received:
                survivor = retry_refund(self.client, interrupted)

        self.assertEqual(received, [])
        self.assertEqual(survivor.pk, adopted.pk)
        self.assertEqual(survivor.request_id, request_id)
        self.assertEqual(survivor.sent_body, body)
        self.assertEqual(Refund.objects.count(), 1)
        history = survivor.merge_metadata["merged_attempts"][0]
        self.assertEqual(history["pk"], interrupted.pk)
        self.assertEqual(history["request_id"], request_id)
        self.assertEqual(self.capture.reserved_refund_amount, Decimal("4.00"))

        settled_again, notify = _settle_refund(
            interrupted, refund_response(value="4.00")
        )
        self.assertEqual(settled_again.pk, survivor.pk)
        self.assertFalse(notify)
        self.assertEqual(
            retry_refund(self.client, interrupted).pk,
            survivor.pk,
            "a concurrent/repeated retry follows the migrated request id",
        )

    def test_retry_with_a_different_remote_id_proves_two_refunds(self):
        body = {"amount": {"currency_code": "EUR", "value": "4.00"}}
        interrupted = self.capture.start_refund(
            amount=Decimal("4.00"), sent_body=body
        )
        adopt_remote_refund(
            self.capture,
            {
                "id": "DASHBOARD-REFUND",
                "status": "COMPLETED",
                "amount": {"currency_code": "EUR", "value": "4.00"},
            },
        )
        self.fake.queue(
            self.path,
            httpx.Response(
                201, json=refund_response(value="4.00", refund_id="LOCAL-REFUND")
            ),
        )

        survivor = retry_refund(self.client, interrupted)

        self.assertEqual(survivor.pk, interrupted.pk)
        self.assertEqual(survivor.paypal_id, "LOCAL-REFUND")
        self.assertEqual(Refund.objects.count(), 2)
        self.assertEqual(self.capture.reserved_refund_amount, Decimal("8.00"))

    def test_a_legacy_attempt_can_be_merged_after_manual_attribution(self):
        interrupted = self.capture.start_refund(amount=Decimal("4.00"))
        adopted = adopt_remote_refund(
            self.capture,
            {
                "id": REFUND_ID,
                "status": "COMPLETED",
                "amount": {"currency_code": "EUR", "value": "4.00"},
            },
        )

        with self.assertLogs("paypal_checkout.payments", level="WARNING"):
            with catch_signal(refund_attempt_merged) as received:
                survivor = merge_refund_attempt(interrupted, adopted)

        self.assertEqual(survivor.pk, adopted.pk)
        self.assertEqual(survivor.request_id, interrupted.request_id)
        self.assertIsNone(survivor.sent_body)
        self.assertEqual(Refund.objects.count(), 1)
        self.assertEqual(received[0]["refund"], survivor)
        self.assertEqual(received[0]["attempt"]["pk"], interrupted.pk)

    def test_a_manual_merge_requires_a_distinct_unresolved_attempt(self):
        interrupted = self.capture.start_refund(sent_body={})

        with self.assertRaisesMessage(PayPalError, "cannot be merged into itself"):
            merge_refund_attempt(interrupted, interrupted)

        interrupted.update_from_payload(refund_response())
        other = self.capture.refunds.create(
            paypal_id="OTHER-REFUND",
            status=Refund.Status.COMPLETED,
            amount=Decimal("1.00"),
            currency="EUR",
        )
        with self.assertRaisesMessage(PayPalError, "not an unresolved attempt"):
            merge_refund_attempt(interrupted, other)

    def test_a_manual_merge_requires_a_remote_survivor_on_the_same_capture(self):
        interrupted = self.capture.start_refund(sent_body={})
        local_survivor = self.capture.refunds.create(
            status=Refund.Status.COMPLETED,
            amount=Decimal("1.00"),
            currency="EUR",
        )
        with self.assertRaisesMessage(PayPalError, "must have a PayPal id"):
            merge_refund_attempt(interrupted, local_survivor)

        other_order = PayPalOrder.objects.start(
            amount=Decimal("1.00"),
            currency="EUR",
            live=False,
            target=self.shop_order,
        )
        other_capture = other_order.captures.create(
            paypal_id="OTHER-CAPTURE",
            status=Capture.Status.COMPLETED,
            amount=Decimal("1.00"),
            currency="EUR",
        )
        remote = other_capture.refunds.create(
            paypal_id="REMOTE-REFUND",
            status=Refund.Status.COMPLETED,
            amount=Decimal("1.00"),
            currency="EUR",
        )
        with self.assertRaisesMessage(PayPalError, "different captures"):
            merge_refund_attempt(interrupted, remote)

    def test_a_manual_merge_does_not_replace_an_existing_local_attribution(self):
        interrupted = self.capture.start_refund(sent_body={})
        survivor = self.capture.refunds.create(
            paypal_id="REMOTE-REFUND",
            status=Refund.Status.COMPLETED,
            amount=Decimal("1.00"),
            currency="EUR",
        )
        survivor.request_id = "already-attributed"
        survivor.save(update_fields=["request_id"])

        with self.assertRaisesMessage(PayPalError, "already associated"):
            merge_refund_attempt(interrupted, survivor)

    def test_a_deleted_attempt_without_a_merge_survivor_stays_missing(self):
        refund = self.capture.start_refund(sent_body={})
        Refund.objects.filter(pk=refund.pk).delete()

        with self.assertRaises(Refund.DoesNotExist):
            retry_refund(self.client, refund)

    def test_settling_a_disappeared_attempt_requires_a_merge_survivor(self):
        refund = self.capture.start_refund(sent_body={})
        Refund.objects.filter(pk=refund.pk).delete()

        with self.assertRaisesMessage(Refund.DoesNotExist, "without a merge survivor"):
            _settle_refund(refund, refund_response())

    def test_a_terminal_error_tolerates_a_disappeared_attempt(self):
        refund = self.capture.start_refund(sent_body={})
        Refund.objects.filter(pk=refund.pk).delete()
        error = PayPalValidationError(
            422,
            name="UNPROCESSABLE_ENTITY",
            payload={"name": "UNPROCESSABLE_ENTITY"},
        )

        _mark_refund_failed(refund, error)

    def test_two_partial_refunds_get_different_keys(self):
        self.fake.queue(
            self.path,
            httpx.Response(201, json=refund_response(value="4.00", refund_id="REF-1")),
            httpx.Response(201, json=refund_response(value="6.00", refund_id="REF-2")),
        )

        first = refund_capture(self.client, self.capture, amount=Decimal("4.00"))
        second = refund_capture(self.client, self.capture, amount=Decimal("6.00"))

        self.assertNotEqual(first.request_id, second.request_id)
        keys = [r.headers["paypal-request-id"] for r in self.fake.api_requests(self.path)]
        self.assertEqual(len(set(keys)), 2)
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.refunded_amount, Decimal("10.00"))
        self.assertEqual(self.capture.status, Capture.Status.REFUNDED)

    def test_refunding_more_than_was_captured_is_refused_locally(self):
        with self.assertRaisesMessage(PayPalAmountError, "still refundable"):
            refund_capture(self.client, self.capture, amount=Decimal("10.01"))

        self.assertEqual(self.fake.requests, [])
        self.assertEqual(Refund.objects.count(), 0, "a refused refund leaves no trace")

    def test_the_guard_counts_refunds_already_in_flight(self):
        self.fake.queue(
            self.path, httpx.Response(201, json=refund_response(value="7.00", refund_id="REF-1"))
        )
        refund_capture(self.client, self.capture, amount=Decimal("7.00"))

        with self.assertRaisesMessage(PayPalAmountError, "still refundable"):
            refund_capture(self.client, self.capture, amount=Decimal("4.00"))

    def test_the_guard_counts_refunds_of_unknown_outcome(self):
        """An INITIATED refund may have reached PayPal, so it must count."""
        self.capture.start_refund(amount=Decimal("8.00"))
        self.capture.refunds.update(status=Refund.Status.PENDING)  # not the pending row

        with self.assertRaises(PayPalAmountError):
            refund_capture(self.client, self.capture, amount=Decimal("4.00"))

    def test_a_cancelled_refund_frees_the_amount_again(self):
        refund = self.capture.start_refund(amount=Decimal("10.00"))
        refund.status = Refund.Status.CANCELLED
        refund.save(update_fields=["status"])
        self.fake.queue(self.path, httpx.Response(201, json=refund_response()))

        self.assertEqual(self.capture.refundable_amount, Decimal("10.00"))
        refund_capture(self.client, self.capture)

    def test_a_capture_paypal_never_confirmed_cannot_be_refunded(self):
        unconfirmed = self.order.start_capture()

        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            refund_capture(self.client, unconfirmed)

        self.assertEqual(self.fake.requests, [])

    def test_a_failure_leaves_the_attempt_discoverable(self):
        self.fake.queue(self.path, *[httpx.Response(500, json={"name": "INTERNAL"})] * 3)

        with self.assertRaises(PayPalServerError):
            refund_capture(self.client, self.capture)

        self.assertIsNotNone(self.capture.pending_refund())


class RefundableAmountTests(TestCase):
    def setUp(self):
        order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False
        )
        self.capture = order.start_capture()
        self.capture.update_from_payload({"id": CAPTURE_ID, "status": "COMPLETED"})

    def make_refund(self, amount, status):
        refund = self.capture.start_refund(amount=Decimal(amount))
        refund.status = status
        refund.save(update_fields=["status"])
        return refund

    def test_nothing_refunded_yet(self):
        self.assertEqual(self.capture.refunded_amount, Decimal("0.00"))
        self.assertEqual(self.capture.refundable_amount, Decimal("10.00"))

    def test_only_completed_counts_as_refunded(self):
        self.make_refund("3.00", Refund.Status.COMPLETED)
        self.make_refund("2.00", Refund.Status.PENDING)

        self.assertEqual(self.capture.refunded_amount, Decimal("3.00"))
        self.assertEqual(self.capture.reserved_refund_amount, Decimal("5.00"))
        self.assertEqual(self.capture.refundable_amount, Decimal("5.00"))

    def test_unresolved_attempts_remain_reserved_but_are_not_pending(self):
        refund = self.make_refund("2.00", Refund.Status.UNRESOLVED)

        self.assertEqual(self.capture.reserved_refund_amount, Decimal("2.00"))
        self.assertEqual(self.capture.refundable_amount, Decimal("8.00"))
        self.assertIsNone(self.capture.pending_refund())
        self.assertEqual(refund.status, Refund.Status.UNRESOLVED)

    def test_failed_and_cancelled_do_not_count(self):
        self.make_refund("3.00", Refund.Status.FAILED)
        self.make_refund("2.00", Refund.Status.CANCELLED)

        self.assertEqual(self.capture.reserved_refund_amount, Decimal("0.00"))
        self.assertEqual(self.capture.refundable_amount, Decimal("10.00"))

    def test_sync_refund_status_is_a_no_op_without_refunds(self):
        self.capture.sync_refund_status()
        self.assertEqual(self.capture.status, Capture.Status.COMPLETED)

    def test_sync_refund_status_can_defer_the_save(self):
        self.make_refund("10.00", Refund.Status.COMPLETED)

        self.capture.sync_refund_status(save=False)

        self.assertEqual(self.capture.status, Capture.Status.REFUNDED)
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.status, Capture.Status.COMPLETED)

    def test_sync_refund_status_is_idempotent(self):
        self.make_refund("10.00", Refund.Status.COMPLETED)

        self.capture.sync_refund_status()
        self.capture.sync_refund_status()

        self.capture.refresh_from_db()
        self.assertEqual(self.capture.status, Capture.Status.REFUNDED)


class RefundModelTests(TestCase):
    def setUp(self):
        order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False
        )
        self.capture = order.start_capture()
        self.capture.update_from_payload({"id": CAPTURE_ID, "status": "COMPLETED"})

    def test_str_before_and_after_confirmation(self):
        refund = self.capture.start_refund()
        self.assertIn("Initiated locally", str(refund))

        refund.update_from_payload(refund_response())
        self.assertEqual(str(refund), REFUND_ID)

    def test_unknown_status_is_ignored(self):
        refund = self.capture.start_refund()
        refund.update_from_payload({"id": REFUND_ID, "status": "WEIRD"})

        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.INITIATED)
        self.assertTrue(refund.is_unconfirmed)

    def test_missing_id_does_not_blank_an_existing_one(self):
        refund = self.capture.start_refund()
        refund.update_from_payload({"id": REFUND_ID, "status": "PENDING"})
        refund.update_from_payload({"status": "COMPLETED"})

        refund.refresh_from_db()
        self.assertEqual(refund.paypal_id, REFUND_ID)
        self.assertTrue(refund.is_successful)

    def test_save_can_be_deferred(self):
        refund = self.capture.start_refund()
        refund.update_from_payload({"id": REFUND_ID}, save=False)

        refund.refresh_from_db()
        self.assertIsNone(refund.paypal_id)

    def test_refunds_die_with_the_capture(self):
        self.capture.start_refund()
        self.capture.delete()

        self.assertEqual(Refund.objects.count(), 0)


class VoidAuthorizationTests(ClientMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.order = PayPalOrder.objects.start(
            amount=Decimal("10.00"),
            currency="EUR",
            live=False,
            intent=PayPalOrder.Intent.AUTHORIZE,
        )
        self.order.update_from_payload(order_response("COMPLETED"))
        self.authorization = self.order.start_authorization()
        self.authorization.update_from_payload({"id": AUTH_ID, "status": "CREATED"})
        self.path = f"{AUTHORIZATIONS_PATH}/{AUTH_ID}/void"

    def test_an_empty_204_marks_the_row_voided(self):
        self.fake.queue(self.path, httpx.Response(204))

        authorization = void_authorization(self.client, self.authorization)

        self.assertEqual(authorization.status, Authorization.Status.VOIDED)
        self.authorization.refresh_from_db()
        self.assertEqual(self.authorization.status, Authorization.Status.VOIDED)

    def test_a_payload_is_used_when_paypal_sends_one(self):
        self.fake.queue(
            self.path, httpx.Response(200, json={"id": AUTH_ID, "status": "VOIDED"})
        )

        authorization = void_authorization(self.client, self.authorization)

        self.assertEqual(authorization.status, Authorization.Status.VOIDED)
        self.assertEqual(authorization.raw["id"], AUTH_ID)

    def test_the_key_is_stable_across_attempts(self):
        self.fake.queue(self.path, httpx.Response(204), httpx.Response(204))

        void_authorization(self.client, self.authorization)
        void_authorization(self.client, self.authorization)

        keys = [r.headers["paypal-request-id"] for r in self.fake.api_requests(self.path)]
        self.assertEqual(len(set(keys)), 1, "voiding is single-shot")
        self.assertEqual(keys[0], f"order:{self.order.pk}:void:{self.authorization.pk}")

    def test_an_unconfirmed_authorization_cannot_be_voided(self):
        unconfirmed = self.order.start_authorization()
        unconfirmed.paypal_id = None

        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            void_authorization(self.client, unconfirmed)

        self.assertEqual(self.fake.requests, [])

    def test_strict_mode_is_satisfied(self):
        from paypal_checkout.client import PayPalClient

        self.fake.queue(self.path, httpx.Response(204))
        strict = PayPalClient(
            make_config(strict_idempotency=True), transport=self.fake.transport
        )
        self.addCleanup(strict.close)

        with self.assertNoLogs("paypal_checkout.client", level="WARNING"):
            void_authorization(strict, self.authorization)
