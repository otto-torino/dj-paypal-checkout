"""Orders v2: create, show, capture.

This is the layer that owns the economic invariant, so the client below it can
stay sharp. Every call here:

* writes a row **before** talking to PayPal, so an interrupted operation is
  discoverable afterwards;
* passes that row's **persisted** idempotency key, so a retry — including one
  after a crash or a re-run job — is deduplicated by PayPal instead of charging
  twice;
* declares :class:`~paypal_checkout.client.Idempotency.REQUIRED`, so a future
  strict-mode default cannot be satisfied by accident.

The amount is always computed here from the caller's own figures and never read
back from the browser.

Synchronous only for now: the ORM helpers these functions rely on run in a
transaction, and async wrappers need more than swapping in ``await``. The async
client remains available for direct calls.
"""

import logging

from .client import Idempotency
from .exceptions import PayPalAmountError, PayPalError
from .models import Authorization, Capture, PayPalOrder
from .money import amount_payload, parse_amount_payload
from .signals import payment_captured, payment_denied

__all__ = [
    "ORDERS_PATH",
    "AUTHORIZATIONS_PATH",
    "create_order",
    "refresh_order",
    "fetch_order",
    "capture_order",
    "authorize_order",
    "capture_authorization",
]

logger = logging.getLogger(__name__)

ORDERS_PATH = "/v2/checkout/orders"
AUTHORIZATIONS_PATH = "/v2/payments/authorizations"


def _validate_purchase_units(purchase_units, amount, currency):
    """Refuse purchase units whose total does not match the recorded amount.

    Cheap protection against the worst kind of mismatch — the local row saying
    €10 while PayPal is asked for €100. Skipped when the units are shaped in a
    way this cannot read (a currency other than ``currency``, or a missing
    amount): a partial check must not become a false rejection.
    """
    total = 0
    for unit in purchase_units:
        if not isinstance(unit, dict) or "amount" not in unit:
            return
        try:
            value, unit_currency = parse_amount_payload(unit["amount"])
        except PayPalAmountError:
            return
        if unit_currency != currency:
            return
        total += value
    if total != amount:
        raise PayPalAmountError(
            f"purchase_units total {total} {currency} does not match the recorded "
            f"amount {amount} {currency}. The local row and PayPal must agree on "
            "what the buyer is being charged."
        )


def create_order(
    client,
    *,
    amount,
    currency=None,
    intent=None,
    target=None,
    purchase_units=None,
    application_context=None,
    payment_source=None,
):
    """Create an order at PayPal and return its :class:`PayPalOrder` row.

    ``amount`` is the total recorded locally. Pass ``purchase_units`` only for
    orders this helper cannot express; the total must still match ``amount``.

    On failure the row is left in ``INITIATED``: the outcome is unknown, so it
    stays visible for reconciliation rather than being deleted.
    """
    currency = (currency or client.config.currency).upper()
    intent = intent or PayPalOrder.Intent.CAPTURE

    if purchase_units is not None:
        _validate_purchase_units(purchase_units, amount, currency)
        units = purchase_units
    else:
        units = [{"amount": amount_payload(amount, currency)}]

    body = {"intent": intent, "purchase_units": units}
    if application_context:
        body["application_context"] = application_context
    if payment_source:
        body["payment_source"] = payment_source

    order = PayPalOrder.objects.start(
        amount=amount,
        currency=currency,
        live=client.config.live,
        intent=intent,
        target=target,
    )
    payload = client.post(
        ORDERS_PATH,
        json=body,
        request_id=order.request_id,
        idempotency=Idempotency.REQUIRED,
    )
    return order.update_from_payload(payload)


def refresh_order(client, order):
    """Re-read an order from PayPal and update its row."""
    payload = client.get(f"{ORDERS_PATH}/{_require_paypal_id(order)}")
    return order.update_from_payload(payload)


def fetch_order(client, paypal_id):
    """Read an order straight from PayPal, with no local row involved."""
    return client.get(f"{ORDERS_PATH}/{paypal_id}")


def _require_paypal_id(order):
    if not order.paypal_id:
        raise PayPalError(
            f"{order!r} has no PayPal id: it was started locally but PayPal never "
            "confirmed it. Reconcile it before continuing."
        )
    return order.paypal_id


def _extract_capture(payload):
    """Pull the capture out of the order representation PayPal returns."""
    for unit in payload.get("purchase_units") or []:
        if not isinstance(unit, dict):
            continue
        captures = (unit.get("payments") or {}).get("captures") or []
        if captures and isinstance(captures[0], dict):
            return captures[0]
    return None


def capture_order(client, order, *, amount=None, final_capture=True):
    """Capture an approved order and return the :class:`Capture` row.

    An unconfirmed previous attempt is reused, key included, because it may
    have reached PayPal — that is what makes a retry after a crash safe. A new
    attempt after a decline gets its own key.

    Pass ``amount`` for a partial capture.
    """
    paypal_id = _require_paypal_id(order)
    capture = order.start_capture(
        amount=amount, currency=order.currency, final_capture=final_capture
    )

    body = None
    if amount is not None:
        body = {
            "amount": amount_payload(amount, capture.currency),
            "final_capture": final_capture,
        }

    payload = client.post(
        f"{ORDERS_PATH}/{paypal_id}/capture",
        json=body,
        request_id=capture.request_id,
        idempotency=Idempotency.REQUIRED,
    )

    # The response is the order, with the capture nested inside it.
    order.update_from_payload(payload)

    capture_payload = _extract_capture(payload)
    if capture_payload is None:
        # Money may well have moved. Keep the attempt in INITIATED so
        # reconciliation finds it, rather than guessing that it succeeded.
        capture.raw = payload
        capture.save(update_fields=["raw", "updated_at"])
        logger.warning(
            "capture response contained no capture object; attempt %s left "
            "unconfirmed for reconciliation",
            capture.request_id,
            extra={
                "paypal_endpoint": f"{ORDERS_PATH}/{{id}}/capture",
                "paypal_issue": "capture_not_in_response",
            },
        )
        return capture

    capture.update_from_payload(capture_payload)
    _notify(capture, order)
    return capture


def _extract_authorization(payload):
    """Pull the authorization out of the order representation PayPal returns."""
    for unit in payload.get("purchase_units") or []:
        if not isinstance(unit, dict):
            continue
        authorizations = (unit.get("payments") or {}).get("authorizations") or []
        if authorizations and isinstance(authorizations[0], dict):
            return authorizations[0]
    return None


def authorize_order(client, order):
    """Authorize an approved order — hold the money without taking it.

    Only meaningful for ``intent=AUTHORIZE``. Returns the
    :class:`~paypal_checkout.models.Authorization` row; capture it later with
    :func:`capture_authorization`.
    """
    paypal_id = _require_paypal_id(order)
    authorization = order.start_authorization()

    payload = client.post(
        f"{ORDERS_PATH}/{paypal_id}/authorize",
        request_id=authorization.request_id,
        idempotency=Idempotency.REQUIRED,
    )
    order.update_from_payload(payload)

    authorization_payload = _extract_authorization(payload)
    if authorization_payload is None:
        # Same reasoning as an unreadable capture response: record the payload,
        # leave the attempt unconfirmed, let reconciliation decide.
        authorization.raw = payload
        authorization.save(update_fields=["raw", "updated_at"])
        logger.warning(
            "authorize response contained no authorization object; attempt %s "
            "left unconfirmed for reconciliation",
            authorization.request_id,
            extra={
                "paypal_endpoint": f"{ORDERS_PATH}/{{id}}/authorize",
                "paypal_issue": "authorization_not_in_response",
            },
        )
        return authorization

    return authorization.update_from_payload(authorization_payload)


def capture_authorization(client, authorization, *, amount=None, final_capture=True):
    """Capture money held by an authorization.

    Unlike the order-capture endpoint, this one answers with the capture itself
    rather than with the enclosing order.
    """
    if not authorization.paypal_id:
        raise PayPalError(
            f"{authorization!r} has no PayPal id: it was started locally but PayPal "
            "never confirmed it. Reconcile it before continuing."
        )
    capture = authorization.start_capture(amount=amount, final_capture=final_capture)

    body = None
    if amount is not None:
        body = {
            "amount": amount_payload(amount, capture.currency),
            "final_capture": final_capture,
        }

    payload = client.post(
        f"{AUTHORIZATIONS_PATH}/{authorization.paypal_id}/capture",
        json=body,
        request_id=capture.request_id,
        idempotency=Idempotency.REQUIRED,
    )
    capture.update_from_payload(payload)
    _notify(capture, authorization.order)
    return capture


def _notify(capture, order):
    """Fire the signal matching the capture outcome, if any."""
    if capture.status == Capture.Status.COMPLETED:
        signal = payment_captured
    elif capture.status in (Capture.Status.DECLINED, Capture.Status.FAILED):
        signal = payment_denied
    else:
        # PENDING and the refund statuses are not this call's business.
        return
    signal.send(sender=Capture, capture=capture, order=order, target=order.target)
