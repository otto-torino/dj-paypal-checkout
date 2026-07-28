"""Amounts: ``Decimal`` in, PayPal amount strings out.

PayPal wants amounts as strings with a currency-correct number of decimals
(``"10.00"`` for EUR, ``"1000"`` for JPY). Two rules are enforced here rather
than left to call sites:

* **floats are rejected.** ``0.1 + 0.2`` is not ``0.3``; a float that reached a
  payment amount is a bug, not something to round away.
* **precision is never silently dropped.** If a value cannot be expressed in the
  currency's decimals, this raises instead of rounding — otherwise the buyer
  would be charged something other than what the caller's own records say.
  Padding is fine (``Decimal("10.1")`` → ``"10.10"``); losing digits is not.

Verified against PayPal's currency-codes reference on 2026-07-28: HUF, JPY and
TWD take no decimals, every other supported currency takes two, and PayPal
supports no three-decimal currency.
"""

from decimal import Decimal, InvalidOperation

from .exceptions import PayPalAmountError

__all__ = [
    "ZERO_DECIMAL_CURRENCIES",
    "SUPPORTED_CURRENCIES",
    "decimal_places",
    "format_amount",
    "parse_amount",
    "amount_payload",
    "parse_amount_payload",
]

#: Currencies PayPal rejects if the amount carries decimals.
ZERO_DECIMAL_CURRENCIES = frozenset({"HUF", "JPY", "TWD"})

#: PayPal's supported currencies, for reference and for callers who want to
#: validate early. Deliberately *not* enforced here: PayPal adding a currency
#: should not require a release of this library.
SUPPORTED_CURRENCIES = frozenset(
    {
        "AUD", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP", "HKD",
        "HUF", "ILS", "JPY", "MXN", "MYR", "NOK", "NZD", "PHP", "PLN", "RUB",
        "SEK", "SGD", "THB", "TWD", "USD",
    }
)

DEFAULT_DECIMAL_PLACES = 2


def _validate_currency(currency):
    if not isinstance(currency, str):
        raise PayPalAmountError(
            f"currency must be a 3-letter ISO-4217 string, got {type(currency).__name__}."
        )
    code = currency.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise PayPalAmountError(f"currency must be a 3-letter ISO-4217 code, got {currency!r}.")
    return code


def _to_decimal(value):
    if isinstance(value, bool):
        raise PayPalAmountError(f"{value!r} is not a monetary amount.")
    if isinstance(value, float):
        raise PayPalAmountError(
            f"refusing to build an amount from the float {value!r}: floats cannot represent "
            "money exactly. Pass a Decimal or a string."
        )
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except InvalidOperation:
            raise PayPalAmountError(f"{value!r} is not a valid decimal amount.") from None
    else:
        raise PayPalAmountError(
            f"amount must be a Decimal, int or str, got {type(value).__name__}."
        )
    if not amount.is_finite():
        raise PayPalAmountError(f"{value!r} is not a finite amount.")
    return amount


def decimal_places(currency):
    """Decimals PayPal accepts for ``currency`` (0 for HUF/JPY/TWD, else 2)."""
    return 0 if _validate_currency(currency) in ZERO_DECIMAL_CURRENCIES else DEFAULT_DECIMAL_PLACES


def format_amount(value, currency):
    """Render ``value`` as a PayPal amount string for ``currency``.

    Raises :class:`~paypal_checkout.exceptions.PayPalAmountError` for floats,
    negatives, non-finite values, and anything that would lose precision.
    """
    code = _validate_currency(currency)
    amount = _to_decimal(value)
    if amount < 0:
        raise PayPalAmountError(f"amount must not be negative, got {amount}.")

    places = decimal_places(code)
    exponent = Decimal(1).scaleb(-places)
    try:
        quantized = amount.quantize(exponent)
    except InvalidOperation:
        raise PayPalAmountError(f"{amount} is too large to express as a {code} amount.") from None

    if quantized != amount:
        detail = "takes no decimals" if places == 0 else f"takes {places} decimals"
        raise PayPalAmountError(
            f"{amount} cannot be expressed in {code}, which {detail}. Round it yourself "
            "and decide what happens to the remainder — this library will not do it "
            "silently for you."
        )
    return f"{quantized:f}"


def parse_amount(value):
    """Turn a PayPal amount string back into a ``Decimal``."""
    return _to_decimal(value)


def amount_payload(value, currency):
    """Build the ``{"currency_code", "value"}`` object PayPal expects."""
    code = _validate_currency(currency)
    return {"currency_code": code, "value": format_amount(value, code)}


def parse_amount_payload(payload):
    """Inverse of :func:`amount_payload`: return ``(Decimal, currency_code)``."""
    if not isinstance(payload, dict):
        raise PayPalAmountError(f"amount payload must be a dict, got {type(payload).__name__}.")
    try:
        raw_value = payload["value"]
        currency = payload["currency_code"]
    except KeyError as exc:
        raise PayPalAmountError(f"amount payload is missing {exc.args[0]!r}.") from None
    return parse_amount(raw_value), _validate_currency(currency)
