"""dj-paypal-checkout — a modern, REST-first PayPal integration for Django.

Orders v2 checkout (create, authorize, capture), refunds and voids, verified
webhooks, models that survive an interrupted call, signals and a read-only
admin. Subscriptions v1 includes products, plans, lifecycle webhooks and
recurring-payment records. Payment Method Tokens v3 provides Vault support.
Card Fields remain an application-side, merchant-enabled integration.

Note that ``models``, ``orders``, ``payments``, ``vault`` and
``webhooks.views`` are *not* re-exported here: they touch the ORM, so importing
them at package-import time would run before the app registry is ready. Import
them from their modules.
"""

__version__ = "0.3.0"

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
    payment_token_created,
    payment_token_deleted,
    subscription_activated,
    subscription_cancelled,
    subscription_expired,
    subscription_payment_completed,
    subscription_payment_failed,
    subscription_suspended,
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
    "payment_token_created",
    "payment_token_deleted",
    "subscription_activated",
    "subscription_suspended",
    "subscription_cancelled",
    "subscription_expired",
    "subscription_payment_completed",
    "subscription_payment_failed",
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
