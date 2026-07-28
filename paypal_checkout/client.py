"""The HTTP client — sync and async, same surface.

Hand-written on top of ``httpx`` rather than wrapping PayPal's
``paypal-server-sdk``: that SDK is sync-only and covers neither webhook
signature verification nor the subscription plans/products catalog, both of
which this library needs (see PROGRESS.md, decision 1).

**Retry safety is the important part of this module.** A blind retry of a
capture would charge the buyer twice, so a non-idempotent request is retried
*only* when the caller supplied a ``PayPal-Request-Id`` — the header PayPal
uses to deduplicate. Without it, a failed POST is raised, never repeated.
"""

import asyncio
import enum
import logging
import random
import re
import time

import httpx

from .auth import aget_access_token, get_access_token
from .config import get_config
from .exceptions import (
    PayPalAPIError,
    PayPalConnectionError,
    PayPalIdempotencyError,
    error_from_response,
    retry_after_seconds,
)

__all__ = ["PayPalClient", "AsyncPayPalClient", "Idempotency", "endpoint_label"]

logger = logging.getLogger(__name__)


class Idempotency(enum.Enum):
    """Whether an *operation* needs an idempotency key — a property of the
    operation, not something inferred from the HTTP verb.

    "Mutating method ⇒ needs a key" is only a heuristic, and
    ``POST /v1/notifications/verify-webhook-signature`` is the counterexample: a
    POST that changes nothing and should be retried freely. Callers of the raw
    client may leave this unset and get the heuristic; the higher-level helpers
    declare it explicitly.
    """

    #: Money moves. Strict mode refuses the call without a ``request_id``.
    REQUIRED = "required"

    #: A key helps, but its absence is not a defect: no warning, and still not
    #: retried without one.
    OPTIONAL = "optional"

    #: Side-effect-free: retryable even with no key, and never reported.
    NOT_APPLICABLE = "not_applicable"

#: Methods that can be repeated without changing the outcome.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

#: Statuses worth retrying: rate limiting and PayPal-side failures.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Upper bound for a single backoff sleep, in seconds.
MAX_BACKOFF = 30.0


#: ``v1``/``v2``/``v3`` are version segments, not resource ids.
_VERSION_SEGMENT = re.compile(r"^v\d+$")


def _user_agent():
    # Imported lazily to avoid a circular import at package init time.
    from . import __version__

    return f"dj-paypal-checkout/{__version__}"


def endpoint_label(url):
    """Templated path for logs and metrics: ``/v2/checkout/orders/{id}/capture``.

    Resource ids are replaced so the label has low cardinality (usable as a
    metric dimension) and carries no per-transaction identifiers.
    """
    path = httpx.URL(url).path if "//" in str(url) else str(url)
    segments = []
    for segment in path.strip("/").split("/"):
        if segment and not _VERSION_SEGMENT.match(segment) and any(c.isdigit() for c in segment):
            segments.append("{id}")
        else:
            segments.append(segment)
    return "/" + "/".join(segments)


class _BasePayPalClient:
    """Shared, side-effect-free decision logic for both clients."""

    def __init__(self, config=None, *, transport=None):
        self.config = config or get_config()
        self._transport = transport

    # -- request shaping ---------------------------------------------------

    def _url(self, path):
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.config.base_url}/{path.lstrip('/')}"

    def _headers(self, token, *, request_id=None, extra=None):
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _user_agent(),
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if request_id:
            # PayPal deduplicates writes carrying the same value, which is
            # what makes retrying a POST safe.
            headers["PayPal-Request-Id"] = str(request_id)
        if extra:
            headers.update(extra)
        return headers

    # -- retry decisions ---------------------------------------------------

    def _is_safe_to_retry(self, method, request_id, idempotency=None):
        """Can this request be repeated without risking a double charge?"""
        if idempotency is Idempotency.NOT_APPLICABLE:
            return True
        if method.upper() in SAFE_METHODS:
            return True
        return bool(request_id)

    def _check_idempotency(self, method, url, request_id, idempotency=None):
        """Report a mutating request that cannot be retried safely.

        With ``STRICT_IDEMPOTENCY`` on this raises before any I/O happens; the
        warning is the *migration* path towards that, not the intended
        end state (see PROGRESS.md).

        The warning is structured — ``paypal_method``, ``paypal_endpoint`` (a
        templated, id-free path) and ``paypal_issue`` land on the ``LogRecord``
        so it can drive a metric instead of being filtered away as prose. No
        request body, credentials, headers or query string are ever logged.

        Silent when retries are disabled entirely, since then there is no
        retry for the missing key to make unsafe.
        """
        if self.config.max_retries == 0:
            return
        if request_id:
            return
        if idempotency in (Idempotency.OPTIONAL, Idempotency.NOT_APPLICABLE):
            return
        # No declared policy: fall back to the HTTP heuristic.
        if idempotency is None and self._is_safe_to_retry(method, request_id):
            return

        endpoint = endpoint_label(url)
        message = (
            f"{method} {endpoint} without a request_id: this write cannot be retried "
            "safely and will be raised on the first failure. Pass request_id with an "
            "id that is stable for this operation and persisted before the call "
            "(e.g. 'order:42:capture:1')."
        )
        if self.config.strict_idempotency:
            raise PayPalIdempotencyError(message)
        logger.warning(
            message,
            extra={
                "paypal_method": method,
                "paypal_endpoint": endpoint,
                "paypal_issue": "missing_request_id",
            },
        )

    def _should_retry_status(self, status_code, method, request_id, attempt, idempotency=None):
        if attempt >= self.config.max_retries:
            return False
        if status_code not in RETRY_STATUSES:
            return False
        return self._is_safe_to_retry(method, request_id, idempotency)

    def _should_retry_transport(self, method, request_id, attempt, idempotency=None):
        # A connection error may still have reached PayPal, so the same
        # idempotency rule applies.
        if attempt >= self.config.max_retries:
            return False
        return self._is_safe_to_retry(method, request_id, idempotency)

    def _backoff(self, attempt, retry_after=None):
        if retry_after is not None:
            return min(retry_after, MAX_BACKOFF)
        base = self.config.retry_backoff
        if base <= 0:
            return 0.0
        # Exponential with jitter, so a fleet of workers does not resynchronise.
        return min(base * (2**attempt) * random.uniform(0.5, 1.5), MAX_BACKOFF)

    # -- response handling -------------------------------------------------

    def _parse(self, response):
        """Return the decoded body, or ``{}`` for an empty (e.g. 204) one."""
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise PayPalAPIError(
                response.status_code,
                name="INVALID_RESPONSE",
                message=f"PayPal returned a body that is not JSON: {exc}",
                debug_id=response.headers.get("paypal-debug-id"),
            ) from exc


class PayPalClient(_BasePayPalClient):
    """Synchronous PayPal REST client.

    Usable as a context manager, which is the recommended form since it closes
    the connection pool::

        with PayPalClient() as client:
            order = client.post("/v2/checkout/orders", json=payload,
                                request_id="order-42")
    """

    def __init__(self, config=None, *, transport=None):
        super().__init__(config, transport=transport)
        self._http = httpx.Client(transport=transport, timeout=self.config.timeout)

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def request(
        self,
        method,
        path,
        *,
        json=None,
        params=None,
        headers=None,
        request_id=None,
        idempotency=None,
        authenticate=True,
    ):
        """Perform a request and return the decoded body.

        Raises a :class:`~paypal_checkout.exceptions.PayPalAPIError` subclass on
        an error status, or
        :class:`~paypal_checkout.exceptions.PayPalConnectionError` if no response
        was obtained.
        """
        method = method.upper()
        url = self._url(path)
        # Before any I/O: strict mode must refuse without touching the network.
        self._check_idempotency(method, url, request_id, idempotency)
        attempt = 0
        token_refreshed = False
        force_refresh = False

        while True:
            token = (
                get_access_token(
                    self.config, transport=self._transport, force_refresh=force_refresh
                )
                if authenticate
                else None
            )
            try:
                response = self._http.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=self._headers(token, request_id=request_id, extra=headers),
                )
            except httpx.HTTPError as exc:
                if self._should_retry_transport(method, request_id, attempt, idempotency):
                    time.sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise PayPalConnectionError(f"{method} {url} failed: {exc}") from exc

            # An expired or revoked token: re-authenticate once, then retry.
            if response.status_code == 401 and authenticate and not token_refreshed:
                token_refreshed = True
                force_refresh = True
                continue

            if self._should_retry_status(
                response.status_code, method, request_id, attempt, idempotency
            ):
                time.sleep(self._backoff(attempt, retry_after_seconds(response)))
                attempt += 1
                continue

            if response.is_error:
                raise error_from_response(response)
            return self._parse(response)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def patch(self, path, **kwargs):
        return self.request("PATCH", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)


class AsyncPayPalClient(_BasePayPalClient):
    """Asynchronous PayPal REST client — same surface as :class:`PayPalClient`.

    ::

        async with AsyncPayPalClient() as client:
            order = await client.post("/v2/checkout/orders", json=payload,
                                      request_id="order-42")
    """

    def __init__(self, config=None, *, transport=None):
        super().__init__(config, transport=transport)
        self._http = httpx.AsyncClient(transport=transport, timeout=self.config.timeout)

    async def aclose(self):
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        await self.aclose()

    async def request(
        self,
        method,
        path,
        *,
        json=None,
        params=None,
        headers=None,
        request_id=None,
        idempotency=None,
        authenticate=True,
    ):
        method = method.upper()
        url = self._url(path)
        # Before any I/O: strict mode must refuse without touching the network.
        self._check_idempotency(method, url, request_id, idempotency)
        attempt = 0
        token_refreshed = False
        force_refresh = False

        while True:
            token = (
                await aget_access_token(
                    self.config, transport=self._transport, force_refresh=force_refresh
                )
                if authenticate
                else None
            )
            try:
                response = await self._http.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=self._headers(token, request_id=request_id, extra=headers),
                )
            except httpx.HTTPError as exc:
                if self._should_retry_transport(method, request_id, attempt, idempotency):
                    await asyncio.sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise PayPalConnectionError(f"{method} {url} failed: {exc}") from exc

            if response.status_code == 401 and authenticate and not token_refreshed:
                token_refreshed = True
                force_refresh = True
                continue

            if self._should_retry_status(
                response.status_code, method, request_id, attempt, idempotency
            ):
                await asyncio.sleep(self._backoff(attempt, retry_after_seconds(response)))
                attempt += 1
                continue

            if response.is_error:
                raise error_from_response(response)
            return self._parse(response)

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def post(self, path, **kwargs):
        return await self.request("POST", path, **kwargs)

    async def patch(self, path, **kwargs):
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path, **kwargs):
        return await self.request("DELETE", path, **kwargs)
