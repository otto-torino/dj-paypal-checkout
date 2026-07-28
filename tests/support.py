"""Test helpers.

Every HTTP interaction in the suite goes through :class:`FakePayPal`, an
``httpx`` mock transport — no test may ever touch the real API, not even the
sandbox.
"""

import base64
from collections import defaultdict, deque
from contextlib import contextmanager

import httpx

from paypal_checkout.auth import TOKEN_PATH
from paypal_checkout.config import PayPalConfig


def make_config(**overrides):
    """A config suitable for tests: dummy creds, no backoff sleeps."""
    values = {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "live": False,
        "retry_backoff": 0,
        "max_retries": 2,
    }
    values.update(overrides)
    return PayPalConfig(**values)


_SIGNING_KEY = None
_CERTIFICATE_PEM = None


def signing_material():
    """A real RSA key and self-signed certificate, generated once per run.

    The verifier is security code, so the tests exercise actual RSA-SHA256
    signatures rather than a stubbed-out "signature is valid" answer.
    """
    global _SIGNING_KEY, _CERTIFICATE_PEM
    if _SIGNING_KEY is None:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        _SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "messageverificationcerts.paypal.com")]
        )
        # A fixed validity window: the verifier checks the signature, not the
        # certificate dates, and tests must not depend on the clock.
        not_before = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(_SIGNING_KEY.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_before + datetime.timedelta(days=7300))
            .sign(_SIGNING_KEY, hashes.SHA256())
        )
        _CERTIFICATE_PEM = certificate.public_bytes(serialization.Encoding.PEM)
    return _SIGNING_KEY, _CERTIFICATE_PEM


def as_signature_values(headers):
    """Translate HTTP header names into the keys the verifier uses."""
    from paypal_checkout.webhooks.verify import SIGNATURE_HEADERS

    return {key: headers[header] for key, header in SIGNATURE_HEADERS.items()}


class WebhookSigner:
    """Produce the headers PayPal would send for a given raw body."""

    CERT_URL = "https://api.paypal.com/v1/notifications/certs/CERT-360caa42-fca2a594"

    def __init__(self, webhook_id="WH-TEST-1", cert_url=None):
        self.webhook_id = webhook_id
        self.cert_url = cert_url or self.CERT_URL
        self.key, self.certificate_pem = signing_material()

    def sign(self, message):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        return base64.b64encode(
            self.key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        ).decode()

    def headers(
        self,
        body,
        *,
        transmission_id="TR-1",
        transmission_time="2026-07-28T10:00:00Z",
        webhook_id=None,
        auth_algo="SHA256withRSA",
        signature=None,
    ):
        from paypal_checkout.webhooks.verify import signed_message

        message = signed_message(
            transmission_id, transmission_time, webhook_id or self.webhook_id, body
        )
        return {
            "PAYPAL-TRANSMISSION-ID": transmission_id,
            "PAYPAL-TRANSMISSION-TIME": transmission_time,
            "PAYPAL-TRANSMISSION-SIG": signature or self.sign(message),
            "PAYPAL-CERT-URL": self.cert_url,
            "PAYPAL-AUTH-ALGO": auth_algo,
        }

    def values(self, body, **kwargs):
        """The same signature data keyed the way the verifier expects it."""
        return as_signature_values(self.headers(body, **kwargs))

    def prime_certificate_cache(self, config):
        """Put the certificate in the cache so no HTTP fetch is attempted."""
        from django.core.cache import caches

        from paypal_checkout.webhooks.verify import cert_cache_key

        caches[config.cache_alias].set(
            cert_cache_key(self.cert_url), self.certificate_pem, 3600
        )


@contextmanager
def catch_signal(signal):
    """Collect the kwargs of every ``signal`` sent inside the block."""
    received = []

    def handler(sender, **kwargs):
        kwargs["sender"] = sender
        received.append(kwargs)

    signal.connect(handler, weak=False)
    try:
        yield received
    finally:
        signal.disconnect(handler)


class FakePayPal:
    """Queue canned responses per path and record what was requested.

    The token endpoint answers automatically unless responses are queued for
    it, so tests only describe the calls they actually care about.
    """

    def __init__(self, *, expires_in=32400):
        self.expires_in = expires_in
        self.requests = []
        self.token_requests = []
        self._queues = defaultdict(deque)

    def queue(self, path, *responses):
        self._queues[path].extend(responses)
        return self

    def queue_token(self, *responses):
        return self.queue(TOKEN_PATH, *responses)

    @property
    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request):
        path = request.url.path
        self.requests.append(request)
        queued = self._queues[path]

        if path == TOKEN_PATH:
            self.token_requests.append(request)
            if queued:
                return self._respond(queued.popleft())
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{len(self.token_requests)}",
                    "token_type": "Bearer",
                    "expires_in": self.expires_in,
                },
            )

        if not queued:
            raise AssertionError(f"Unexpected request: {request.method} {path}")
        return self._respond(queued.popleft())

    @staticmethod
    def _respond(queued):
        # A queued entry may be an exception to raise instead of a response,
        # which is how transport failures are simulated.
        if isinstance(queued, Exception):
            raise queued
        return queued

    def api_requests(self, path=None):
        """Requests other than token requests, optionally filtered by path."""
        return [
            request
            for request in self.requests
            if request.url.path != TOKEN_PATH
            and (path is None or request.url.path == path)
        ]
