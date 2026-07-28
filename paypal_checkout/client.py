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
import random
import time

import httpx

from .auth import aget_access_token, get_access_token
from .config import get_config
from .exceptions import (
    PayPalAPIError,
    PayPalConnectionError,
    error_from_response,
    retry_after_seconds,
)

__all__ = ["PayPalClient", "AsyncPayPalClient"]

#: Methods that can be repeated without changing the outcome.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

#: Statuses worth retrying: rate limiting and PayPal-side failures.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Upper bound for a single backoff sleep, in seconds.
MAX_BACKOFF = 30.0


def _user_agent():
    # Imported lazily to avoid a circular import at package init time.
    from . import __version__

    return f"dj-paypal-checkout/{__version__}"


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

    def _is_safe_to_retry(self, method, request_id):
        """Can this request be repeated without risking a double charge?"""
        if method.upper() in SAFE_METHODS:
            return True
        return bool(request_id)

    def _should_retry_status(self, status_code, method, request_id, attempt):
        if attempt >= self.config.max_retries:
            return False
        if status_code not in RETRY_STATUSES:
            return False
        return self._is_safe_to_retry(method, request_id)

    def _should_retry_transport(self, method, request_id, attempt):
        # A connection error may still have reached PayPal, so the same
        # idempotency rule applies.
        if attempt >= self.config.max_retries:
            return False
        return self._is_safe_to_retry(method, request_id)

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
                if self._should_retry_transport(method, request_id, attempt):
                    time.sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise PayPalConnectionError(f"{method} {url} failed: {exc}") from exc

            # An expired or revoked token: re-authenticate once, then retry.
            if response.status_code == 401 and authenticate and not token_refreshed:
                token_refreshed = True
                force_refresh = True
                continue

            if self._should_retry_status(response.status_code, method, request_id, attempt):
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
        authenticate=True,
    ):
        method = method.upper()
        url = self._url(path)
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
                if self._should_retry_transport(method, request_id, attempt):
                    await asyncio.sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise PayPalConnectionError(f"{method} {url} failed: {exc}") from exc

            if response.status_code == 401 and authenticate and not token_refreshed:
                token_refreshed = True
                force_refresh = True
                continue

            if self._should_retry_status(response.status_code, method, request_id, attempt):
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
