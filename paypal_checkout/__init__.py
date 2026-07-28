"""dj-paypal-checkout — a modern, REST-first PayPal integration for Django.

Implemented so far: configuration, OAuth2 authentication with token caching,
and the sync/async HTTP clients. Orders, webhooks and models are next; see
PROGRESS.md for the milestone plan.
"""

__version__ = "0.0.0"

from .client import AsyncPayPalClient, Idempotency, PayPalClient  # noqa: E402
from .config import PayPalConfig, get_config  # noqa: E402
from .exceptions import (  # noqa: E402
    PayPalAmountError,
    PayPalAPIError,
    PayPalAuthenticationError,
    PayPalConfigurationError,
    PayPalConnectionError,
    PayPalError,
    PayPalIdempotencyError,
    PayPalNotFoundError,
    PayPalRateLimitError,
    PayPalServerError,
    PayPalValidationError,
)
from .money import amount_payload, format_amount, parse_amount  # noqa: E402
from .signals import (  # noqa: E402
    payment_captured,
    payment_denied,
    payment_refunded,
)

__all__ = [
    "__version__",
    "AsyncPayPalClient",
    "PayPalClient",
    "Idempotency",
    "PayPalConfig",
    "get_config",
    "format_amount",
    "parse_amount",
    "amount_payload",
    "payment_captured",
    "payment_denied",
    "payment_refunded",
    "PayPalError",
    "PayPalConfigurationError",
    "PayPalIdempotencyError",
    "PayPalAmountError",
    "PayPalConnectionError",
    "PayPalAPIError",
    "PayPalAuthenticationError",
    "PayPalValidationError",
    "PayPalNotFoundError",
    "PayPalRateLimitError",
    "PayPalServerError",
]
