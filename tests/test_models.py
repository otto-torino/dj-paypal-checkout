from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase

from paypal_checkout.models import Capture, PayPalOrder, capture_request_id, order_request_id

from .test_app.models import ShopOrder


class RequestIdSchemeTests(TestCase):
    def test_order_key(self):
        self.assertEqual(order_request_id(42), "order:42:create")

    def test_capture_key_is_per_attempt(self):
        """A fixed key per order would make PayPal replay the first response."""
        self.assertEqual(capture_request_id(42, 7), "order:42:capture:7")
        self.assertNotEqual(capture_request_id(42, 7), capture_request_id(42, 8))


class StartOrderTests(TestCase):
    def test_row_exists_before_paypal_is_called(self):
        order = PayPalOrder.objects.start(amount=Decimal("10.00"), currency="EUR", live=False)

        self.assertIsNotNone(order.pk)
        self.assertIsNone(order.paypal_id, "PayPal has not answered yet")
        self.assertEqual(order.status, PayPalOrder.Status.INITIATED)
        self.assertFalse(order.is_confirmed_by_paypal)

    def test_key_is_persisted_not_just_derived(self):
        order = PayPalOrder.objects.start(amount=Decimal("10.00"), currency="EUR", live=False)

        order.refresh_from_db()
        self.assertEqual(order.request_id, f"order:{order.pk}:create")

    def test_defaults(self):
        order = PayPalOrder.objects.start(amount=Decimal("10.00"), currency="eur", live=False)

        self.assertEqual(order.intent, PayPalOrder.Intent.CAPTURE)
        self.assertEqual(order.currency, "EUR")
        self.assertEqual(order.raw, {})

    def test_environment_is_recorded(self):
        sandbox = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=False)
        live = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=True)

        self.assertFalse(sandbox.live)
        self.assertTrue(live.live)

    def test_target_links_to_the_host_order(self):
        shop_order = ShopOrder.objects.create(reference="ORD-1", total=Decimal("10.00"))
        order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False, target=shop_order
        )

        order.refresh_from_db()
        self.assertEqual(order.target, shop_order)
        self.assertEqual(list(PayPalOrder.objects.for_target(shop_order)), [order])

    def test_target_is_optional(self):
        order = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=False)
        self.assertIsNone(order.target)

    def test_pending_queryset_finds_unconfirmed_orders(self):
        order = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=False)
        confirmed = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=False)
        confirmed.update_from_payload({"id": "5O1", "status": "CREATED"})

        self.assertEqual(list(PayPalOrder.objects.pending()), [order])

    def test_paypal_id_is_unique(self):
        first = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=False)
        second = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=False)
        first.update_from_payload({"id": "5O1"})

        second.paypal_id = "5O1"
        with self.assertRaises(IntegrityError):
            second.save(update_fields=["paypal_id"])

    def test_str_before_and_after_confirmation(self):
        order = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=False)
        self.assertIn("Initiated locally", str(order))

        order.update_from_payload({"id": "5O1", "status": "CREATED"})
        self.assertEqual(str(order), "5O1")


class UpdateFromPayloadTests(TestCase):
    def setUp(self):
        self.order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False
        )

    def test_known_fields_are_merged(self):
        self.order.update_from_payload(
            {"id": "5O1", "status": "APPROVED", "intent": "AUTHORIZE", "extra": 1}
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.paypal_id, "5O1")
        self.assertEqual(self.order.status, PayPalOrder.Status.APPROVED)
        self.assertEqual(self.order.intent, PayPalOrder.Intent.AUTHORIZE)
        self.assertEqual(self.order.raw["extra"], 1)

    def test_unknown_status_is_ignored_rather_than_stored(self):
        self.order.update_from_payload({"id": "5O1", "status": "SOMETHING_NEW"})

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, PayPalOrder.Status.INITIATED)
        self.assertEqual(self.order.raw["status"], "SOMETHING_NEW", "still auditable")

    def test_missing_id_does_not_blank_an_existing_one(self):
        self.order.update_from_payload({"id": "5O1"})
        self.order.update_from_payload({"status": "APPROVED"})

        self.order.refresh_from_db()
        self.assertEqual(self.order.paypal_id, "5O1")

    def test_save_can_be_deferred(self):
        self.order.update_from_payload({"id": "5O1"}, save=False)

        self.assertEqual(self.order.paypal_id, "5O1")
        self.order.refresh_from_db()
        self.assertIsNone(self.order.paypal_id)


class StartCaptureTests(TestCase):
    def setUp(self):
        self.order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False
        )
        self.order.update_from_payload({"id": "5O1", "status": "APPROVED"})

    def test_capture_row_exists_before_the_call(self):
        capture = self.order.start_capture()

        self.assertEqual(capture.status, Capture.Status.INITIATED)
        self.assertIsNone(capture.paypal_id)
        self.assertEqual(capture.amount, Decimal("10.00"))
        self.assertEqual(capture.currency, "EUR")
        self.assertTrue(capture.final_capture)

    def test_key_is_persisted_and_per_attempt(self):
        capture = self.order.start_capture()

        capture.refresh_from_db()
        self.assertEqual(capture.request_id, f"order:{self.order.pk}:capture:{capture.pk}")

    def test_an_unconfirmed_attempt_is_reused_not_duplicated(self):
        """Recovery after a crash must reuse the key: it may have reached PayPal."""
        first = self.order.start_capture()
        again = self.order.start_capture()

        self.assertEqual(first.pk, again.pk)
        self.assertEqual(first.request_id, again.request_id)
        self.assertEqual(self.order.captures.count(), 1)

    def test_a_new_attempt_after_a_decline_gets_a_new_key(self):
        declined = self.order.start_capture()
        declined.update_from_payload({"id": "CAP-1", "status": "DECLINED"})

        retry = self.order.start_capture()

        self.assertNotEqual(retry.pk, declined.pk)
        self.assertNotEqual(retry.request_id, declined.request_id)
        self.assertEqual(self.order.captures.count(), 2)

    def test_partial_capture_amount_and_currency(self):
        capture = self.order.start_capture(
            amount=Decimal("4.00"), currency="usd", final_capture=False
        )

        self.assertEqual(capture.amount, Decimal("4.00"))
        self.assertEqual(capture.currency, "USD")
        self.assertFalse(capture.final_capture)

    def test_pending_capture_is_none_once_confirmed(self):
        capture = self.order.start_capture()
        capture.update_from_payload({"id": "CAP-1", "status": "COMPLETED"})

        self.assertIsNone(self.order.pending_capture())

    def test_captures_are_deleted_with_the_order(self):
        self.order.start_capture()
        self.order.delete()

        self.assertEqual(Capture.objects.count(), 0)


class CaptureTests(TestCase):
    def setUp(self):
        self.order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False
        )

    def test_update_from_payload(self):
        capture = self.order.start_capture()
        capture.update_from_payload(
            {"id": "CAP-1", "status": "COMPLETED", "final_capture": False, "x": 2}
        )

        capture.refresh_from_db()
        self.assertEqual(capture.paypal_id, "CAP-1")
        self.assertTrue(capture.is_successful)
        self.assertFalse(capture.final_capture)
        self.assertEqual(capture.raw["x"], 2)

    def test_unknown_status_is_ignored(self):
        capture = self.order.start_capture()
        capture.update_from_payload({"id": "CAP-1", "status": "WEIRD"})

        capture.refresh_from_db()
        self.assertEqual(capture.status, Capture.Status.INITIATED)

    def test_missing_id_does_not_blank_an_existing_one(self):
        """A webhook payload may describe a capture without repeating its id."""
        capture = self.order.start_capture()
        capture.update_from_payload({"id": "CAP-1", "status": "PENDING"})
        capture.update_from_payload({"status": "COMPLETED"})

        capture.refresh_from_db()
        self.assertEqual(capture.paypal_id, "CAP-1")
        self.assertTrue(capture.is_successful)

    def test_save_can_be_deferred(self):
        capture = self.order.start_capture()
        capture.update_from_payload({"id": "CAP-1"}, save=False)

        capture.refresh_from_db()
        self.assertIsNone(capture.paypal_id)

    def test_str_before_and_after_confirmation(self):
        capture = self.order.start_capture()
        self.assertIn("Initiated locally", str(capture))

        capture.update_from_payload({"id": "CAP-1", "status": "COMPLETED"})
        self.assertEqual(str(capture), "CAP-1")

    def test_is_successful_only_for_completed(self):
        capture = self.order.start_capture()
        for status in (Capture.Status.PENDING, Capture.Status.DECLINED, Capture.Status.FAILED):
            with self.subTest(status=status):
                capture.status = status
                self.assertFalse(capture.is_successful)
