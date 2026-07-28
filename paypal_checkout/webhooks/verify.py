"""Webhook signature verification.

PayPal signs webhooks with RSA-SHA256 over::

    <transmission_id>|<transmission_time>|<webhook_id>|<crc32(raw_body)>

where the CRC32 is a base-10 unsigned integer over the **exact bytes** PayPal
sent. Parsing, re-formatting or re-serialising the payload before verifying
changes that number and the signature will not match — which is why the whole
path here works on ``request.body`` and never on a parsed dict.

Two modes, chosen by ``PAYPAL['WEBHOOK_VERIFY_MODE']``:

``offline`` (default)
    Fetch the certificate from ``PAYPAL-CERT-URL`` and verify locally. No extra
    API call per webhook. The cert URL host is validated as a paypal.com host
    *before* anything is fetched, and the certificate is cached.

``api``
    Ask PayPal via ``/v1/notifications/verify-webhook-signature``. Simpler, but
    it needs the event as JSON, so it inherits exactly the re-serialisation
    fragility described above.

There is deliberately no "try offline, fall back to the API" mode: a signature
that fails verification must be rejected, never re-checked by another method
that might say yes.
"""

import base64
import hashlib
import zlib
from urllib.parse import urlparse

import httpx
from django.core.cache import caches

from ..client import Idempotency
from ..exceptions import (
    PayPalConfigurationError,
    PayPalConnectionError,
    PayPalWebhookError,
)

__all__ = [
    "SIGNATURE_HEADERS",
    "signature_headers",
    "signed_message",
    "validate_cert_url",
    "cert_cache_key",
    "fetch_certificate",
    "verify_offline",
    "verify_via_api",
    "verify_webhook",
]

#: Header names PayPal sends, mapped to the keys used internally.
SIGNATURE_HEADERS = {
    "transmission_id": "PAYPAL-TRANSMISSION-ID",
    "transmission_time": "PAYPAL-TRANSMISSION-TIME",
    "transmission_sig": "PAYPAL-TRANSMISSION-SIG",
    "cert_url": "PAYPAL-CERT-URL",
    "auth_algo": "PAYPAL-AUTH-ALGO",
}

#: Only ``SHA256withRSA`` is documented; anything else is refused rather than
#: guessed at.
SUPPORTED_AUTH_ALGOS = frozenset({"SHA256withRSA"})

CERT_CACHE_PREFIX = "paypal_checkout:cert:"
CERT_CACHE_SECONDS = 24 * 60 * 60

#: A PayPal signing certificate is ~2 KB. The cap exists because the URL comes
#: from a request header: without it, a response we were tricked into fetching
#: could stream until memory runs out.
MAX_CERTIFICATE_BYTES = 64 * 1024

VERIFY_PATH = "/v1/notifications/verify-webhook-signature"


def signature_headers(request):
    """Extract PayPal's signature headers from a Django request.

    Raises :class:`~paypal_checkout.exceptions.PayPalWebhookError` if any is
    missing — an unsigned request is not a webhook.
    """
    values = {}
    missing = []
    for key, header in SIGNATURE_HEADERS.items():
        value = request.headers.get(header)
        if not value:
            missing.append(header)
        else:
            values[key] = value
    if missing:
        raise PayPalWebhookError(f"missing signature header(s): {', '.join(missing)}")
    return values


def signed_message(transmission_id, transmission_time, webhook_id, body):
    """Build the exact bytes PayPal signed."""
    if not isinstance(body, (bytes, bytearray)):
        raise PayPalWebhookError(
            "the signed message must be built from the raw request body bytes, "
            f"got {type(body).__name__}."
        )
    checksum = zlib.crc32(bytes(body)) & 0xFFFFFFFF
    return f"{transmission_id}|{transmission_time}|{webhook_id}|{checksum}".encode()


def validate_cert_url(url):
    """Refuse to fetch a certificate from anywhere but PayPal over HTTPS.

    This URL arrives in a request header, i.e. from an untrusted source. Without
    these checks a forged webhook could point us at the attacker's certificate
    and then every forgery would verify. Refused: any scheme but https,
    credentials embedded in the URL, a host that is not ``paypal.com`` or a
    ``.paypal.com`` subdomain (so ``paypal.com.evil.example`` does not slip
    through), and any port other than 443.
    """
    try:
        parsed = urlparse(url or "")
        port = parsed.port
    except ValueError as exc:
        raise PayPalWebhookError(f"certificate URL {url!r} is not a valid URL: {exc}") from None

    if parsed.scheme != "https":
        raise PayPalWebhookError(f"certificate URL must be https, got {url!r}.")
    if parsed.username or parsed.password:
        raise PayPalWebhookError("certificate URL must not carry credentials.")
    host = (parsed.hostname or "").lower()
    if host != "paypal.com" and not host.endswith(".paypal.com"):
        raise PayPalWebhookError(f"certificate URL host {host!r} is not a paypal.com host.")
    if port not in (None, 443):
        raise PayPalWebhookError(f"certificate URL must use port 443, got {port}.")
    return url


def cert_cache_key(cert_url):
    """Cache key for a certificate URL.

    Hashed, so an attacker-supplied URL cannot produce an unbounded or
    backend-hostile key.
    """
    digest = hashlib.sha256(cert_url.encode("utf-8")).hexdigest()[:32]
    return f"{CERT_CACHE_PREFIX}{digest}"


def fetch_certificate(cert_url, *, config, transport=None):
    """Return the PEM certificate for ``cert_url``, cached for a day.

    Redirects are **not** followed: a redirect off a validated paypal.com URL
    would defeat :func:`validate_cert_url`, so anything but ``200`` is refused.
    The body is capped at :data:`MAX_CERTIFICATE_BYTES`.
    """
    validate_cert_url(cert_url)
    cache = caches[config.cache_alias]
    key = cert_cache_key(cert_url)
    cached = cache.get(key)
    if cached:
        return cached

    try:
        with httpx.Client(
            transport=transport, timeout=config.timeout, follow_redirects=False
        ) as client:
            with client.stream("GET", cert_url) as response:
                if response.status_code != 200:
                    raise PayPalWebhookError(
                        f"could not fetch {cert_url}: HTTP {response.status_code} "
                        "(redirects are not followed)"
                    )
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_CERTIFICATE_BYTES:
                        raise PayPalWebhookError(
                            f"certificate at {cert_url} exceeds "
                            f"{MAX_CERTIFICATE_BYTES} bytes; refusing to read it."
                        )
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise PayPalConnectionError(f"could not fetch {cert_url}: {exc}") from exc

    pem = b"".join(chunks)
    if not pem:
        raise PayPalWebhookError(f"certificate at {cert_url} is empty.")
    cache.set(key, pem, CERT_CACHE_SECONDS)
    return pem


def _load_public_key(pem):
    try:
        from cryptography import x509
    except ImportError:  # pragma: no cover - depends on the install
        raise PayPalConfigurationError(
            "offline webhook verification needs the 'cryptography' package. "
            "Install dj-paypal-checkout[crypto], or set "
            "PAYPAL['WEBHOOK_VERIFY_MODE'] = 'api'."
        ) from None
    try:
        return x509.load_pem_x509_certificate(pem).public_key()
    except Exception as exc:
        raise PayPalWebhookError(f"could not read PayPal's certificate: {exc}") from exc


def verify_offline(*, config, headers, body, transport=None):
    """Verify the signature locally. Returns ``True``/``False``.

    Raises rather than returning ``False`` when verification could not be
    *attempted* (bad cert URL, unreachable cert, missing crypto library) — that
    is a different situation from "this signature is wrong" and must not be
    mistaken for it.
    """
    algo = headers["auth_algo"]
    if algo not in SUPPORTED_AUTH_ALGOS:
        raise PayPalWebhookError(f"unsupported PAYPAL-AUTH-ALGO {algo!r}.")

    webhook_id = config.require_webhook_id()
    message = signed_message(
        headers["transmission_id"], headers["transmission_time"], webhook_id, body
    )
    pem = fetch_certificate(headers["cert_url"], config=config, transport=transport)
    public_key = _load_public_key(pem)

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        signature = base64.b64decode(headers["transmission_sig"], validate=True)
    except Exception:
        return False

    try:
        public_key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True


def verify_via_api(client, *, headers, event):
    """Ask PayPal whether the signature is valid. Returns ``True``/``False``.

    ``event`` is the parsed webhook body. The transmission id is used as the
    idempotency key: it is naturally stable for this transmission, and the call
    itself has no side effects, hence ``NOT_APPLICABLE``.
    """
    payload = {
        "auth_algo": headers["auth_algo"],
        "cert_url": validate_cert_url(headers["cert_url"]),
        "transmission_id": headers["transmission_id"],
        "transmission_sig": headers["transmission_sig"],
        "transmission_time": headers["transmission_time"],
        "webhook_id": client.config.require_webhook_id(),
        "webhook_event": event,
    }
    response = client.post(
        VERIFY_PATH,
        json=payload,
        request_id=f"webhook-verify:{headers['transmission_id']}",
        idempotency=Idempotency.NOT_APPLICABLE,
    )
    return response.get("verification_status") == "SUCCESS"


def verify_webhook(*, config, headers, body, event=None, client=None, transport=None):
    """Verify a webhook using the configured mode."""
    if config.webhook_verify_mode == "api":
        if client is None:
            from ..client import PayPalClient

            with PayPalClient(config, transport=transport) as api_client:
                return verify_via_api(api_client, headers=headers, event=event)
        return verify_via_api(client, headers=headers, event=event)
    return verify_offline(config=config, headers=headers, body=body, transport=transport)
