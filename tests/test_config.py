from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from paypal_checkout.config import LIVE_BASE_URL, SANDBOX_BASE_URL, get_config
from paypal_checkout.exceptions import PayPalConfigurationError

MINIMAL = {"CLIENT_ID": "id", "CLIENT_SECRET": "secret"}


class GetConfigTests(SimpleTestCase):
    @override_settings(PAYPAL=None)
    def test_missing_settings_dict(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "settings.PAYPAL is missing"):
            get_config()

    @override_settings(PAYPAL="nope")
    def test_settings_must_be_a_dict(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "must be a dict"):
            get_config()

    @override_settings(PAYPAL={"CLIENT_SECRET": "secret"})
    def test_client_id_required(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "PAYPAL['CLIENT_ID'] is required"):
            get_config()

    @override_settings(PAYPAL={"CLIENT_ID": "id", "CLIENT_SECRET": "   "})
    def test_blank_credentials_rejected(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "PAYPAL['CLIENT_SECRET'] is required"):
            get_config()

    @override_settings(PAYPAL={**MINIMAL, "CLIENT_TOKEN": "typo"})
    def test_unknown_key_is_named(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "Unknown key(s) in settings.PAYPAL: CLIENT_TOKEN"):
            get_config()

    @override_settings(PAYPAL=MINIMAL)
    def test_defaults(self):
        config = get_config()
        self.assertFalse(config.live)
        self.assertEqual(config.base_url, SANDBOX_BASE_URL)
        self.assertEqual(config.environment, "sandbox")
        self.assertEqual(config.currency, "EUR")
        self.assertEqual(config.timeout, 30.0)
        self.assertEqual(config.max_retries, 2)
        self.assertEqual(config.cache_alias, "default")
        self.assertEqual(config.webhook_id, "")

    @override_settings(PAYPAL={**MINIMAL, "LIVE": True})
    def test_live_switches_base_url(self):
        config = get_config()
        self.assertEqual(config.base_url, LIVE_BASE_URL)
        self.assertEqual(config.environment, "live")

    @override_settings(PAYPAL={**MINIMAL, "CURRENCY": "usd"})
    def test_currency_is_normalised(self):
        self.assertEqual(get_config().currency, "USD")

    @override_settings(PAYPAL={**MINIMAL, "CURRENCY": "EU"})
    def test_currency_must_be_iso4217(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "3-letter ISO-4217 code"):
            get_config()

    @override_settings(PAYPAL={**MINIMAL, "TIMEOUT": "slow"})
    def test_timeout_must_be_numeric(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "PAYPAL['TIMEOUT'] must be a number"):
            get_config()

    @override_settings(PAYPAL={**MINIMAL, "MAX_RETRIES": -1})
    def test_max_retries_must_not_be_negative(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "must not be negative"):
            get_config()

    @override_settings(PAYPAL={**MINIMAL, "TIMEOUT": 5})
    def test_numeric_settings_are_coerced(self):
        self.assertIsInstance(get_config().timeout, float)

    @override_settings(PAYPAL={**MINIMAL, "CLIENT_ID": 12345})
    def test_string_settings_must_be_strings(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "must be a string, got int"):
            get_config()

    @override_settings(PAYPAL={**MINIMAL, "TIMEOUT": -1})
    def test_timeout_must_not_be_negative(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "PAYPAL['TIMEOUT'] must not be negative"):
            get_config()

    @override_settings(PAYPAL={**MINIMAL, "MAX_RETRIES": "many"})
    def test_integer_settings_must_be_integers(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "PAYPAL['MAX_RETRIES'] must be an integer"):
            get_config()

    @override_settings(
        PAYPAL={
            **MINIMAL,
            "MAX_RETRIES": 5,
            "TOKEN_LEEWAY": 60,
            "RETRY_BACKOFF": 0.1,
            "CACHE_ALIAS": "default",
            "STRICT_IDEMPOTENCY": 1,
        }
    )
    def test_all_tuning_keys_are_accepted(self):
        config = get_config()
        self.assertEqual(config.max_retries, 5)
        self.assertEqual(config.token_leeway, 60)
        self.assertEqual(config.retry_backoff, 0.1)
        self.assertEqual(config.cache_alias, "default")
        self.assertIs(config.strict_idempotency, True)

    @override_settings(PAYPAL=MINIMAL)
    def test_strict_idempotency_is_off_by_default(self):
        self.assertIs(get_config().strict_idempotency, False)

    @override_settings(PAYPAL=MINIMAL)
    def test_overrides_win_over_settings(self):
        config = get_config(client_id="other", live=True)
        self.assertEqual(config.client_id, "other")
        self.assertTrue(config.live)

    @override_settings(PAYPAL=MINIMAL)
    def test_unknown_override_is_rejected(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "Unknown config override"):
            get_config(base_url="https://example.com")

    @override_settings(PAYPAL=MINIMAL)
    def test_configuration_error_is_improperly_configured(self):
        """So a misconfiguration surfaces like any other Django one."""
        with self.assertRaises(ImproperlyConfigured):
            get_config(currency="nope")


class PayPalConfigTests(SimpleTestCase):
    @override_settings(PAYPAL=MINIMAL)
    def test_require_webhook_id_explains_itself(self):
        with self.assertRaisesMessage(PayPalConfigurationError, "WEBHOOK_ID'] is required"):
            get_config().require_webhook_id()

    @override_settings(PAYPAL={**MINIMAL, "WEBHOOK_ID": "WH-1"})
    def test_require_webhook_id_returns_it(self):
        self.assertEqual(get_config().require_webhook_id(), "WH-1")

    @override_settings(PAYPAL=MINIMAL)
    def test_replace_returns_a_new_config(self):
        config = get_config()
        other = config.replace(live=True)
        self.assertFalse(config.live)
        self.assertTrue(other.live)

    @override_settings(PAYPAL=MINIMAL)
    def test_config_is_immutable(self):
        with self.assertRaises(Exception):
            get_config().live = True
