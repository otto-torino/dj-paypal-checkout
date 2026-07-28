"""dj-paypal-checkout — a modern, REST-first PayPal integration for Django.

Implemented so far: configuration, OAuth2 authentication with token caching,
and the sync/async HTTP clients. Orders, webhooks and models are next; see
PROGRESS.md for the milestone plan.
"""

__version__ = "0.0.0"

from .client import AsyncPayPalClient, PayPalClient  # noqa: E402
from .config import PayPalConfig, get_config  # noqa: E402
from .exceptions import (  # noqa: E402
    PayPalAPIError,
    PayPalAuthenticationError,
    PayPalConfigurationError,
    PayPalConnectionError,
    PayPalError,
    PayPalNotFoundError,
    PayPalRateLimitError,
    PayPalServerError,
    PayPalValidationError,
)

__all__ = [
    "__version__",
    "AsyncPayPalClient",
    "PayPalClient",
    "PayPalConfig",
    "get_config",
    "PayPalError",
    "PayPalConfigurationError",
    "PayPalConnectionError",
    "PayPalAPIError",
    "PayPalAuthenticationError",
    "PayPalValidationError",
    "PayPalNotFoundError",
    "PayPalRateLimitError",
    "PayPalServerError",
]
