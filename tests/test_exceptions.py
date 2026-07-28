import httpx
from django.test import SimpleTestCase

from paypal_checkout.exceptions import (
    PayPalAPIError,
    PayPalAuthenticationError,
    PayPalNotFoundError,
    PayPalRateLimitError,
    PayPalServerError,
    PayPalValidationError,
    error_class_for_status,
    error_from_response,
    retry_after_seconds,
)


class ErrorClassForStatusTests(SimpleTestCase):
    def test_known_statuses(self):
        cases = {
            400: PayPalValidationError,
            401: PayPalAuthenticationError,
            403: PayPalAuthenticationError,
            404: PayPalNotFoundError,
            422: PayPalValidationError,
            429: PayPalRateLimitError,
            500: PayPalServerError,
            503: PayPalServerError,
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertIs(error_class_for_status(status), expected)

    def test_unmapped_status_falls_back_to_the_base_class(self):
        self.assertIs(error_class_for_status(418), PayPalAPIError)


class RetryAfterTests(SimpleTestCase):
    def test_seconds_are_parsed(self):
        self.assertEqual(retry_after_seconds(httpx.Response(429, headers={"Retry-After": "7"})), 7.0)

    def test_missing_header(self):
        self.assertIsNone(retry_after_seconds(httpx.Response(429)))

    def test_http_date_is_ignored(self):
        """A date is legal HTTP but not worth parsing — fall back to backoff."""
        response = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        self.assertIsNone(retry_after_seconds(response))

    def test_negative_values_are_clamped(self):
        self.assertEqual(retry_after_seconds(httpx.Response(429, headers={"Retry-After": "-5"})), 0.0)


class ErrorFromResponseTests(SimpleTestCase):
    def test_rest_error_shape(self):
        response = httpx.Response(
            422,
            json={
                "name": "UNPROCESSABLE_ENTITY",
                "message": "Requested action could not be performed.",
                "debug_id": "abc123",
                "details": [{"issue": "INSTRUMENT_DECLINED", "description": "declined"}],
            },
        )
        error = error_from_response(response)
        self.assertIsInstance(error, PayPalValidationError)
        self.assertEqual(error.name, "UNPROCESSABLE_ENTITY")
        self.assertEqual(error.debug_id, "abc123")
        self.assertIn("INSTRUMENT_DECLINED", str(error))

    def test_oauth_error_shape(self):
        response = httpx.Response(
            401, json={"error": "invalid_client", "error_description": "bad secret"}
        )
        error = error_from_response(response)
        self.assertIsInstance(error, PayPalAuthenticationError)
        self.assertEqual(error.name, "invalid_client")
        self.assertEqual(error.message, "bad secret")

    def test_non_json_body(self):
        error = error_from_response(httpx.Response(502, text="<html>bad gateway</html>"))
        self.assertIsInstance(error, PayPalServerError)
        self.assertEqual(error.payload, {})
        self.assertEqual(error.status_code, 502)

    def test_json_body_that_is_not_an_object(self):
        error = error_from_response(httpx.Response(500, json=["boom"]))
        self.assertEqual(error.payload, {"raw": ["boom"]})

    def test_details_that_are_not_a_list_are_ignored(self):
        error = error_from_response(httpx.Response(400, json={"details": "nope"}))
        self.assertEqual(error.details, [])

    def test_message_falls_back_to_the_reason_phrase(self):
        error = error_from_response(httpx.Response(404, json={}))
        self.assertTrue(error.message)

    def test_rate_limit_error_gets_retry_after(self):
        response = httpx.Response(429, json={"name": "RATE_LIMIT_REACHED"}, headers={"Retry-After": "3"})
        error = error_from_response(response)
        self.assertIsInstance(error, PayPalRateLimitError)
        self.assertEqual(error.retry_after, 3.0)

    def test_correlation_id_header_is_used_as_debug_id(self):
        response = httpx.Response(500, json={}, headers={"Correlation-Id": "corr-9"})
        self.assertEqual(error_from_response(response).debug_id, "corr-9")


class ExceptionStrTests(SimpleTestCase):
    def test_minimal_error(self):
        self.assertEqual(str(PayPalAPIError(500)), "500")

    def test_full_error(self):
        error = PayPalAPIError(
            422,
            name="UNPROCESSABLE_ENTITY",
            message="nope",
            debug_id="d1",
            details=[{"issue": "A"}, {"issue": "B"}],
        )
        self.assertEqual(str(error), "422 UNPROCESSABLE_ENTITY: nope [A, B] (debug_id=d1)")

    def test_details_without_issues(self):
        error = PayPalAPIError(400, name="X", message="m", details=[{"description": "no issue key"}])
        self.assertEqual(str(error), "400 X: m")
