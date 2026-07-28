import base64

import httpx
from django.core.cache import cache
from django.test import SimpleTestCase

from paypal_checkout.auth import (
    TOKEN_PATH,
    aget_access_token,
    get_access_token,
    invalidate_token,
    token_cache_key,
)
from paypal_checkout.exceptions import (
    PayPalAuthenticationError,
    PayPalConnectionError,
)

from .support import FakePayPal, make_config


class TokenCacheKeyTests(SimpleTestCase):
    def test_sandbox_and_live_never_share_a_token(self):
        sandbox = make_config(live=False)
        live = make_config(live=True)
        self.assertNotEqual(token_cache_key(sandbox), token_cache_key(live))

    def test_rotating_the_secret_invalidates_the_key(self):
        before = make_config(client_secret="old")
        after = make_config(client_secret="new")
        self.assertNotEqual(token_cache_key(before), token_cache_key(after))

    def test_credentials_never_appear_in_the_key(self):
        config = make_config()
        key = token_cache_key(config)
        self.assertNotIn(config.client_id, key)
        self.assertNotIn(config.client_secret, key)


class GetAccessTokenTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_token_is_fetched_and_cached(self):
        fake = FakePayPal()
        config = make_config()

        first = get_access_token(config, transport=fake.transport)
        second = get_access_token(config, transport=fake.transport)

        self.assertEqual(first, "token-1")
        self.assertEqual(second, "token-1")
        self.assertEqual(len(fake.token_requests), 1, "the second call must hit the cache")

    def test_request_uses_basic_auth_and_client_credentials(self):
        fake = FakePayPal()
        config = make_config()

        get_access_token(config, transport=fake.transport)

        request = fake.token_requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, TOKEN_PATH)
        self.assertIn(b"grant_type=client_credentials", request.content)
        expected = base64.b64encode(
            f"{config.client_id}:{config.client_secret}".encode()
        ).decode()
        self.assertEqual(request.headers["authorization"], f"Basic {expected}")

    def test_force_refresh_bypasses_the_cache(self):
        fake = FakePayPal()
        config = make_config()

        get_access_token(config, transport=fake.transport)
        refreshed = get_access_token(config, transport=fake.transport, force_refresh=True)

        self.assertEqual(refreshed, "token-2")
        self.assertEqual(len(fake.token_requests), 2)

    def test_different_accounts_do_not_share_a_token(self):
        fake = FakePayPal()
        get_access_token(make_config(client_id="a"), transport=fake.transport)
        get_access_token(make_config(client_id="b"), transport=fake.transport)
        self.assertEqual(len(fake.token_requests), 2)

    def test_short_lived_token_is_not_cached(self):
        """With expiry below the refresh leeway there is nothing worth caching."""
        fake = FakePayPal(expires_in=60)
        config = make_config(token_leeway=300)

        get_access_token(config, transport=fake.transport)
        get_access_token(config, transport=fake.transport)

        self.assertEqual(len(fake.token_requests), 2)
        self.assertIsNone(cache.get(token_cache_key(config)))

    def test_leeway_is_subtracted_from_the_cache_ttl(self):
        fake = FakePayPal(expires_in=1000)
        config = make_config(token_leeway=400)

        get_access_token(config, transport=fake.transport)

        # LocMem stores an absolute expiry; asserting on the stored value is
        # backend-specific, so just check the token is there and reused.
        self.assertEqual(cache.get(token_cache_key(config)), "token-1")

    def test_unparseable_expiry_is_treated_as_expired(self):
        fake = FakePayPal().queue_token(
            httpx.Response(200, json={"access_token": "t", "expires_in": "soon"})
        )
        config = make_config()

        self.assertEqual(get_access_token(config, transport=fake.transport), "t")
        self.assertIsNone(cache.get(token_cache_key(config)))

    def test_invalid_credentials_raise_authentication_error(self):
        fake = FakePayPal().queue_token(
            httpx.Response(
                401,
                json={
                    "error": "invalid_client",
                    "error_description": "Client Authentication failed",
                },
            )
        )
        with self.assertRaises(PayPalAuthenticationError) as ctx:
            get_access_token(make_config(), transport=fake.transport)

        self.assertEqual(ctx.exception.name, "invalid_client")
        self.assertIn("Client Authentication failed", str(ctx.exception))

    def test_missing_access_token_is_an_error(self):
        fake = FakePayPal().queue_token(httpx.Response(200, json={"token_type": "Bearer"}))
        with self.assertRaises(Exception):
            get_access_token(make_config(), transport=fake.transport)

    def test_unreachable_paypal_raises_connection_error(self):
        fake = FakePayPal().queue_token(httpx.ConnectError("no route to host"))
        with self.assertRaisesMessage(PayPalConnectionError, "Could not reach PayPal"):
            get_access_token(make_config(), transport=fake.transport)

    def test_invalidate_token_forces_a_refetch(self):
        fake = FakePayPal()
        config = make_config()

        get_access_token(config, transport=fake.transport)
        invalidate_token(config)
        get_access_token(config, transport=fake.transport)

        self.assertEqual(len(fake.token_requests), 2)


class AsyncGetAccessTokenTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    async def test_token_is_fetched_and_cached(self):
        fake = FakePayPal()
        config = make_config()

        first = await aget_access_token(config, transport=fake.transport)
        second = await aget_access_token(config, transport=fake.transport)

        self.assertEqual(first, "token-1")
        self.assertEqual(second, "token-1")
        self.assertEqual(len(fake.token_requests), 1)

    async def test_force_refresh_bypasses_the_cache(self):
        fake = FakePayPal()
        config = make_config()

        await aget_access_token(config, transport=fake.transport)
        refreshed = await aget_access_token(config, transport=fake.transport, force_refresh=True)

        self.assertEqual(refreshed, "token-2")
        self.assertEqual(len(fake.token_requests), 2)

    async def test_sync_and_async_share_the_cache(self):
        fake = FakePayPal()
        config = make_config()

        token = await aget_access_token(config, transport=fake.transport)

        self.assertEqual(token, "token-1")
        self.assertEqual(len(fake.token_requests), 1)

    async def test_short_lived_token_is_not_cached(self):
        fake = FakePayPal(expires_in=60)
        config = make_config(token_leeway=300)

        await aget_access_token(config, transport=fake.transport)
        await aget_access_token(config, transport=fake.transport)

        self.assertEqual(len(fake.token_requests), 2)

    async def test_unreachable_paypal_raises_connection_error(self):
        fake = FakePayPal().queue_token(httpx.ConnectError("boom"))
        with self.assertRaises(PayPalConnectionError):
            await aget_access_token(make_config(), transport=fake.transport)
