"""Payment Method Tokens v3 helpers and models."""

import json

import httpx
from django.test import TestCase

from paypal_checkout.client import PayPalClient
from paypal_checkout.exceptions import PayPalError, PayPalServerError
from paypal_checkout.models import (
    PaymentToken,
    SetupToken,
    payment_token_request_id,
    setup_token_request_id,
)
from paypal_checkout.vault import (
    PAYMENT_TOKENS_PATH,
    SETUP_TOKENS_PATH,
    create_payment_token,
    create_setup_token,
    delete_payment_token,
    fetch_payment_token,
    fetch_setup_token,
    list_payment_tokens,
    refresh_payment_token,
    refresh_setup_token,
)

from .support import FakePayPal, make_config
from .test_app.models import ShopOrder

SETUP_ID = "5C991763VB2781612"
TOKEN_ID = "8kk8451t"
CUSTOMER_ID = "BygeLlrpZF"


def setup_response(status="APPROVED", **extra):
    payload = {
        "id": SETUP_ID,
        "status": status,
        "customer": {
            "id": CUSTOMER_ID,
            "merchant_customer_id": "customer-42",
        },
        "payment_source": {
            "card": {"brand": "VISA", "last_digits": "1111", "expiry": "2027-02"}
        },
        "links": [
            {"rel": "approve", "href": "https://www.sandbox.paypal.com/approve"},
            {"rel": "self", "href": f"https://api.paypal.com/v3/vault/setup-tokens/{SETUP_ID}"},
        ],
    }
    payload.update(extra)
    return payload


def payment_token_response(**extra):
    payload = {
        "id": TOKEN_ID,
        "customer": {
            "id": CUSTOMER_ID,
            "merchant_customer_id": "customer-42",
        },
        "payment_source": {
            "card": {"brand": "VISA", "last_digits": "1111", "expiry": "2027-02"}
        },
        "links": [
            {"rel": "self", "href": f"https://api.paypal.com/v3/vault/payment-tokens/{TOKEN_ID}"}
        ],
    }
    payload.update(extra)
    return payload


def sent_body(request):
    return json.loads(request.read())


class ClientMixin:
    def setUp(self):
        self.fake = FakePayPal()
        self.client = PayPalClient(make_config(), transport=self.fake.transport)
        self.addCleanup(self.client.close)


class RequestIdTests(TestCase):
    def test_keys_are_per_row(self):
        self.assertEqual(setup_token_request_id(3), "setup-token:3:create")
        self.assertEqual(payment_token_request_id(4), "payment-token:4:create")


class SetupTokenModelTests(TestCase):
    def test_start_persists_key_environment_and_target(self):
        target = ShopOrder.objects.create(reference="VAULT-1")

        token = SetupToken.objects.start(
            live=True, target=target, merchant_customer_id="ours-1"
        )

        self.assertEqual(token.request_id, f"setup-token:{token.pk}:create")
        self.assertEqual(token.status, SetupToken.Status.INITIATED)
        self.assertTrue(token.live)
        self.assertEqual(token.target, target)
        self.assertEqual(token.merchant_customer_id, "ours-1")
        self.assertEqual(list(SetupToken.objects.pending()), [token])
        self.assertEqual(list(SetupToken.objects.for_target(target)), [token])
        self.assertIn("Initiated locally", str(token))

    def test_update_maps_safe_metadata_and_approval_url(self):
        token = SetupToken.objects.start(live=False)

        returned = token.update_from_payload(setup_response())

        self.assertIs(returned, token)
        self.assertEqual(token.paypal_id, SETUP_ID)
        self.assertEqual(token.status, SetupToken.Status.APPROVED)
        self.assertEqual(token.customer_id, CUSTOMER_ID)
        self.assertEqual(token.merchant_customer_id, "customer-42")
        self.assertEqual(token.payment_source_type, "card")
        self.assertEqual(token.approve_url(), "https://www.sandbox.paypal.com/approve")
        self.assertEqual(str(token), SETUP_ID)

    def test_update_ignores_unknown_or_missing_fields(self):
        token = SetupToken.objects.start(
            live=False, merchant_customer_id="existing"
        )
        token.update_from_payload(
            {
                "status": "NEW_STATUS",
                "customer": "invalid",
                "payment_source": [],
                "links": ["invalid", {"rel": "self", "href": "x"}],
            }
        )

        self.assertEqual(token.status, SetupToken.Status.INITIATED)
        self.assertEqual(token.merchant_customer_id, "existing")
        self.assertEqual(token.payment_source_type, "")
        self.assertIsNone(token.approve_url())

    def test_update_can_be_deferred_and_strips_card_secrets(self):
        token = SetupToken.objects.start(live=False)
        payload = setup_response(
            payment_source={
                "card": {
                    "number": "4111111111111111",
                    "security_code": "123",
                    "nested": [{"cvv": "999", "last_digits": "1111"}],
                }
            }
        )

        token.update_from_payload(payload, save=False)

        self.assertNotIn("number", token.raw["payment_source"]["card"])
        self.assertNotIn("security_code", token.raw["payment_source"]["card"])
        self.assertNotIn("cvv", token.raw["payment_source"]["card"]["nested"][0])
        token.refresh_from_db()
        self.assertIsNone(token.paypal_id)


class PaymentTokenModelTests(TestCase):
    def setUp(self):
        self.target = ShopOrder.objects.create(reference="VAULT-2")
        self.setup = SetupToken.objects.start(live=False, target=self.target)
        self.setup.update_from_payload(setup_response())

    def test_start_inherits_target_and_persists_key(self):
        token = PaymentToken.objects.start(live=False, setup_token=self.setup)

        self.assertEqual(token.request_id, f"payment-token:{token.pk}:create")
        self.assertEqual(token.target, self.target)
        self.assertEqual(token.customer_id, CUSTOMER_ID)
        self.assertEqual(token.merchant_customer_id, "customer-42")
        self.assertEqual(token.status, PaymentToken.Status.INITIATED)
        self.assertIn("Initiated locally", str(token))

    def test_explicit_target_overrides_setup_target(self):
        other = ShopOrder.objects.create(reference="VAULT-OTHER")

        token = PaymentToken.objects.start(
            live=False, setup_token=self.setup, target=other
        )

        self.assertEqual(token.target, other)

    def test_start_reuses_an_unconfirmed_exchange(self):
        first = PaymentToken.objects.start(live=False, setup_token=self.setup)

        again = PaymentToken.objects.start(live=False, setup_token=self.setup)

        self.assertEqual(again, first)
        self.assertEqual(again.request_id, first.request_id)

    def test_update_and_querysets(self):
        token = PaymentToken.objects.start(live=False, setup_token=self.setup)

        returned = token.update_from_payload(payment_token_response())

        self.assertIs(returned, token)
        self.assertEqual(str(token), TOKEN_ID)
        self.assertTrue(token.is_active)
        self.assertEqual(token.customer_id, CUSTOMER_ID)
        self.assertEqual(token.merchant_customer_id, "customer-42")
        self.assertEqual(token.payment_source_type, "card")
        self.assertEqual(list(PaymentToken.objects.active()), [token])
        self.assertEqual(
            list(PaymentToken.objects.for_customer(CUSTOMER_ID)), [token]
        )
        self.assertEqual(
            list(PaymentToken.objects.for_customer(CUSTOMER_ID, live=False)),
            [token],
        )
        self.assertEqual(
            list(PaymentToken.objects.for_customer(CUSTOMER_ID, live=True)), []
        )
        self.assertEqual(list(PaymentToken.objects.for_target(self.target)), [token])

    def test_update_can_be_deferred_and_ignores_malformed_metadata(self):
        token = PaymentToken.objects.start(live=False)

        token.update_from_payload(
            {"customer": None, "payment_source": {}, "card_number": "secret"},
            save=False,
        )

        self.assertEqual(token.status, PaymentToken.Status.ACTIVE)
        self.assertNotIn("card_number", token.raw)
        token.refresh_from_db()
        self.assertEqual(token.status, PaymentToken.Status.INITIATED)

    def test_deletion_states_are_auditable_and_sanitized(self):
        token = PaymentToken.objects.start(live=False)
        token.update_from_payload(payment_token_response())

        token.mark_deletion_pending({"id": TOKEN_ID, "cvv2": "123"})
        self.assertEqual(token.status, PaymentToken.Status.DELETION_PENDING)
        self.assertNotIn("cvv2", token.raw)

        token.mark_deleted({"id": TOKEN_ID, "number": "4111"})
        self.assertEqual(token.status, PaymentToken.Status.DELETED)
        self.assertIsNotNone(token.deleted_at)
        self.assertFalse(token.is_active)
        self.assertNotIn("number", token.raw)

    def test_marking_without_a_payload_preserves_raw(self):
        token = PaymentToken.objects.start(live=False)
        token.update_from_payload(payment_token_response())
        original = token.raw

        token.mark_deletion_pending()
        token.mark_deleted()

        self.assertEqual(token.raw, original)


class SetupTokenAPITests(ClientMixin, TestCase):
    def test_create_uses_persisted_key_and_forwards_safe_body(self):
        self.fake.queue(
            SETUP_TOKENS_PATH, httpx.Response(201, json=setup_response())
        )
        target = ShopOrder.objects.create(reference="VAULT-3")

        token = create_setup_token(
            self.client,
            payment_source={"card": {}},
            customer={"merchant_customer_id": "customer-42"},
            target=target,
            experience_context={"return_url": "https://example.com/ok"},
        )

        request = self.fake.api_requests(SETUP_TOKENS_PATH)[0]
        self.assertEqual(request.headers["paypal-request-id"], token.request_id)
        self.assertEqual(token.target, target)
        self.assertEqual(
            sent_body(request),
            {
                "payment_source": {"card": {}},
                "customer": {"merchant_customer_id": "customer-42"},
                "experience_context": {"return_url": "https://example.com/ok"},
            },
        )

    def test_create_without_customer(self):
        self.fake.queue(
            SETUP_TOKENS_PATH, httpx.Response(201, json=setup_response())
        )

        create_setup_token(self.client, payment_source={"card": {}})

        self.assertNotIn(
            "customer", sent_body(self.fake.api_requests(SETUP_TOKENS_PATH)[0])
        )

    def test_invalid_payment_sources_are_rejected_before_io(self):
        for source in (None, {}, {"card": {}, "paypal": {}}, {"card": "bad"}):
            with self.subTest(source=source):
                with self.assertRaises(PayPalError):
                    create_setup_token(self.client, payment_source=source)
        self.assertEqual(self.fake.requests, [])

    def test_invalid_customer_is_rejected(self):
        with self.assertRaisesMessage(PayPalError, "customer must be an object"):
            create_setup_token(
                self.client, payment_source={"card": {}}, customer="bad"
            )

    def test_raw_card_secrets_are_rejected_recursively(self):
        for body in (
            {"card": {"number": "4111111111111111"}},
            {"card": {"verification": [{"security_code": "123"}]}},
            {"card": {"cvv": "123"}},
        ):
            with self.subTest(body=body):
                with self.assertRaisesMessage(PayPalError, "Card Fields"):
                    create_setup_token(self.client, payment_source=body)
        self.assertEqual(SetupToken.objects.count(), 0)

    def test_failure_leaves_a_discoverable_row(self):
        self.fake.queue(
            SETUP_TOKENS_PATH,
            *[httpx.Response(500, json={"name": "INTERNAL"})] * 3,
        )

        with self.assertRaises(PayPalServerError):
            create_setup_token(self.client, payment_source={"card": {}})

        token = SetupToken.objects.pending().get()
        self.assertIsNotNone(token.request_id)
        self.assertIsNone(token.paypal_id)

    def test_fetch_and_refresh(self):
        path = f"{SETUP_TOKENS_PATH}/{SETUP_ID}"
        self.fake.queue(
            path,
            httpx.Response(200, json=setup_response()),
            httpx.Response(200, json=setup_response(status="APPROVED")),
        )

        self.assertEqual(fetch_setup_token(self.client, SETUP_ID)["id"], SETUP_ID)
        token = SetupToken.objects.start(live=False)
        token.update_from_payload(setup_response(status="CREATED"))
        refresh_setup_token(self.client, token)

        self.assertEqual(token.status, SetupToken.Status.APPROVED)

    def test_refresh_requires_confirmation_and_matching_environment(self):
        unconfirmed = SetupToken.objects.start(live=False)
        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            refresh_setup_token(self.client, unconfirmed)

        live = SetupToken.objects.start(live=True)
        with self.assertRaisesMessage(PayPalError, "belongs to live"):
            refresh_setup_token(self.client, live)


class PaymentTokenAPITests(ClientMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.setup = SetupToken.objects.start(live=False)
        self.setup.update_from_payload(setup_response())

    def test_exchange_uses_setup_token_and_persisted_key(self):
        self.fake.queue(
            PAYMENT_TOKENS_PATH,
            httpx.Response(201, json=payment_token_response()),
        )

        token = create_payment_token(
            self.client,
            setup_token=self.setup,
            customer={"id": CUSTOMER_ID},
        )

        request = self.fake.api_requests(PAYMENT_TOKENS_PATH)[0]
        self.assertEqual(request.headers["paypal-request-id"], token.request_id)
        self.assertEqual(
            sent_body(request),
            {
                "payment_source": {
                    "token": {"id": SETUP_ID, "type": "SETUP_TOKEN"}
                },
                "customer": {"id": CUSTOMER_ID},
            },
        )
        self.setup.refresh_from_db()
        self.assertEqual(self.setup.status, SetupToken.Status.VAULTED)

    def test_exchange_accepts_a_bare_setup_id_and_target(self):
        self.fake.queue(
            PAYMENT_TOKENS_PATH,
            httpx.Response(201, json=payment_token_response()),
        )
        target = ShopOrder.objects.create(reference="VAULT-4")

        token = create_payment_token(
            self.client,
            setup_token_id=SETUP_ID,
            target=target,
            customer={
                "id": CUSTOMER_ID,
                "merchant_customer_id": "customer-42",
            },
            external_reference={"id": "ours"},
        )

        self.assertIsNone(token.setup_token)
        self.assertEqual(token.target, target)
        self.assertEqual(token.customer_id, CUSTOMER_ID)
        self.assertEqual(token.merchant_customer_id, "customer-42")
        body = sent_body(self.fake.api_requests(PAYMENT_TOKENS_PATH)[0])
        self.assertEqual(body["external_reference"], {"id": "ours"})

    def test_exchange_without_customer(self):
        self.fake.queue(
            PAYMENT_TOKENS_PATH,
            httpx.Response(201, json=payment_token_response()),
        )

        create_payment_token(self.client, setup_token=self.setup)

        self.assertNotIn(
            "customer", sent_body(self.fake.api_requests(PAYMENT_TOKENS_PATH)[0])
        )

    def test_setup_token_arguments_are_validated(self):
        with self.assertRaisesMessage(PayPalError, "not both"):
            create_payment_token(
                self.client,
                setup_token=self.setup,
                setup_token_id=SETUP_ID,
            )
        with self.assertRaisesMessage(PayPalError, "needs a setup token"):
            create_payment_token(self.client)

    def test_customer_and_card_secrets_are_validated(self):
        with self.assertRaisesMessage(PayPalError, "customer must be an object"):
            create_payment_token(
                self.client, setup_token=self.setup, customer="bad"
            )
        with self.assertRaisesMessage(PayPalError, "Card Fields"):
            create_payment_token(
                self.client,
                setup_token=self.setup,
                metadata={"nested": {"card_number": "4111"}},
            )

    def test_local_setup_must_be_confirmed_and_match_environment(self):
        unconfirmed = SetupToken.objects.start(live=False)
        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            create_payment_token(self.client, setup_token=unconfirmed)

        live = SetupToken.objects.start(live=True)
        live.update_from_payload(setup_response(id="LIVE-SETUP"))
        with self.assertRaisesMessage(PayPalError, "belongs to live"):
            create_payment_token(self.client, setup_token=live)

    def test_failure_leaves_a_discoverable_row(self):
        self.fake.queue(
            PAYMENT_TOKENS_PATH,
            *[httpx.Response(500, json={"name": "INTERNAL"})] * 3,
        )

        with self.assertRaises(PayPalServerError):
            create_payment_token(self.client, setup_token=self.setup)

        token = PaymentToken.objects.get()
        self.assertEqual(token.status, PaymentToken.Status.INITIATED)
        self.assertIsNotNone(token.request_id)

    def test_retry_reuses_the_unconfirmed_row_and_key(self):
        pending = PaymentToken.objects.start(
            live=False, setup_token=self.setup
        )
        self.fake.queue(
            PAYMENT_TOKENS_PATH,
            httpx.Response(201, json=payment_token_response()),
        )

        token = create_payment_token(self.client, setup_token=self.setup)

        self.assertEqual(token.pk, pending.pk)
        request = self.fake.api_requests(PAYMENT_TOKENS_PATH)[0]
        self.assertEqual(request.headers["paypal-request-id"], pending.request_id)

    def test_a_setup_token_cannot_be_exchanged_twice(self):
        existing = PaymentToken.objects.start(
            live=False, setup_token=self.setup
        )
        existing.update_from_payload(payment_token_response())

        with self.assertRaisesMessage(PayPalError, "already has payment token"):
            create_payment_token(self.client, setup_token=self.setup)

        self.assertEqual(self.fake.requests, [])

    def test_fetch_refresh_list_and_delete(self):
        token_path = f"{PAYMENT_TOKENS_PATH}/{TOKEN_ID}"
        self.fake.queue(
            token_path,
            httpx.Response(200, json=payment_token_response()),
            httpx.Response(200, json=payment_token_response()),
            httpx.Response(204),
        )
        self.fake.queue(
            PAYMENT_TOKENS_PATH,
            httpx.Response(
                200,
                json={"payment_tokens": [payment_token_response()]},
            ),
        )

        self.assertEqual(fetch_payment_token(self.client, TOKEN_ID)["id"], TOKEN_ID)
        token = PaymentToken.objects.start(live=False)
        token.update_from_payload(payment_token_response())
        refresh_payment_token(self.client, token)
        listed = list_payment_tokens(
            self.client, CUSTOMER_ID, page_size=4, page=2, total_required=True
        )
        delete_payment_token(self.client, token)

        self.assertEqual(listed["payment_tokens"][0]["id"], TOKEN_ID)
        list_request = self.fake.api_requests(PAYMENT_TOKENS_PATH)[0]
        self.assertEqual(list_request.url.params["customer_id"], CUSTOMER_ID)
        self.assertEqual(list_request.url.params["page_size"], "4")
        self.assertEqual(list_request.url.params["page"], "2")
        self.assertEqual(list_request.url.params["total_required"], "true")
        self.assertEqual(token.status, PaymentToken.Status.DELETED)

    def test_list_defaults_and_validation(self):
        self.fake.queue(
            PAYMENT_TOKENS_PATH, httpx.Response(200, json={"payment_tokens": []})
        )
        list_payment_tokens(self.client, CUSTOMER_ID)
        params = self.fake.api_requests(PAYMENT_TOKENS_PATH)[0].url.params
        self.assertEqual(params["page_size"], "5")
        self.assertEqual(params["page"], "1")
        self.assertEqual(params["total_required"], "false")

        invalid_calls = (
            lambda: list_payment_tokens(self.client, ""),
            lambda: list_payment_tokens(self.client, 123),
            lambda: list_payment_tokens(self.client, CUSTOMER_ID, page_size=True),
            lambda: list_payment_tokens(self.client, CUSTOMER_ID, page_size=0),
            lambda: list_payment_tokens(self.client, CUSTOMER_ID, page_size=6),
            lambda: list_payment_tokens(self.client, CUSTOMER_ID, page=True),
            lambda: list_payment_tokens(self.client, CUSTOMER_ID, page=0),
            lambda: list_payment_tokens(self.client, CUSTOMER_ID, page=11),
            lambda: list_payment_tokens(
                self.client, CUSTOMER_ID, total_required="yes"
            ),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(PayPalError):
                    call()

    def test_refresh_and_delete_require_a_confirmed_matching_token(self):
        unconfirmed = PaymentToken.objects.start(live=False)
        for operation in (refresh_payment_token, delete_payment_token):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
                    operation(self.client, unconfirmed)

        live = PaymentToken.objects.start(live=True)
        for operation in (refresh_payment_token, delete_payment_token):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesMessage(PayPalError, "belongs to live"):
                    operation(self.client, live)
