"""Payment Method Tokens v3 (Vault).

The safe browser flow is:

1. create a temporary :class:`~paypal_checkout.models.SetupToken`;
2. let PayPal's Card Fields or hosted approval UI populate/approve it;
3. exchange it for a permanent :class:`~paypal_checkout.models.PaymentToken`.

This module deliberately refuses raw card numbers and security codes. Those
belong in PayPal-hosted fields, never in a Django request, log or database.
"""

from .client import Idempotency
from .exceptions import PayPalError
from .models import PaymentToken, SetupToken

__all__ = [
    "SETUP_TOKENS_PATH",
    "PAYMENT_TOKENS_PATH",
    "create_setup_token",
    "fetch_setup_token",
    "refresh_setup_token",
    "create_payment_token",
    "fetch_payment_token",
    "refresh_payment_token",
    "list_payment_tokens",
    "delete_payment_token",
]

SETUP_TOKENS_PATH = "/v3/vault/setup-tokens"
PAYMENT_TOKENS_PATH = "/v3/vault/payment-tokens"

_CARD_SECRET_KEYS = frozenset(
    {"number", "security_code", "card_number", "cvv", "cvv2"}
)


def _contains_card_secret(value):
    if isinstance(value, dict):
        return any(
            str(key).lower() in _CARD_SECRET_KEYS or _contains_card_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_card_secret(item) for item in value)
    return False


def _require_safe_body(body):
    if _contains_card_secret(body):
        raise PayPalError(
            "raw card numbers and security codes are not accepted; collect them "
            "with PayPal-hosted Card Fields."
        )


def _require_payment_source(payment_source):
    if not isinstance(payment_source, dict) or len(payment_source) != 1:
        raise PayPalError("payment_source must contain exactly one payment method.")
    source = next(iter(payment_source.values()))
    if not isinstance(source, dict):
        raise PayPalError("the payment_source value must be an object.")


def _require_customer(customer):
    if customer is not None and not isinstance(customer, dict):
        raise PayPalError("customer must be an object.")


def _require_paypal_id(instance, kind):
    if not instance.paypal_id:
        raise PayPalError(
            f"{instance!r} has no PayPal id: this {kind} was started locally but "
            "PayPal never confirmed it. Reconcile it before continuing."
        )
    return instance.paypal_id


def _require_same_environment(client, instance, kind):
    if instance.live != client.config.live:
        client_environment = "live" if client.config.live else "sandbox"
        instance_environment = "live" if instance.live else "sandbox"
        raise PayPalError(
            f"{kind} {instance!r} belongs to {instance_environment}, but the "
            f"client is configured for {client_environment}."
        )


def create_setup_token(
    client, *, payment_source, customer=None, target=None, **extra
):
    """Create and persist a temporary setup token.

    For Card Fields, pass ``payment_source={"card": {}}``; PayPal's browser
    component fills the token without exposing card data to Django.
    """
    _require_payment_source(payment_source)
    _require_customer(customer)
    body = {"payment_source": payment_source}
    if customer is not None:
        body["customer"] = customer
    body.update(extra)
    _require_safe_body(body)

    merchant_customer_id = (
        customer.get("merchant_customer_id", "") if customer is not None else ""
    )
    setup_token = SetupToken.objects.start(
        live=client.config.live,
        target=target,
        merchant_customer_id=merchant_customer_id,
    )
    payload = client.post(
        SETUP_TOKENS_PATH,
        json=body,
        request_id=setup_token.request_id,
        idempotency=Idempotency.REQUIRED,
    )
    return setup_token.update_from_payload(payload)


def fetch_setup_token(client, paypal_id):
    """Read a setup token directly from PayPal."""
    return client.get(f"{SETUP_TOKENS_PATH}/{paypal_id}")


def refresh_setup_token(client, setup_token):
    """Refresh a local setup token after browser approval/Card Fields."""
    _require_same_environment(client, setup_token, "setup token")
    paypal_id = _require_paypal_id(setup_token, "setup token")
    return setup_token.update_from_payload(
        client.get(f"{SETUP_TOKENS_PATH}/{paypal_id}")
    )


def create_payment_token(
    client,
    *,
    setup_token=None,
    setup_token_id=None,
    customer=None,
    target=None,
    **extra,
):
    """Exchange an approved setup token for a permanent payment token."""
    if setup_token is not None and setup_token_id is not None:
        raise PayPalError("pass setup_token or setup_token_id, not both.")
    _require_customer(customer)

    if setup_token is not None:
        _require_same_environment(client, setup_token, "setup token")
        paypal_setup_id = _require_paypal_id(setup_token, "setup token")
        existing = PaymentToken.objects.filter(setup_token=setup_token).first()
        if existing is not None and not existing.is_unconfirmed:
            raise PayPalError(
                f"setup token {paypal_setup_id} already has payment token "
                f"{existing.paypal_id or existing.pk}."
            )
    else:
        paypal_setup_id = setup_token_id
    if not paypal_setup_id:
        raise PayPalError(
            "a payment token needs a setup token: pass setup_token= or "
            "setup_token_id=."
        )

    body = {
        "payment_source": {
            "token": {"id": paypal_setup_id, "type": "SETUP_TOKEN"}
        }
    }
    if customer is not None:
        body["customer"] = customer
    body.update(extra)
    _require_safe_body(body)

    payment_token = PaymentToken.objects.start(
        live=client.config.live,
        setup_token=setup_token,
        target=target,
        customer_id=customer.get("id", "") if customer is not None else "",
        merchant_customer_id=(
            customer.get("merchant_customer_id", "")
            if customer is not None
            else ""
        ),
    )
    payload = client.post(
        PAYMENT_TOKENS_PATH,
        json=body,
        request_id=payment_token.request_id,
        idempotency=Idempotency.REQUIRED,
    )
    payment_token.update_from_payload(payload)
    if setup_token is not None and setup_token.status != SetupToken.Status.VAULTED:
        setup_token.status = SetupToken.Status.VAULTED
        setup_token.save(update_fields=["status", "updated_at"])
    return payment_token


def fetch_payment_token(client, paypal_id):
    """Read a permanent payment token directly from PayPal."""
    return client.get(f"{PAYMENT_TOKENS_PATH}/{paypal_id}")


def refresh_payment_token(client, payment_token):
    _require_same_environment(client, payment_token, "payment token")
    paypal_id = _require_paypal_id(payment_token, "payment token")
    return payment_token.update_from_payload(
        client.get(f"{PAYMENT_TOKENS_PATH}/{paypal_id}")
    )


def list_payment_tokens(
    client, customer_id, *, page_size=5, page=1, total_required=False
):
    """List PayPal's saved methods for one PayPal-generated customer id."""
    if not isinstance(customer_id, str) or not customer_id:
        raise PayPalError("customer_id must be a non-empty string.")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 5:
        raise PayPalError("page_size must be an integer from 1 to 5.")
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 10:
        raise PayPalError("page must be an integer from 1 to 10.")
    if not isinstance(total_required, bool):
        raise PayPalError("total_required must be a boolean.")
    return client.get(
        PAYMENT_TOKENS_PATH,
        params={
            "customer_id": customer_id,
            "page_size": page_size,
            "page": page,
            "total_required": str(total_required).lower(),
        },
    )


def delete_payment_token(client, payment_token):
    """Delete a saved method and keep a local tombstone for audit."""
    _require_same_environment(client, payment_token, "payment token")
    paypal_id = _require_paypal_id(payment_token, "payment token")
    client.delete(
        f"{PAYMENT_TOKENS_PATH}/{paypal_id}",
        idempotency=Idempotency.OPTIONAL,
    )
    return payment_token.mark_deleted()
