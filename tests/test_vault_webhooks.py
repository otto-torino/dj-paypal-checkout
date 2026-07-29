"""Verified webhook state transitions for Payment Method Tokens."""

from django.test import TestCase

from paypal_checkout.exceptions import PayPalWebhookNotReady
from paypal_checkout.models import PaymentToken, SetupToken, WebhookEvent
from paypal_checkout.signals import payment_token_created, payment_token_deleted
from paypal_checkout.webhooks.handlers import dispatch

from .support import catch_signal
from .test_vault import CUSTOMER_ID, TOKEN_ID, payment_token_response


def vault_event(event_type, resource=None, *, live=False, event_id="WH-VAULT"):
    return WebhookEvent.objects.create(
        event_id=event_id,
        event_type=event_type,
        live=live,
        payload={"resource": resource or {}},
    )


class VaultWebhookTests(TestCase):
    def test_created_adopts_and_signals_a_token(self):
        event = vault_event(
            "VAULT.PAYMENT-TOKEN.CREATED", payment_token_response()
        )

        with catch_signal(payment_token_created) as received:
            self.assertEqual(dispatch(event), 1)

        token = PaymentToken.objects.get(paypal_id=TOKEN_ID)
        self.assertEqual(token.status, PaymentToken.Status.ACTIVE)
        self.assertEqual(token.customer_id, CUSTOMER_ID)
        self.assertIsNone(token.request_id)
        self.assertEqual(received[0]["payment_token"], token)
        self.assertIsNone(received[0]["target"])

    def test_created_updates_an_existing_token(self):
        token = PaymentToken.objects.start(live=False)
        token.update_from_payload(payment_token_response())
        event = vault_event(
            "VAULT.PAYMENT-TOKEN.CREATED",
            payment_token_response(
                payment_source={"paypal": {"email_address": "payer@example.com"}}
            ),
        )

        dispatch(event)

        token.refresh_from_db()
        self.assertEqual(token.payment_source_type, "paypal")
        self.assertEqual(PaymentToken.objects.count(), 1)

    def test_created_overtaking_a_pending_attempt_asks_for_a_retry(self):
        setup = SetupToken.objects.start(live=False)
        setup.update_from_payload(
            {
                "id": "SETUP-RACE",
                "status": "APPROVED",
                "customer": {"id": CUSTOMER_ID},
                "payment_source": {"card": {}},
            }
        )
        pending = PaymentToken.objects.start(live=False, setup_token=setup)
        event = vault_event(
            "VAULT.PAYMENT-TOKEN.CREATED", payment_token_response()
        )

        with self.assertRaisesMessage(PayPalWebhookNotReady, "1 pending"):
            dispatch(event)

        pending.update_from_payload(payment_token_response())
        dispatch(event)

        pending.refresh_from_db()
        self.assertEqual(pending.paypal_id, TOKEN_ID)
        self.assertEqual(pending.status, PaymentToken.Status.ACTIVE)
        self.assertEqual(PaymentToken.objects.count(), 1)

    def test_ambiguous_pending_attempts_ask_for_a_retry(self):
        for suffix in ("1", "2"):
            setup = SetupToken.objects.start(live=False)
            setup.update_from_payload(
                {
                    "id": f"SETUP-{suffix}",
                    "status": "APPROVED",
                    "customer": {"id": CUSTOMER_ID},
                    "payment_source": {"card": {}},
                }
            )
            PaymentToken.objects.start(live=False, setup_token=setup)
        event = vault_event(
            "VAULT.PAYMENT-TOKEN.CREATED", payment_token_response()
        )

        with self.assertRaisesMessage(PayPalWebhookNotReady, "2 pending"):
            dispatch(event)

        self.assertEqual(
            PaymentToken.objects.filter(paypal_id=TOKEN_ID).count(), 0
        )

    def test_missing_id_is_ignored(self):
        event = vault_event("VAULT.PAYMENT-TOKEN.CREATED", {"customer": {}})

        with catch_signal(payment_token_created) as received:
            dispatch(event)

        self.assertEqual(PaymentToken.objects.count(), 0)
        self.assertEqual(received, [])

    def test_created_without_customer_is_adopted_without_guessing(self):
        pending = PaymentToken.objects.start(live=False)
        event = vault_event(
            "VAULT.PAYMENT-TOKEN.CREATED",
            {"id": TOKEN_ID, "payment_source": {"paypal": {}}},
        )

        dispatch(event)

        adopted = PaymentToken.objects.get(paypal_id=TOKEN_ID)
        pending.refresh_from_db()
        self.assertNotEqual(adopted, pending)
        self.assertIsNone(pending.paypal_id)

    def test_wrong_environment_is_ignored(self):
        token = PaymentToken.objects.create(paypal_id=TOKEN_ID, live=True)
        event = vault_event(
            "VAULT.PAYMENT-TOKEN.CREATED", payment_token_response(), live=False
        )

        with catch_signal(payment_token_created) as received:
            dispatch(event)

        token.refresh_from_db()
        self.assertEqual(token.status, PaymentToken.Status.INITIATED)
        self.assertEqual(received, [])

    def test_deletion_initiated_adopts_a_tombstone(self):
        event = vault_event(
            "VAULT.PAYMENT-TOKEN.DELETION-INITIATED",
            payment_token_response(),
        )

        dispatch(event)

        token = PaymentToken.objects.get(paypal_id=TOKEN_ID)
        self.assertEqual(token.status, PaymentToken.Status.DELETION_PENDING)

    def test_deletion_initiated_without_id_is_ignored(self):
        event = vault_event(
            "VAULT.PAYMENT-TOKEN.DELETION-INITIATED", {"customer": {}}
        )

        dispatch(event)

        self.assertEqual(PaymentToken.objects.count(), 0)

    def test_deleted_marks_and_signals_the_token(self):
        token = PaymentToken.objects.start(live=False)
        token.update_from_payload(payment_token_response())
        event = vault_event(
            "VAULT.PAYMENT-TOKEN.DELETED",
            {"id": TOKEN_ID, "customer": {"id": CUSTOMER_ID}},
        )

        with catch_signal(payment_token_deleted) as received:
            dispatch(event)

        token.refresh_from_db()
        self.assertEqual(token.status, PaymentToken.Status.DELETED)
        self.assertIsNotNone(token.deleted_at)
        self.assertEqual(received[0]["payment_token"], token)

    def test_deleted_without_id_is_ignored(self):
        event = vault_event("VAULT.PAYMENT-TOKEN.DELETED", {"customer": {}})

        with catch_signal(payment_token_deleted) as received:
            dispatch(event)

        self.assertEqual(received, [])
