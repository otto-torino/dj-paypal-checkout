import httpx
from django.core.cache import cache
from django.test import SimpleTestCase

from paypal_checkout import __version__
from paypal_checkout.client import AsyncPayPalClient, PayPalClient
from paypal_checkout.exceptions import (
    PayPalAPIError,
    PayPalAuthenticationError,
    PayPalConnectionError,
    PayPalRateLimitError,
    PayPalServerError,
    PayPalValidationError,
)

from .support import FakePayPal, make_config

ORDERS = "/v2/checkout/orders"


class RequestShapeTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_successful_get_returns_parsed_body(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(200, json={"id": "5O1"}))
        with PayPalClient(make_config(), transport=fake.transport) as client:
            self.assertEqual(client.get(ORDERS), {"id": "5O1"})

    def test_bearer_token_and_user_agent_are_sent(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(200, json={}))
        with PayPalClient(make_config(), transport=fake.transport) as client:
            client.get(ORDERS)

        request = fake.api_requests()[0]
        self.assertEqual(request.headers["authorization"], "Bearer token-1")
        self.assertEqual(request.headers["user-agent"], f"dj-paypal-checkout/{__version__}")
        self.assertEqual(request.headers["accept"], "application/json")

    def test_request_id_becomes_the_idempotency_header(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(201, json={"id": "5O1"}))
        with PayPalClient(make_config(), transport=fake.transport) as client:
            client.post(ORDERS, json={"intent": "CAPTURE"}, request_id="order-42")

        self.assertEqual(fake.api_requests()[0].headers["paypal-request-id"], "order-42")

    def test_no_request_id_header_when_not_supplied(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(201, json={}))
        with PayPalClient(make_config(), transport=fake.transport) as client:
            client.post(ORDERS, json={})

        self.assertNotIn("paypal-request-id", fake.api_requests()[0].headers)

    def test_sandbox_base_url_is_used(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(200, json={}))
        config = make_config(live=False)
        with PayPalClient(config, transport=fake.transport) as client:
            client.get(ORDERS)

        self.assertEqual(str(fake.api_requests()[0].url), f"{config.base_url}{ORDERS}")

    def test_absolute_urls_are_passed_through(self):
        """PayPal responses contain absolute HATEOAS links."""
        fake = FakePayPal().queue("/v2/checkout/orders/5O1", httpx.Response(200, json={"id": "5O1"}))
        config = make_config()
        with PayPalClient(config, transport=fake.transport) as client:
            client.get(f"{config.base_url}/v2/checkout/orders/5O1")

        self.assertEqual(len(fake.api_requests()), 1)

    def test_empty_response_becomes_an_empty_dict(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(204))
        with PayPalClient(make_config(), transport=fake.transport) as client:
            self.assertEqual(client.delete(ORDERS), {})

    def test_non_json_body_is_reported_as_such(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(200, text="<html>oops</html>"))
        with PayPalClient(make_config(), transport=fake.transport) as client:
            with self.assertRaises(PayPalAPIError) as ctx:
                client.get(ORDERS)

        self.assertEqual(ctx.exception.name, "INVALID_RESPONSE")

    def test_unauthenticated_requests_send_no_token(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(200, json={}))
        with PayPalClient(make_config(), transport=fake.transport) as client:
            client.get(ORDERS, authenticate=False)

        self.assertEqual(fake.token_requests, [])
        self.assertNotIn("authorization", fake.api_requests()[0].headers)

    def test_params_and_extra_headers_are_forwarded(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(200, json={}))
        with PayPalClient(make_config(), transport=fake.transport) as client:
            client.get(ORDERS, params={"page": 2}, headers={"Prefer": "return=representation"})

        request = fake.api_requests()[0]
        self.assertEqual(request.url.params["page"], "2")
        self.assertEqual(request.headers["prefer"], "return=representation")


    def test_patch_and_delete_are_available(self):
        fake = FakePayPal().queue(
            ORDERS, httpx.Response(204), httpx.Response(204)
        )
        with PayPalClient(make_config(), transport=fake.transport) as client:
            self.assertEqual(client.patch(ORDERS, json=[{"op": "replace"}]), {})
            self.assertEqual(client.delete(ORDERS), {})

        self.assertEqual([r.method for r in fake.api_requests()], ["PATCH", "DELETE"])


class BackoffTests(SimpleTestCase):
    """Backoff is pure arithmetic, so it is tested directly rather than slept through."""

    def test_retry_after_wins_and_is_capped(self):
        client = PayPalClient(make_config(retry_backoff=0.5), transport=FakePayPal().transport)
        self.addCleanup(client.close)
        self.assertEqual(client._backoff(0, retry_after=3), 3)
        self.assertEqual(client._backoff(0, retry_after=10_000), 30.0)

    def test_zero_backoff_disables_sleeping(self):
        client = PayPalClient(make_config(retry_backoff=0), transport=FakePayPal().transport)
        self.addCleanup(client.close)
        self.assertEqual(client._backoff(3), 0.0)

    def test_backoff_grows_with_jitter_and_is_capped(self):
        client = PayPalClient(make_config(retry_backoff=1.0), transport=FakePayPal().transport)
        self.addCleanup(client.close)
        # attempt 0 -> 1s +/- 50% jitter, attempt 1 -> 2s +/- 50%
        self.assertTrue(0.5 <= client._backoff(0) <= 1.5)
        self.assertTrue(1.0 <= client._backoff(1) <= 3.0)
        self.assertLessEqual(client._backoff(20), 30.0)


class RetrySafetyRulesTests(SimpleTestCase):
    """The idempotency rule, checked directly on every method."""

    def setUp(self):
        self.client = PayPalClient(make_config(), transport=FakePayPal().transport)
        self.addCleanup(self.client.close)

    def test_safe_methods_are_always_retryable(self):
        for method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertTrue(self.client._is_safe_to_retry(method, None))

    def test_writes_need_an_idempotency_key(self):
        for method in ("POST", "PATCH"):
            with self.subTest(method=method):
                self.assertFalse(self.client._is_safe_to_retry(method, None))
                self.assertTrue(self.client._is_safe_to_retry(method, "req-1"))


class ErrorMappingTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_validation_error_carries_debug_id_and_issues(self):
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(
                422,
                json={
                    "name": "UNPROCESSABLE_ENTITY",
                    "message": "Requested action could not be performed.",
                    "debug_id": "d3adb33f",
                    "details": [{"issue": "CURRENCY_NOT_SUPPORTED"}],
                },
            ),
        )
        with PayPalClient(make_config(), transport=fake.transport) as client:
            with self.assertRaises(PayPalValidationError) as ctx:
                client.post(ORDERS, json={})

        error = ctx.exception
        self.assertEqual(error.status_code, 422)
        self.assertEqual(error.debug_id, "d3adb33f")
        self.assertIn("CURRENCY_NOT_SUPPORTED", str(error))
        self.assertIn("d3adb33f", str(error))

    def test_debug_id_falls_back_to_the_header(self):
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"}, headers={"Paypal-Debug-Id": "hdr-1"}),
        )
        with PayPalClient(make_config(), transport=fake.transport) as client:
            with self.assertRaises(PayPalAPIError) as ctx:
                client.get(ORDERS)

        self.assertEqual(ctx.exception.debug_id, "hdr-1")

    def test_rate_limit_error_exposes_retry_after(self):
        fake = FakePayPal().queue(
            ORDERS, httpx.Response(429, json={"name": "RATE_LIMIT_REACHED"}, headers={"Retry-After": "12"})
        )
        config = make_config(max_retries=0)
        with PayPalClient(config, transport=fake.transport) as client:
            with self.assertRaises(PayPalRateLimitError) as ctx:
                client.get(ORDERS)

        self.assertEqual(ctx.exception.retry_after, 12.0)


class RetrySafetyTests(SimpleTestCase):
    """The rules that keep a retry from turning into a double charge."""

    def setUp(self):
        cache.clear()

    def test_get_is_retried_on_server_error(self):
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}),
            httpx.Response(200, json={"id": "5O1"}),
        )
        with PayPalClient(make_config(), transport=fake.transport) as client:
            self.assertEqual(client.get(ORDERS), {"id": "5O1"})

        self.assertEqual(len(fake.api_requests()), 2)

    def test_retries_are_bounded_by_max_retries(self):
        fake = FakePayPal().queue(
            ORDERS, *[httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"})] * 3
        )
        with PayPalClient(make_config(max_retries=2), transport=fake.transport) as client:
            with self.assertRaises(PayPalServerError):
                client.get(ORDERS)

        self.assertEqual(len(fake.api_requests()), 3, "1 attempt + 2 retries")

    def test_post_without_request_id_is_never_retried(self):
        """Repeating a write with no idempotency key could charge twice."""
        fake = FakePayPal().queue(ORDERS, httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"}))
        with PayPalClient(make_config(), transport=fake.transport) as client:
            with self.assertRaises(PayPalServerError):
                client.post(ORDERS, json={})

        self.assertEqual(len(fake.api_requests()), 1)

    def test_post_with_request_id_is_retried(self):
        """PayPal deduplicates on PayPal-Request-Id, so this is safe."""
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"}),
            httpx.Response(201, json={"id": "5O1"}),
        )
        with PayPalClient(make_config(), transport=fake.transport) as client:
            self.assertEqual(client.post(ORDERS, json={}, request_id="order-42"), {"id": "5O1"})

        self.assertEqual(len(fake.api_requests()), 2)

    def test_client_errors_are_not_retried(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"}))
        with PayPalClient(make_config(), transport=fake.transport) as client:
            with self.assertRaises(PayPalValidationError):
                client.get(ORDERS)

        self.assertEqual(len(fake.api_requests()), 1)

    def test_rate_limit_is_retried_on_a_safe_method(self):
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(429, json={"name": "RATE_LIMIT_REACHED"}, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"ok": True}),
        )
        with PayPalClient(make_config(), transport=fake.transport) as client:
            self.assertEqual(client.get(ORDERS), {"ok": True})

    def test_transport_error_is_retried_on_a_safe_method(self):
        fake = FakePayPal().queue(
            ORDERS, httpx.ConnectError("reset"), httpx.Response(200, json={"ok": True})
        )
        with PayPalClient(make_config(), transport=fake.transport) as client:
            self.assertEqual(client.get(ORDERS), {"ok": True})

    def test_transport_error_on_post_without_request_id_is_raised(self):
        """The request may have reached PayPal, so the same rule applies."""
        fake = FakePayPal().queue(ORDERS, httpx.ConnectError("reset"))
        with PayPalClient(make_config(), transport=fake.transport) as client:
            with self.assertRaises(PayPalConnectionError):
                client.post(ORDERS, json={})

        self.assertEqual(len(fake.api_requests()), 1)

    def test_exhausted_transport_retries_raise_connection_error(self):
        fake = FakePayPal().queue(ORDERS, *[httpx.ConnectError("reset")] * 3)
        with PayPalClient(make_config(max_retries=2), transport=fake.transport) as client:
            with self.assertRaises(PayPalConnectionError):
                client.get(ORDERS)

        self.assertEqual(len(fake.api_requests()), 3)


class TokenRefreshTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_401_triggers_one_refresh_and_a_retry(self):
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
            httpx.Response(200, json={"id": "5O1"}),
        )
        with PayPalClient(make_config(), transport=fake.transport) as client:
            self.assertEqual(client.get(ORDERS), {"id": "5O1"})

        self.assertEqual(len(fake.token_requests), 2, "the token must be re-fetched once")
        self.assertEqual(fake.api_requests()[1].headers["authorization"], "Bearer token-2")

    def test_a_second_401_is_raised_instead_of_looping(self):
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
        )
        with PayPalClient(make_config(), transport=fake.transport) as client:
            with self.assertRaises(PayPalAuthenticationError):
                client.get(ORDERS)

        self.assertEqual(len(fake.api_requests()), 2)
        self.assertEqual(len(fake.token_requests), 2)

    def test_refresh_is_skipped_for_unauthenticated_requests(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(401, json={"name": "NOT_AUTHORIZED"}))
        with PayPalClient(make_config(), transport=fake.transport) as client:
            with self.assertRaises(PayPalAuthenticationError):
                client.get(ORDERS, authenticate=False)

        self.assertEqual(fake.token_requests, [])

    def test_post_without_request_id_still_retries_after_a_401(self):
        """A 401 means the request was rejected before any money moved."""
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
            httpx.Response(201, json={"id": "5O1"}),
        )
        with PayPalClient(make_config(), transport=fake.transport) as client:
            self.assertEqual(client.post(ORDERS, json={}), {"id": "5O1"})

    def test_replay_reuses_the_same_request_id_and_body(self):
        """The replay must be the *same* request, or it could charge twice."""
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
            httpx.Response(201, json={"id": "5O1"}),
        )
        with PayPalClient(make_config(), transport=fake.transport) as client:
            client.post(ORDERS, json={"intent": "CAPTURE"}, request_id="order:42:capture:1")

        requests = fake.api_requests()
        self.assertEqual(
            [r.headers["paypal-request-id"] for r in requests],
            ["order:42:capture:1", "order:42:capture:1"],
        )
        self.assertEqual(requests[0].content, requests[1].content)

    def test_write_that_401s_twice_refreshes_only_once(self):
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
        )
        with PayPalClient(make_config(), transport=fake.transport) as client:
            with self.assertRaises(PayPalAuthenticationError):
                client.post(ORDERS, json={}, request_id="order:42:capture:1")

        self.assertEqual(len(fake.api_requests()), 2, "one attempt + one replay, no more")
        self.assertEqual(len(fake.token_requests), 2, "exactly one refresh")

    def test_replay_failing_with_5xx_is_not_retried_further(self):
        """The 401 exemption must not hand an unsafe write a free retry."""
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
            httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"}),
        )
        with PayPalClient(make_config(max_retries=2), transport=fake.transport) as client:
            with self.assertRaises(PayPalServerError):
                client.post(ORDERS, json={})

        self.assertEqual(len(fake.api_requests()), 2)

    def test_replay_failing_transport_is_not_retried_further(self):
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
            httpx.ConnectError("reset"),
        )
        with PayPalClient(make_config(max_retries=2), transport=fake.transport) as client:
            with self.assertRaises(PayPalConnectionError):
                client.post(ORDERS, json={})

        self.assertEqual(len(fake.api_requests()), 2)

    def test_refresh_budget_is_separate_from_the_retry_budget(self):
        """A 401 replay must not consume one of the max_retries attempts."""
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
            httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}),
            httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}),
            httpx.Response(200, json={"ok": True}),
        )
        with PayPalClient(make_config(max_retries=2), transport=fake.transport) as client:
            self.assertEqual(client.get(ORDERS), {"ok": True})

        self.assertEqual(len(fake.api_requests()), 4, "1 attempt + 1 replay + 2 retries")


class AsyncClientTests(SimpleTestCase):
    """The async client must behave identically to the sync one."""

    def setUp(self):
        cache.clear()

    async def test_successful_post_returns_parsed_body(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(201, json={"id": "5O1"}))
        async with AsyncPayPalClient(make_config(), transport=fake.transport) as client:
            result = await client.post(ORDERS, json={}, request_id="order-42")

        self.assertEqual(result, {"id": "5O1"})
        self.assertEqual(fake.api_requests()[0].headers["paypal-request-id"], "order-42")

    async def test_401_triggers_one_refresh_and_a_retry(self):
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
            httpx.Response(200, json={"id": "5O1"}),
        )
        async with AsyncPayPalClient(make_config(), transport=fake.transport) as client:
            self.assertEqual(await client.get(ORDERS), {"id": "5O1"})

        self.assertEqual(len(fake.token_requests), 2)

    async def test_post_without_request_id_is_never_retried(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"}))
        async with AsyncPayPalClient(make_config(), transport=fake.transport) as client:
            with self.assertRaises(PayPalServerError):
                await client.post(ORDERS, json={})

        self.assertEqual(len(fake.api_requests()), 1)

    async def test_get_is_retried_on_server_error(self):
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}),
            httpx.Response(200, json={"ok": True}),
        )
        async with AsyncPayPalClient(make_config(), transport=fake.transport) as client:
            self.assertEqual(await client.get(ORDERS), {"ok": True})

        self.assertEqual(len(fake.api_requests()), 2)

    async def test_transport_error_raises_connection_error(self):
        fake = FakePayPal().queue(ORDERS, *[httpx.ConnectError("reset")] * 3)
        async with AsyncPayPalClient(make_config(max_retries=2), transport=fake.transport) as client:
            with self.assertRaises(PayPalConnectionError):
                await client.get(ORDERS)

    async def test_patch_and_delete_are_available(self):
        fake = FakePayPal().queue(ORDERS, httpx.Response(204), httpx.Response(204))
        async with AsyncPayPalClient(make_config(), transport=fake.transport) as client:
            self.assertEqual(await client.patch(ORDERS, json=[]), {})
            self.assertEqual(await client.delete(ORDERS), {})

        self.assertEqual([r.method for r in fake.api_requests()], ["PATCH", "DELETE"])

    async def test_replay_reuses_the_same_request_id(self):
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
            httpx.Response(201, json={"id": "5O1"}),
        )
        async with AsyncPayPalClient(make_config(), transport=fake.transport) as client:
            await client.post(ORDERS, json={}, request_id="order:42:capture:1")

        self.assertEqual(
            [r.headers["paypal-request-id"] for r in fake.api_requests()],
            ["order:42:capture:1", "order:42:capture:1"],
        )

    async def test_replay_failing_with_5xx_is_not_retried_further(self):
        fake = FakePayPal().queue(
            ORDERS,
            httpx.Response(401, json={"name": "NOT_AUTHORIZED"}),
            httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"}),
        )
        async with AsyncPayPalClient(make_config(max_retries=2), transport=fake.transport) as client:
            with self.assertRaises(PayPalServerError):
                await client.post(ORDERS, json={})

        self.assertEqual(len(fake.api_requests()), 2)

    async def test_validation_error_is_raised(self):
        fake = FakePayPal().queue(
            ORDERS, httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "d1"})
        )
        async with AsyncPayPalClient(make_config(), transport=fake.transport) as client:
            with self.assertRaises(PayPalValidationError) as ctx:
                await client.post(ORDERS, json={})

        self.assertEqual(ctx.exception.debug_id, "d1")
