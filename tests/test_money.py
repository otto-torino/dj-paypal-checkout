from decimal import Decimal

from django.test import SimpleTestCase

from paypal_checkout.exceptions import PayPalAmountError
from paypal_checkout.money import (
    SUPPORTED_CURRENCIES,
    ZERO_DECIMAL_CURRENCIES,
    amount_payload,
    decimal_places,
    format_amount,
    parse_amount,
    parse_amount_payload,
)


class DecimalPlacesTests(SimpleTestCase):
    def test_two_decimal_currencies(self):
        for currency in ("EUR", "USD", "GBP", "CHF"):
            with self.subTest(currency=currency):
                self.assertEqual(decimal_places(currency), 2)

    def test_zero_decimal_currencies(self):
        for currency in ZERO_DECIMAL_CURRENCIES:
            with self.subTest(currency=currency):
                self.assertEqual(decimal_places(currency), 0)

    def test_zero_decimal_set_matches_paypal_docs(self):
        """Verified against PayPal's currency-codes reference, 2026-07-28."""
        self.assertEqual(ZERO_DECIMAL_CURRENCIES, {"HUF", "JPY", "TWD"})

    def test_unknown_currencies_default_to_two(self):
        """PayPal adding a currency must not need a release of this library."""
        self.assertNotIn("XYZ", SUPPORTED_CURRENCIES)
        self.assertEqual(decimal_places("XYZ"), 2)

    def test_lowercase_is_accepted(self):
        self.assertEqual(decimal_places("jpy"), 0)

    def test_invalid_currency(self):
        for bad in ("EU", "EURO", "12A", "", 978, None):
            with self.subTest(currency=bad):
                with self.assertRaises(PayPalAmountError):
                    decimal_places(bad)


class FormatAmountTests(SimpleTestCase):
    def test_two_decimal_formatting(self):
        cases = {
            Decimal("10"): "10.00",
            Decimal("10.1"): "10.10",
            Decimal("10.00"): "10.00",
            Decimal("0"): "0.00",
            Decimal("1234567.89"): "1234567.89",
            10: "10.00",
            "10.5": "10.50",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(format_amount(value, "EUR"), expected)

    def test_zero_decimal_formatting(self):
        self.assertEqual(format_amount(Decimal("1000"), "JPY"), "1000")
        self.assertEqual(format_amount(1000, "JPY"), "1000")
        self.assertEqual(format_amount(Decimal("1000.00"), "JPY"), "1000")

    def test_padding_is_allowed_but_losing_digits_is_not(self):
        self.assertEqual(format_amount(Decimal("10.1"), "EUR"), "10.10")
        with self.assertRaisesMessage(PayPalAmountError, "cannot be expressed in EUR"):
            format_amount(Decimal("10.005"), "EUR")

    def test_decimals_are_refused_for_zero_decimal_currencies(self):
        with self.assertRaisesMessage(PayPalAmountError, "which takes no decimals"):
            format_amount(Decimal("1000.50"), "JPY")

    def test_floats_are_refused(self):
        with self.assertRaisesMessage(PayPalAmountError, "floats cannot represent money exactly"):
            format_amount(10.0, "EUR")

    def test_booleans_are_refused(self):
        with self.assertRaisesMessage(PayPalAmountError, "not a monetary amount"):
            format_amount(True, "EUR")

    def test_negative_amounts_are_refused(self):
        with self.assertRaisesMessage(PayPalAmountError, "must not be negative"):
            format_amount(Decimal("-1.00"), "EUR")

    def test_non_finite_amounts_are_refused(self):
        for bad in (Decimal("NaN"), Decimal("Infinity"), "-Infinity"):
            with self.subTest(value=bad):
                with self.assertRaisesMessage(PayPalAmountError, "not a finite amount"):
                    format_amount(bad, "EUR")

    def test_garbage_strings_are_refused(self):
        with self.assertRaisesMessage(PayPalAmountError, "not a valid decimal amount"):
            format_amount("ten euros", "EUR")

    def test_unsupported_types_are_refused(self):
        with self.assertRaisesMessage(PayPalAmountError, "must be a Decimal, int or str"):
            format_amount([10], "EUR")

    def test_absurdly_large_exponent_is_refused(self):
        with self.assertRaises(PayPalAmountError):
            format_amount(Decimal("1E+1000"), "EUR")

    def test_whitespace_is_tolerated(self):
        self.assertEqual(format_amount(" 10.50 ", " eur "), "10.50")


class ParseAmountTests(SimpleTestCase):
    def test_paypal_strings_round_trip(self):
        for value, currency in (("10.00", "EUR"), ("1000", "JPY"), ("0.01", "USD")):
            with self.subTest(value=value):
                parsed = parse_amount(value)
                self.assertIsInstance(parsed, Decimal)
                self.assertEqual(format_amount(parsed, currency), value)

    def test_garbage_is_refused(self):
        with self.assertRaises(PayPalAmountError):
            parse_amount("free")

    def test_floats_are_refused_here_too(self):
        with self.assertRaises(PayPalAmountError):
            parse_amount(10.0)


class AmountPayloadTests(SimpleTestCase):
    def test_payload_shape(self):
        self.assertEqual(
            amount_payload(Decimal("10.5"), "eur"),
            {"currency_code": "EUR", "value": "10.50"},
        )

    def test_zero_decimal_payload(self):
        self.assertEqual(
            amount_payload(1000, "JPY"), {"currency_code": "JPY", "value": "1000"}
        )

    def test_round_trip(self):
        payload = amount_payload(Decimal("12.34"), "USD")
        self.assertEqual(parse_amount_payload(payload), (Decimal("12.34"), "USD"))

    def test_parse_requires_a_dict(self):
        with self.assertRaisesMessage(PayPalAmountError, "must be a dict"):
            parse_amount_payload("10.00")

    def test_parse_reports_the_missing_key(self):
        with self.assertRaisesMessage(PayPalAmountError, "missing 'currency_code'"):
            parse_amount_payload({"value": "10.00"})
        with self.assertRaisesMessage(PayPalAmountError, "missing 'value'"):
            parse_amount_payload({"currency_code": "EUR"})
