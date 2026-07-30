"""Concurrency tests that require a database with real row locks."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

from django.db import connections
from django.test import TransactionTestCase, skipUnlessDBFeature

from paypal_checkout.exceptions import PayPalError
from paypal_checkout.models import Capture, PayPalOrder, Refund


@skipUnlessDBFeature("has_select_for_update")
class RefundConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        order = PayPalOrder.objects.start(
            amount=Decimal("10.00"), currency="EUR", live=False
        )
        self.capture = order.start_capture()
        self.capture.update_from_payload({"id": "CAPTURE-1", "status": "COMPLETED"})

    def test_two_starters_are_serialized_by_the_capture_lock(self):
        ready = Barrier(2)

        def start_refund():
            connections.close_all()
            capture = Capture.objects.get(pk=self.capture.pk)
            ready.wait()
            try:
                return capture.start_refund(
                    amount=Decimal("6.00"),
                    sent_body={
                        "amount": {"currency_code": "EUR", "value": "6.00"}
                    },
                )
            except PayPalError as exc:
                return exc
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: start_refund(), range(2)))

        self.assertEqual(Refund.objects.count(), 1)
        self.assertEqual(sum(isinstance(item, Refund) for item in results), 1)
        self.assertEqual(sum(isinstance(item, PayPalError) for item in results), 1)
