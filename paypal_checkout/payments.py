"""Payments v2: refunds and voids.

Same rules as :mod:`paypal_checkout.orders` — the row and its persisted key exist
before the call, the amount is built here rather than accepted from a caller's
string, and every write declares its idempotency policy.

One guard specific to refunds: a refund is refused locally when it would take the
capture past what was captured, counting refunds whose outcome we do not know
yet. PayPal would refuse it too, but only after the fact, and by then the local
row would claim money was returned that never was.
"""

import logging

from .client import Idempotency
from .exceptions import PayPalAmountError, PayPalError
from .models import Authorization, Capture, void_request_id
from .money import amount_payload
from .signals import payment_refunded

__all__ = ["CAPTURES_PATH", "AUTHORIZATIONS_PATH", "refund_capture", "void_authorization"]

logger = logging.getLogger(__name__)

CAPTURES_PATH = "/v2/payments/captures"
AUTHORIZATIONS_PATH = "/v2/payments/authorizations"


def _require_paypal_id(instance, kind):
    if not instance.paypal_id:
        raise PayPalError(
            f"{instance!r} has no PayPal id: this {kind} was started locally but "
            "PayPal never confirmed it. Reconcile it before continuing."
        )
    return instance.paypal_id


def refund_capture(
    client, capture, *, amount=None, note_to_payer=None, invoice_id=None
):
    """Refund a capture, fully or partially, and return the ``Refund`` row.

    ``amount`` defaults to the whole capture. An unconfirmed previous attempt is
    reused, key included, because it may have reached PayPal.

    Raises :class:`~paypal_checkout.exceptions.PayPalAmountError` if the refund
    would exceed what is still refundable.
    """
    paypal_id = _require_paypal_id(capture, "capture")

    requested = capture.amount if amount is None else amount
    pending = capture.pending_refund()
    if pending is None:
        # Check before creating the row, so a refused refund leaves no trace.
        available = capture.refundable_amount
        if requested > available:
            raise PayPalAmountError(
                f"cannot refund {requested} {capture.currency} of capture "
                f"{paypal_id}: only {available} {capture.currency} is still "
                f"refundable (captured {capture.amount}, already refunded or in "
                f"flight {capture.reserved_refund_amount})."
            )

    refund = capture.start_refund(
        amount=amount, note_to_payer=note_to_payer, invoice_id=invoice_id
    )

    body = {}
    if amount is not None:
        body["amount"] = amount_payload(refund.amount, refund.currency)
    if refund.note_to_payer:
        body["note_to_payer"] = refund.note_to_payer
    if refund.invoice_id:
        body["invoice_id"] = refund.invoice_id

    payload = client.post(
        f"{CAPTURES_PATH}/{paypal_id}/refund",
        json=body or None,
        request_id=refund.request_id,
        idempotency=Idempotency.REQUIRED,
    )
    refund.update_from_payload(payload)

    if refund.is_successful:
        capture.sync_refund_status()
        payment_refunded.send(
            sender=Capture,
            capture=capture,
            order=capture.order,
            target=capture.order.target,
            refund=refund,
        )
    return refund


def void_authorization(client, authorization):
    """Release the hold on an authorization.

    PayPal answers ``204`` with no body, so the row is marked ``VOIDED`` locally
    rather than from a payload. Voiding is single-shot, hence the one idempotency
    key in this library that is not per attempt — see
    :func:`~paypal_checkout.models.void_request_id`.
    """
    paypal_id = _require_paypal_id(authorization, "authorization")

    payload = client.post(
        f"{AUTHORIZATIONS_PATH}/{paypal_id}/void",
        request_id=void_request_id(authorization.order_id, authorization.pk),
        idempotency=Idempotency.REQUIRED,
    )

    if payload:
        return authorization.update_from_payload(payload)

    authorization.status = Authorization.Status.VOIDED
    authorization.save(update_fields=["status", "updated_at"])
    return authorization
