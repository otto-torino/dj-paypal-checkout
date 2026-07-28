"""OAuth2 client-credentials authentication.

PayPal access tokens are long-lived (hours), so re-authenticating on every call
would be pure waste. Tokens are cached in the Django cache under a key derived
from the credentials *and* the environment, so:

* sandbox and live tokens can never be mixed up;
* rotating the secret invalidates the cached token immediately;
* the raw client id never appears in a cache key.

Two workers refreshing at the same time is harmless — PayPal accepts several
valid tokens for the same client, and the last write simply wins.
"""

import hashlib

import httpx
from django.core.cache import caches

from .exceptions import PayPalConnectionError, error_from_response

__all__ = ["get_access_token", "aget_access_token", "token_cache_key", "invalidate_token"]

TOKEN_PATH = "/v1/oauth2/token"
CACHE_KEY_PREFIX = "paypal_checkout:token:"

#: Never cache a token for less than this, otherwise the cache round-trip
#: costs more than it saves.
MIN_CACHE_SECONDS = 30


def token_cache_key(config):
    """Cache key for this account's access token."""
    material = f"{config.client_id}:{config.client_secret}:{config.environment}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{CACHE_KEY_PREFIX}{digest}"


def _request_kwargs(config):
    return {
        "url": f"{config.base_url}{TOKEN_PATH}",
        "data": {"grant_type": "client_credentials"},
        "headers": {
            "Accept": "application/json",
            "Accept-Language": "en_US",
        },
        "auth": (config.client_id, config.client_secret),
    }


def _token_from_response(response):
    if response.is_error:
        raise error_from_response(response)
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise error_from_response(response)
    expires_in = payload.get("expires_in")
    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        expires_in = 0
    return token, expires_in


def _cache_seconds(config, expires_in):
    """How long to keep the token, leaving room to refresh before expiry."""
    ttl = expires_in - config.token_leeway
    return ttl if ttl >= MIN_CACHE_SECONDS else 0


def invalidate_token(config):
    """Drop the cached token — used after a 401."""
    caches[config.cache_alias].delete(token_cache_key(config))


def get_access_token(config, *, transport=None, force_refresh=False):
    """Return a bearer token, from the cache unless ``force_refresh``.

    ``transport`` injects an ``httpx.BaseTransport`` and exists so tests can
    serve canned responses; production code never passes it.
    """
    cache = caches[config.cache_alias]
    key = token_cache_key(config)
    if not force_refresh:
        cached = cache.get(key)
        if cached:
            return cached

    try:
        with httpx.Client(transport=transport, timeout=config.timeout) as client:
            response = client.post(**_request_kwargs(config))
    except httpx.HTTPError as exc:
        raise PayPalConnectionError(f"Could not reach PayPal to authenticate: {exc}") from exc

    token, expires_in = _token_from_response(response)
    seconds = _cache_seconds(config, expires_in)
    if seconds:
        cache.set(key, token, seconds)
    return token


async def aget_access_token(config, *, transport=None, force_refresh=False):
    """Async counterpart of :func:`get_access_token`."""
    cache = caches[config.cache_alias]
    key = token_cache_key(config)
    if not force_refresh:
        cached = await cache.aget(key)
        if cached:
            return cached

    try:
        async with httpx.AsyncClient(transport=transport, timeout=config.timeout) as client:
            response = await client.post(**_request_kwargs(config))
    except httpx.HTTPError as exc:
        raise PayPalConnectionError(f"Could not reach PayPal to authenticate: {exc}") from exc

    token, expires_in = _token_from_response(response)
    seconds = _cache_seconds(config, expires_in)
    if seconds:
        await cache.aset(key, token, seconds)
    return token
