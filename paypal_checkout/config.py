"""Configuration — the only module that reads ``django.conf.settings``.

Everything else in the library takes an explicit :class:`PayPalConfig`, which
keeps the rest of the code testable without ``override_settings`` and makes it
impossible to accidentally read a setting from a code path that should not.
"""

from dataclasses import dataclass, fields, replace

from django.conf import settings

from .exceptions import PayPalConfigurationError

__all__ = [
    "PayPalConfig",
    "get_config",
    "SANDBOX_BASE_URL",
    "LIVE_BASE_URL",
]

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
LIVE_BASE_URL = "https://api-m.paypal.com"

SETTINGS_KEY = "PAYPAL"

#: Keys accepted in ``settings.PAYPAL``, mapped to dataclass field names.
#: Settings are UPPER_CASE by Django convention; the dataclass is lower_case.
_ALIASES = {
    "CLIENT_ID": "client_id",
    "CLIENT_SECRET": "client_secret",
    "LIVE": "live",
    "WEBHOOK_ID": "webhook_id",
    "CURRENCY": "currency",
    "TIMEOUT": "timeout",
    "MAX_RETRIES": "max_retries",
    "RETRY_BACKOFF": "retry_backoff",
    "CACHE_ALIAS": "cache_alias",
    "TOKEN_LEEWAY": "token_leeway",
}


@dataclass(frozen=True)
class PayPalConfig:
    """Validated, immutable configuration for one PayPal account."""

    client_id: str
    client_secret: str

    #: ``False`` targets the sandbox, ``True`` the live API. Switching
    #: environments must never be a code path — only this flag.
    live: bool = False

    #: Id of the webhook registered for your endpoint; required to verify
    #: incoming webhook signatures (M3).
    webhook_id: str = ""

    #: Default currency for created orders.
    currency: str = "EUR"

    #: Per-request timeout in seconds.
    timeout: float = 30.0

    #: Extra attempts after the first one. Only ever applied to requests that
    #: are safe to repeat — see ``client._is_safe_to_retry``.
    max_retries: int = 2

    #: Base for the exponential backoff between retries, in seconds.
    #: Set to 0 in tests to keep them fast.
    retry_backoff: float = 0.5

    #: Django cache alias used to store OAuth access tokens.
    cache_alias: str = "default"

    #: Refresh the access token this many seconds before it actually expires.
    token_leeway: int = 300

    @property
    def base_url(self):
        return LIVE_BASE_URL if self.live else SANDBOX_BASE_URL

    @property
    def environment(self):
        return "live" if self.live else "sandbox"

    def require_webhook_id(self):
        """Return the webhook id, or explain why verification cannot happen."""
        if not self.webhook_id:
            raise PayPalConfigurationError(
                f"{SETTINGS_KEY}['WEBHOOK_ID'] is required to verify webhook "
                "signatures. Register a webhook in the PayPal dashboard and "
                "copy its id into settings."
            )
        return self.webhook_id

    def replace(self, **changes):
        """Return a copy with ``changes`` applied (the config is frozen)."""
        return replace(self, **changes)


def _coerce(name, value):
    """Coerce and validate a single field, raising a helpful error if invalid."""
    setting = f"{SETTINGS_KEY}['{name.upper()}']"
    if name in {"client_id", "client_secret", "webhook_id", "currency", "cache_alias"}:
        if not isinstance(value, str):
            raise PayPalConfigurationError(f"{setting} must be a string, got {type(value).__name__}.")
        return value.strip()
    if name == "live":
        return bool(value)
    if name in {"timeout", "retry_backoff"}:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise PayPalConfigurationError(f"{setting} must be a number, got {value!r}.") from None
        if number < 0:
            raise PayPalConfigurationError(f"{setting} must not be negative, got {number!r}.")
        return number
    if name in {"max_retries", "token_leeway"}:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise PayPalConfigurationError(f"{setting} must be an integer, got {value!r}.") from None
        if number < 0:
            raise PayPalConfigurationError(f"{setting} must not be negative, got {number!r}.")
        return number
    # Fallback for a field added to PayPalConfig without a coercion rule.
    return value  # pragma: no cover


def get_config(**overrides):
    """Build a :class:`PayPalConfig` from ``settings.PAYPAL``.

    Keyword ``overrides`` use the dataclass (lower-case) names and win over
    settings — handy for projects juggling more than one PayPal account.
    """
    raw = getattr(settings, SETTINGS_KEY, None)
    if raw is None:
        raise PayPalConfigurationError(
            f"settings.{SETTINGS_KEY} is missing. Add a {SETTINGS_KEY} dict with at "
            "least CLIENT_ID and CLIENT_SECRET."
        )
    if not isinstance(raw, dict):
        raise PayPalConfigurationError(
            f"settings.{SETTINGS_KEY} must be a dict, got {type(raw).__name__}."
        )

    known = {f.name for f in fields(PayPalConfig)}
    unknown = sorted(set(raw) - set(_ALIASES))
    if unknown:
        raise PayPalConfigurationError(
            f"Unknown key(s) in settings.{SETTINGS_KEY}: {', '.join(unknown)}. "
            f"Valid keys: {', '.join(sorted(_ALIASES))}."
        )

    values = {}
    for key, value in raw.items():
        name = _ALIASES[key]
        values[name] = _coerce(name, value)

    for name, value in overrides.items():
        if name not in known:
            raise PayPalConfigurationError(f"Unknown config override: {name!r}.")
        values[name] = _coerce(name, value)

    for required in ("client_id", "client_secret"):
        if not values.get(required):
            raise PayPalConfigurationError(
                f"{SETTINGS_KEY}['{required.upper()}'] is required and must not be empty. "
                "Read it from the environment — never commit credentials."
            )

    config = PayPalConfig(**values)

    if len(config.currency) != 3 or not config.currency.isalpha():
        raise PayPalConfigurationError(
            f"{SETTINGS_KEY}['CURRENCY'] must be a 3-letter ISO-4217 code, "
            f"got {config.currency!r}."
        )
    return config.replace(currency=config.currency.upper())
