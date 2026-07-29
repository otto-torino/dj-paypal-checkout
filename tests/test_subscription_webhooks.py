"""Webhook state transitions and recurring payments for Subscriptions v1."""

from decimal import Decimal

from django.test import TestCase

from paypal_checkout.exceptions import PayPalAmountError
from paypal_checkout.models import (
    Plan,
    Product,
    Subscription,
    SubscriptionPayment,
)
from paypal_checkout.signals import (
    subscription_activated,
    subscription_cancelled,
    subscription_expired,
    subscription_payment_completed,
    subscription_payment_failed,
    subscription_suspended,
)
from paypal_checkout.webhooks.handlers import (
    _apply_subscription_status,
    dispatch,
    get_handlers,
)

from .support import catch_signal
from .test_app.models import ShopOrder
from .test_subscriptions import (
    PLAN_ID,
    PRODUCT_ID,
    SUB_ID,
    plan_response,
    product_response,
    subscription_response,
)
from .test_webhooks import event_payload, store_event


class SubscriptionWebhookTests(TestCase):
    def setUp(self):
        self.product = Product.objects.start(name="Membership", live=False)
        self.product.update_from_payload(product_response())
        self.plan = Plan.objects.start(name="Monthly", live=False, product=self.product)
        self.plan.update_from_payload(plan_response("ACTIVE"))
        self.target = ShopOrder.objects.create(reference="SUB-1", total="9.99")
        self.subscription = Subscription.objects.start(
            live=False, plan=self.plan, target=self.target
        )
        self.subscription.update_from_payload(subscription_response("ACTIVE"))

    def event(self, event_type, resource, event_id="WH-SUB-1"):
        payload = event_payload(event_type, resource=resource)
        payload["id"] = event_id
        return store_event(payload)

    def test_subscription_event_types_are_registered(self):
        for event_type in (
            "BILLING.SUBSCRIPTION.CREATED",
            "BILLING.SUBSCRIPTION.UPDATED",
            "BILLING.SUBSCRIPTION.ACTIVATED",
            "BILLING.SUBSCRIPTION.SUSPENDED",
            "BILLING.SUBSCRIPTION.CANCELLED",
            "BILLING.SUBSCRIPTION.EXPIRED",
            "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
            "PAYMENT.SALE.COMPLETED",
        ):
            with self.subTest(event_type=event_type):
                self.assertTrue(get_handlers(event_type))

    def test_created_and_updated_refresh_the_local_row(self):
        created = subscription_response(
            "APPROVAL_PENDING", custom_id="CREATED-BY-WEBHOOK"
        )
        dispatch(self.event("BILLING.SUBSCRIPTION.CREATED", created))

        self.subscription.refresh_from_db()
        self.assertEqual(
            self.subscription.status, Subscription.Status.APPROVAL_PENDING
        )
        self.assertEqual(self.subscription.custom_id, "CREATED-BY-WEBHOOK")

        updated = subscription_response("ACTIVE", quantity="3")
        dispatch(
            self.event(
                "BILLING.SUBSCRIPTION.UPDATED", updated, event_id="WH-SUB-2"
            )
        )

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.quantity, 3)

    def test_lifecycle_events_force_status_and_send_signals(self):
        cases = (
            (
                "BILLING.SUBSCRIPTION.ACTIVATED",
                Subscription.Status.ACTIVE,
                subscription_activated,
            ),
            (
                "BILLING.SUBSCRIPTION.SUSPENDED",
                Subscription.Status.SUSPENDED,
                subscription_suspended,
            ),
            (
                "BILLING.SUBSCRIPTION.CANCELLED",
                Subscription.Status.CANCELLED,
                subscription_cancelled,
            ),
            (
                "BILLING.SUBSCRIPTION.EXPIRED",
                Subscription.Status.EXPIRED,
                subscription_expired,
            ),
        )
        for index, (event_type, expected, signal) in enumerate(cases):
            with self.subTest(event_type=event_type):
                resource = {
                    "id": SUB_ID,
                    "status": "UNKNOWN",
                    "status_change_note": "PayPal changed it",
                }
                with catch_signal(signal) as received:
                    dispatch(
                        self.event(
                            event_type, resource, event_id=f"WH-LIFE-{index}"
                        )
                    )

                self.subscription.refresh_from_db()
                self.assertEqual(self.subscription.status, expected)
                self.assertEqual(received[0]["target"], self.target)
                self.assertEqual(received[0]["reason"], "PayPal changed it")

    def test_unknown_or_unidentified_subscriptions_are_ignored(self):
        dispatch(
            self.event(
                "BILLING.SUBSCRIPTION.UPDATED",
                {"id": "I-FOREIGN", "status": "ACTIVE"},
            )
        )
        dispatch(
            self.event(
                "BILLING.SUBSCRIPTION.CREATED",
                {"status": "ACTIVE"},
                event_id="WH-SUB-2",
            )
        )

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.raw["id"], SUB_ID)

    def test_an_unknown_lifecycle_subscription_is_ignored(self):
        with catch_signal(subscription_activated) as received:
            dispatch(
                self.event(
                    "BILLING.SUBSCRIPTION.ACTIVATED",
                    {"id": "I-FOREIGN", "status": "ACTIVE"},
                )
            )

        self.assertEqual(received, [])

    def test_status_can_be_applied_without_a_signal(self):
        event = self.event(
            "PROJECT.INTERNAL.SUBSCRIPTION.SYNC",
            {"id": SUB_ID, "status": "UNKNOWN"},
        )

        _apply_subscription_status(event, Subscription.Status.SUSPENDED)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, Subscription.Status.SUSPENDED)

    def test_failed_payment_refreshes_and_signals(self):
        resource = {
            "id": SUB_ID,
            "status": "SUSPENDED",
            "status_change_note": "threshold reached",
        }

        with catch_signal(subscription_payment_failed) as received:
            dispatch(self.event("BILLING.SUBSCRIPTION.PAYMENT.FAILED", resource))

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, Subscription.Status.SUSPENDED)
        self.assertEqual(received[0]["raw"], resource)
        self.assertEqual(received[0]["target"], self.target)

    def test_failed_payment_for_an_unknown_subscription_is_ignored(self):
        with catch_signal(subscription_payment_failed) as received:
            dispatch(
                self.event(
                    "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
                    {"id": "I-FOREIGN", "status": "SUSPENDED"},
                )
            )

        self.assertEqual(received, [])

    def test_completed_sale_creates_one_payment_and_signals(self):
        resource = {
            "id": "SALE-1",
            "billing_agreement_id": SUB_ID,
            "state": "completed",
            "amount": {"total": "9.99", "currency": "eur"},
        }

        with catch_signal(subscription_payment_completed) as received:
            dispatch(self.event("PAYMENT.SALE.COMPLETED", resource))

        payment = SubscriptionPayment.objects.get()
        self.assertEqual(payment.subscription, self.subscription)
        self.assertEqual(payment.amount, Decimal("9.99"))
        self.assertEqual(payment.currency, "EUR")
        self.assertTrue(payment.is_successful)
        self.assertTrue(received[0]["created"])
        self.assertEqual(received[0]["payment"], payment)

        with catch_signal(subscription_payment_completed) as redelivery:
            dispatch(
                self.event(
                    "PAYMENT.SALE.COMPLETED",
                    resource,
                    event_id="WH-SALE-REDLV",
                )
            )

        self.assertEqual(SubscriptionPayment.objects.count(), 1)
        self.assertFalse(redelivery[0]["created"])

    def test_currency_code_value_amount_shape_is_accepted(self):
        resource = {
            "id": "SALE-2",
            "billing_agreement_id": SUB_ID,
            "state": "COMPLETED",
            "amount": {"value": "12.50", "currency_code": "USD"},
        }

        dispatch(self.event("PAYMENT.SALE.COMPLETED", resource))

        payment = SubscriptionPayment.objects.get()
        self.assertEqual(payment.amount, Decimal("12.50"))
        self.assertEqual(payment.currency, "USD")

    def test_a_sale_without_a_subscription_or_id_is_ignored(self):
        dispatch(
            self.event(
                "PAYMENT.SALE.COMPLETED",
                {"id": "SALE-FOREIGN"},
            )
        )
        dispatch(
            self.event(
                "PAYMENT.SALE.COMPLETED",
                {"id": "SALE-UNKNOWN", "billing_agreement_id": "I-FOREIGN"},
                event_id="WH-SALE-2",
            )
        )
        dispatch(
            self.event(
                "PAYMENT.SALE.COMPLETED",
                {"billing_agreement_id": SUB_ID},
                event_id="WH-SALE-3",
            )
        )

        self.assertFalse(SubscriptionPayment.objects.exists())

    def test_an_unreadable_sale_amount_raises_instead_of_recording_zero(self):
        resources = (
            {
                "id": "SALE-BAD-1",
                "billing_agreement_id": SUB_ID,
                "amount": {"total": "not-money", "currency": "EUR"},
            },
            {
                "id": "SALE-BAD-2",
                "billing_agreement_id": SUB_ID,
            },
        )
        for index, resource in enumerate(resources):
            with self.subTest(resource=resource):
                with self.assertRaises(PayPalAmountError):
                    dispatch(
                        self.event(
                            "PAYMENT.SALE.COMPLETED",
                            resource,
                            event_id=f"WH-BAD-{index}",
                        )
                    )

        self.assertFalse(SubscriptionPayment.objects.exists())
