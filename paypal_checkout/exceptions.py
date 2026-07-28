"""Exception hierarchy.

Every failure that comes back from PayPal carries a ``debug_id`` — that is the
one value PayPal support asks for, so it is surfaced on the exception and in
``str()`` rather than being buried in the raw payload.
"""

from django.core.exceptions import ImproperlyConfigured

__all__ = [
    "PayPalError",
    "PayPalConfigurationError",
    "PayPalIdempotencyError",
    "PayPalAmountError",
    "PayPalWebhookError",
    "PayPalWebhookNotReady",
    "PayPalConnectionError",
    "PayPalAPIError",
    "PayPalAuthenticationError",
    "PayPalValidationError",
    "PayPalNotFoundError",
    "PayPalRateLimitError",
    "PayPalServerError",
]


class PayPalError(Exception):
    """Base class for everything this library raises."""


class PayPalConfigurationError(PayPalError, ImproperlyConfigured):
    """The ``PAYPAL`` settings dict is missing or invalid.

    Also an ``ImproperlyConfigured`` so it surfaces like any other Django
    misconfiguration.
    """


class PayPalIdempotencyError(PayPalError):
    """A mutating request was attempted without an idempotency key.

    Only raised when ``PAYPAL['STRICT_IDEMPOTENCY']`` is on — a caller-side
    programming error, raised *before* anything is sent to PayPal.
    """


class PayPalAmountError(PayPalError, ValueError):
    """An amount cannot be expressed as a PayPal amount for its currency.

    A ``ValueError`` too, since it always signals a bad value at the call site
    (a float amount, a negative, precision that would have to be dropped).
    """


class PayPalWebhookError(PayPalError):
    """A webhook could not be accepted: missing headers, or an unverifiable
    signature."""


class PayPalWebhookNotReady(PayPalError):
    """The event is ours, but the local row it refers to is not there yet.

    Happens when a webhook overtakes the API response that created the row.
    Raising this makes the endpoint answer 5xx so PayPal retries later, which is
    free reconciliation — the alternative would be silently dropping a payment
    confirmation.
    """


class PayPalConnectionError(PayPalError):
    """The request never produced an HTTP response (timeout, DNS, TLS, reset)."""


class PayPalAPIError(PayPalError):
    """PayPal answered with an error status."""

    def __init__(
        self,
        status_code,
        *,
        name="",
        message="",
        debug_id=None,
        details=None,
        payload=None,
    ):
        self.status_code = status_code
        self.name = name
        self.message = message
        self.debug_id = debug_id
        self.details = details or []
        self.payload = payload if payload is not None else {}
        super().__init__(str(self))

    def __str__(self):
        parts = [f"{self.status_code}"]
        if self.name:
            parts.append(self.name)
        text = " ".join(parts)
        if self.message:
            text = f"{text}: {self.message}"
        if self.details:
            issues = ", ".join(
                filter(None, (d.get("issue") for d in self.details if isinstance(d, dict)))
            )
            if issues:
                text = f"{text} [{issues}]"
        if self.debug_id:
            text = f"{text} (debug_id={self.debug_id})"
        return text


class PayPalAuthenticationError(PayPalAPIError):
    """401/403 — bad or insufficient credentials."""


class PayPalValidationError(PayPalAPIError):
    """400/422 — PayPal rejected the request body."""


class PayPalNotFoundError(PayPalAPIError):
    """404 — the resource does not exist (or belongs to another account)."""


class PayPalRateLimitError(PayPalAPIError):
    """429 — too many requests.

    ``retry_after`` holds the ``Retry-After`` header in seconds when PayPal
    sent one.
    """

    def __init__(self, *args, retry_after=None, **kwargs):
        self.retry_after = retry_after
        super().__init__(*args, **kwargs)


class PayPalServerError(PayPalAPIError):
    """5xx — a PayPal-side failure; safe to retry if the call is idempotent."""


_STATUS_MAP = {
    400: PayPalValidationError,
    401: PayPalAuthenticationError,
    403: PayPalAuthenticationError,
    404: PayPalNotFoundError,
    422: PayPalValidationError,
    429: PayPalRateLimitError,
}


def error_class_for_status(status_code):
    """Map an HTTP status onto the most specific exception class."""
    if status_code in _STATUS_MAP:
        return _STATUS_MAP[status_code]
    if status_code >= 500:
        return PayPalServerError
    return PayPalAPIError


def retry_after_seconds(response):
    """Parse the ``Retry-After`` header, if present and expressed in seconds."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        # PayPal sends seconds; an HTTP-date is legal but not worth parsing here.
        return None


def error_from_response(response):
    """Build the right exception from an error response.

    Handles both PayPal error shapes: the REST one
    (``{"name", "message", "debug_id", "details"}``) and the OAuth one
    (``{"error", "error_description"}``). The response only needs to quack like
    an ``httpx.Response``, so this stays independent of the HTTP layer.
    """
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - any non-JSON body lands here
        payload = {}
    if not isinstance(payload, dict):
        payload = {"raw": payload}

    name = payload.get("name") or payload.get("error") or ""
    message = payload.get("message") or payload.get("error_description") or ""
    if not message:
        message = getattr(response, "reason_phrase", "") or ""
    details = payload.get("details")
    if not isinstance(details, list):
        details = []

    debug_id = payload.get("debug_id")
    if not debug_id:
        headers = response.headers
        debug_id = headers.get("paypal-debug-id") or headers.get("correlation-id")

    error_class = error_class_for_status(response.status_code)
    kwargs = {
        "name": name,
        "message": message,
        "debug_id": debug_id,
        "details": details,
        "payload": payload,
    }
    if error_class is PayPalRateLimitError:
        kwargs["retry_after"] = retry_after_seconds(response)
    return error_class(response.status_code, **kwargs)
