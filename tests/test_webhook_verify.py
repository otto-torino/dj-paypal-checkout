"""Signature verification — exercised with real RSA signatures."""

import zlib

import httpx
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase

from paypal_checkout.client import PayPalClient
from paypal_checkout.exceptions import (
    PayPalConfigurationError,
    PayPalConnectionError,
    PayPalWebhookError,
)
from paypal_checkout.webhooks.verify import (
    VERIFY_PATH,
    fetch_certificate,
    signature_headers,
    signed_message,
    validate_cert_url,
    verify_offline,
    verify_via_api,
    verify_webhook,
)

from .support import FakePayPal, WebhookSigner, make_config

BODY = b'{"id":"WH-1","event_type":"PAYMENT.CAPTURE.COMPLETED"}'


class SignedMessageTests(SimpleTestCase):
    def test_format_is_pipe_separated_with_a_crc32(self):
        message = signed_message("TR-1", "2026-07-28T10:00:00Z", "WH-ID", BODY)

        expected_crc = zlib.crc32(BODY) & 0xFFFFFFFF
        self.assertEqual(
            message, f"TR-1|2026-07-28T10:00:00Z|WH-ID|{expected_crc}".encode()
        )

    def test_crc32_is_an_unsigned_base_10_integer(self):
        """Signed CRC32 values would be negative on some inputs and never match."""
        body = b"\xff" * 64
        message = signed_message("a", "b", "c", body).decode()
        self.assertGreaterEqual(int(message.rsplit("|", 1)[1]), 0)

    def test_a_single_changed_byte_changes_the_message(self):
        self.assertNotEqual(
            signed_message("a", "b", "c", BODY),
            signed_message("a", "b", "c", BODY.replace(b"WH-1", b"WH-2")),
        )

    def test_a_parsed_body_is_refused(self):
        """Re-serialising the payload is the classic cause of failures."""
        with self.assertRaisesMessage(PayPalWebhookError, "raw request body bytes"):
            signed_message("a", "b", "c", '{"id":"WH-1"}')


class ValidateCertUrlTests(SimpleTestCase):
    def test_paypal_hosts_are_accepted(self):
        for url in (
            "https://api.paypal.com/v1/notifications/certs/CERT-1",
            "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
            "https://paypal.com/certs/CERT-1",
        ):
            with self.subTest(url=url):
                self.assertEqual(validate_cert_url(url), url)

    def test_http_is_refused(self):
        with self.assertRaisesMessage(PayPalWebhookError, "must be https"):
            validate_cert_url("http://api.paypal.com/certs/CERT-1")

    def test_foreign_hosts_are_refused(self):
        """Otherwise a forged header points us at the attacker's certificate."""
        for url in (
            "https://evil.example/certs/CERT-1",
            "https://paypal.com.evil.example/certs/CERT-1",
            "https://notpaypal.com/certs/CERT-1",
            "",
        ):
            with self.subTest(url=url):
                with self.assertRaises(PayPalWebhookError):
                    validate_cert_url(url)


class SignatureHeadersTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.signer = WebhookSigner()

    def test_all_headers_are_extracted(self):
        request = self.factory.post("/", data=BODY, content_type="application/json",
                                    headers=self.signer.headers(BODY))

        headers = signature_headers(request)

        self.assertEqual(headers["transmission_id"], "TR-1")
        self.assertEqual(headers["auth_algo"], "SHA256withRSA")
        self.assertEqual(headers["cert_url"], self.signer.CERT_URL)

    def test_missing_headers_are_all_named(self):
        request = self.factory.post("/", data=BODY, content_type="application/json")

        with self.assertRaises(PayPalWebhookError) as ctx:
            signature_headers(request)

        for header in ("PAYPAL-TRANSMISSION-ID", "PAYPAL-TRANSMISSION-SIG"):
            self.assertIn(header, str(ctx.exception))


class FetchCertificateTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.signer = WebhookSigner()
        self.config = make_config(webhook_id="WH-TEST-1")

    def _transport(self, *responses):
        return FakePayPal().queue(
            "/v1/notifications/certs/CERT-360caa42-fca2a594", *responses
        ).transport

    def test_certificate_is_fetched_then_cached(self):
        fake = FakePayPal().queue(
            "/v1/notifications/certs/CERT-360caa42-fca2a594",
            httpx.Response(200, content=self.signer.certificate_pem),
        )

        first = fetch_certificate(self.signer.CERT_URL, config=self.config, transport=fake.transport)
        second = fetch_certificate(self.signer.CERT_URL, config=self.config, transport=fake.transport)

        self.assertEqual(first, self.signer.certificate_pem)
        self.assertEqual(second, self.signer.certificate_pem)
        self.assertEqual(len(fake.requests), 1, "the second call must hit the cache")

    def test_a_foreign_url_is_never_fetched(self):
        fake = FakePayPal()

        with self.assertRaises(PayPalWebhookError):
            fetch_certificate("https://evil.example/c", config=self.config, transport=fake.transport)

        self.assertEqual(fake.requests, [])

    def test_an_error_response_is_reported(self):
        transport = self._transport(httpx.Response(404))

        with self.assertRaisesMessage(PayPalWebhookError, "HTTP 404"):
            fetch_certificate(self.signer.CERT_URL, config=self.config, transport=transport)

    def test_an_unreachable_host_is_reported(self):
        transport = self._transport(httpx.ConnectError("down"))

        with self.assertRaises(PayPalConnectionError):
            fetch_certificate(self.signer.CERT_URL, config=self.config, transport=transport)


class VerifyOfflineTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.signer = WebhookSigner()
        self.config = make_config(webhook_id=self.signer.webhook_id)
        self.signer.prime_certificate_cache(self.config)

    def verify(self, body=BODY, **header_kwargs):
        values = self.signer.values(body, **header_kwargs)
        return verify_offline(config=self.config, headers=values, body=body)

    def test_a_genuine_signature_verifies(self):
        self.assertTrue(self.verify())

    def test_a_tampered_body_does_not_verify(self):
        values = self.signer.values(BODY)
        tampered = BODY.replace(b"WH-1", b"WH-9")

        self.assertFalse(
            verify_offline(config=self.config, headers=values, body=tampered)
        )

    def test_a_tampered_signature_does_not_verify(self):
        genuine = self.signer.headers(BODY)["PAYPAL-TRANSMISSION-SIG"]
        forged = ("A" if genuine[0] != "A" else "B") + genuine[1:]

        self.assertFalse(self.verify(signature=forged))

    def test_a_non_base64_signature_does_not_verify(self):
        self.assertFalse(self.verify(signature="not base64 at all!!"))

    def test_another_webhook_id_does_not_verify(self):
        """The webhook id is part of the signed message, so it is bound to us."""
        self.assertFalse(self.verify(webhook_id="WH-SOMEONE-ELSE"))

    def test_a_replayed_transmission_id_does_not_verify(self):
        values = self.signer.values(BODY)
        values["transmission_id"] = "TR-OTHER"

        self.assertFalse(
            verify_offline(config=self.config, headers=values, body=BODY)
        )

    def test_an_unsupported_algorithm_is_refused_not_guessed(self):
        with self.assertRaisesMessage(PayPalWebhookError, "unsupported PAYPAL-AUTH-ALGO"):
            self.verify(auth_algo="SHA1withRSA")

    def test_a_missing_webhook_id_is_a_configuration_error(self):
        config = make_config(webhook_id="")
        values = self.signer.values(BODY)

        with self.assertRaisesMessage(PayPalConfigurationError, "WEBHOOK_ID"):
            verify_offline(config=config, headers=values, body=BODY)

    def test_a_broken_certificate_is_reported(self):
        cache.clear()
        fake = FakePayPal().queue(
            "/v1/notifications/certs/CERT-360caa42-fca2a594",
            httpx.Response(200, content=b"-----BEGIN CERTIFICATE-----\nnope\n"),
        )
        values = self.signer.values(BODY)

        with self.assertRaisesMessage(PayPalWebhookError, "could not read PayPal's certificate"):
            verify_offline(
                config=self.config, headers=values, body=BODY, transport=fake.transport
            )


class VerifyViaApiTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.signer = WebhookSigner()
        self.event = {"id": "WH-1"}

    def call(self, response, **config_kwargs):
        fake = FakePayPal().queue(VERIFY_PATH, response)
        config = make_config(webhook_id=self.signer.webhook_id, **config_kwargs)
        with PayPalClient(config, transport=fake.transport) as client:
            result = verify_via_api(
                client, headers=self.signer.values(BODY), event=self.event
            )
        return result, fake

    def test_success(self):
        result, _ = self.call(httpx.Response(200, json={"verification_status": "SUCCESS"}))
        self.assertTrue(result)

    def test_failure(self):
        result, _ = self.call(httpx.Response(200, json={"verification_status": "FAILURE"}))
        self.assertFalse(result)

    def test_payload_carries_everything_paypal_needs(self):
        import json

        _, fake = self.call(httpx.Response(200, json={"verification_status": "SUCCESS"}))

        body = json.loads(fake.api_requests(VERIFY_PATH)[0].read())
        self.assertEqual(body["webhook_id"], self.signer.webhook_id)
        self.assertEqual(body["transmission_id"], "TR-1")
        self.assertEqual(body["webhook_event"], self.event)

    def test_the_transmission_id_is_the_idempotency_key(self):
        _, fake = self.call(httpx.Response(200, json={"verification_status": "SUCCESS"}))

        self.assertEqual(
            fake.api_requests(VERIFY_PATH)[0].headers["paypal-request-id"],
            "webhook-verify:TR-1",
        )

    def test_it_works_under_strict_idempotency(self):
        """It is declared NOT_APPLICABLE, so strict mode cannot trip on it."""
        result, _ = self.call(
            httpx.Response(200, json={"verification_status": "SUCCESS"}),
            strict_idempotency=True,
        )
        self.assertTrue(result)

    def test_a_foreign_cert_url_is_refused_before_calling(self):
        fake = FakePayPal()
        config = make_config(webhook_id="WH-TEST-1")
        values = self.signer.values(BODY)
        values["cert_url"] = "https://evil.example/c"

        with PayPalClient(config, transport=fake.transport) as client:
            with self.assertRaises(PayPalWebhookError):
                verify_via_api(client, headers=values, event=self.event)

        self.assertEqual(fake.requests, [])


class VerifyWebhookDispatchTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.signer = WebhookSigner()

    def test_offline_mode_is_the_default(self):
        config = make_config(webhook_id=self.signer.webhook_id)
        self.assertEqual(config.webhook_verify_mode, "offline")
        self.signer.prime_certificate_cache(config)
        fake = FakePayPal()

        result = verify_webhook(
            config=config,
            headers=self.signer.values(BODY),
            body=BODY,
            transport=fake.transport,
        )

        self.assertTrue(result)
        self.assertEqual(fake.requests, [], "no API call in offline mode")

    def test_api_mode_calls_paypal(self):
        config = make_config(webhook_id=self.signer.webhook_id, webhook_verify_mode="api")
        fake = FakePayPal().queue(
            VERIFY_PATH, httpx.Response(200, json={"verification_status": "SUCCESS"})
        )

        result = verify_webhook(
            config=config,
            headers=self.signer.values(BODY),
            body=BODY,
            event={"id": "WH-1"},
            transport=fake.transport,
        )

        self.assertTrue(result)
        self.assertEqual(len(fake.api_requests(VERIFY_PATH)), 1)

    def test_api_mode_accepts_an_existing_client(self):
        config = make_config(webhook_id=self.signer.webhook_id, webhook_verify_mode="api")
        fake = FakePayPal().queue(
            VERIFY_PATH, httpx.Response(200, json={"verification_status": "SUCCESS"})
        )

        with PayPalClient(config, transport=fake.transport) as client:
            result = verify_webhook(
                config=config,
                headers=self.signer.values(BODY),
                body=BODY,
                event={"id": "WH-1"},
                client=client,
            )

        self.assertTrue(result)
