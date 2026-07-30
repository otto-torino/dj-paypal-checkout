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

from django.db import transaction

from .client import Idempotency
from .exceptions import PayPalAPIError, PayPalAmountError, PayPalError
from .models import Authorization, Capture, Refund, void_request_id
from .money import amount_payload, parse_amount_payload
from .signals import payment_refunded, refund_attempt_merged

__all__ = [
    "CAPTURES_PATH",
    "AUTHORIZATIONS_PATH",
    "merge_refund_attempt",
    "refund_capture",
    "retry_refund",
    "void_authorization",
]

logger = logging.getLogger(__name__)

CAPTURES_PATH = "/v2/payments/captures"
AUTHORIZATIONS_PATH = "/v2/payments/authorizations"

# A response carrying one of these issues proves that this invocation did not
# create a refund. Unknown 4xx errors remain unresolved: status class alone is
# not enough evidence for a money-state transition.
TERMINAL_REFUND_ISSUES = frozenset(
    {
        "CAPTURE_FULLY_REFUNDED",
        "CAPTURED_AMOUNT_FULLY_REFUNDED",
        "PREVIOUSLY_REFUNDED",
        "REFUND_AMOUNT_EXCEEDED",
        "REFUND_FAILED_BY_PAYMENT_SOURCE",
        "TRANSACTION_REFUSED",
    }
)


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

    ``amount`` defaults to the whole capture. An unconfirmed previous attempt
    must be retried explicitly with :func:`retry_refund`: silently reusing it
    would make the new arguments lie about the request actually sent.

    Raises :class:`~paypal_checkout.exceptions.PayPalAmountError` if the refund
    would exceed what is still refundable.
    """
    paypal_id = _require_paypal_id(capture, "capture")
    body = _refund_body(
        capture,
        amount=amount,
        note_to_payer=note_to_payer,
        invoice_id=invoice_id,
    )
    refund = capture.start_refund(
        amount=amount,
        note_to_payer=note_to_payer,
        invoice_id=invoice_id,
        sent_body=body,
    )
    return _post_refund(client, refund, paypal_id=paypal_id)


def _refund_body(capture, *, amount, note_to_payer, invoice_id):
    """Build the canonical body persisted before the first network call."""
    body = {}
    if amount is not None:
        body["amount"] = amount_payload(amount, capture.currency)
    if note_to_payer:
        body["note_to_payer"] = note_to_payer
    if invoice_id:
        body["invoice_id"] = invoice_id
    return body


def retry_refund(client, refund):
    """Retry exactly one interrupted refund, key and original body included.

    Legacy rows whose body predates ``sent_body`` are refused: reconstructing a
    full-versus-partial request from the amount would be a guess.
    """
    try:
        refund.refresh_from_db()
    except Refund.DoesNotExist:
        # A concurrent retry may already have merged this duplicate into the
        # webhook-observed row. The request id migrates to the survivor.
        survivor = Refund.objects.filter(request_id=refund.request_id).first()
        if survivor is not None:
            return survivor
        raise
    if refund.status not in (Refund.Status.INITIATED, Refund.Status.UNRESOLVED):
        raise PayPalError(
            f"refund #{refund.pk} is {refund.status}, not an unresolved attempt."
        )
    if refund.sent_body is None:
        raise PayPalError(
            f"refund #{refund.pk} predates persisted request bodies and cannot be "
            "retried safely; review it against PayPal."
        )
    paypal_id = _require_paypal_id(refund.capture, "capture")
    return _post_refund(client, refund, paypal_id=paypal_id)


def _post_refund(client, refund, *, paypal_id):
    """Send a persisted refund request and settle its local row."""
    try:
        payload = client.post(
            f"{CAPTURES_PATH}/{paypal_id}/refund",
            json=refund.sent_body or None,
            request_id=refund.request_id,
            idempotency=Idempotency.REQUIRED,
        )
    except PayPalAPIError as exc:
        if _refund_error_issues(exc) & TERMINAL_REFUND_ISSUES:
            _mark_refund_failed(refund, exc)
        raise

    refund, notify = _settle_refund(refund, payload)
    if refund.is_successful:
        refund.capture.sync_refund_status()
    if notify:
        _notify_refunded(refund)
    return refund


def _refund_error_issues(exc):
    issues = {exc.name} if exc.name else set()
    issues.update(
        detail.get("issue")
        for detail in exc.details
        if isinstance(detail, dict) and detail.get("issue")
    )
    return issues


def _mark_refund_failed(refund, exc):
    with transaction.atomic():
        Capture.objects.select_for_update().get(pk=refund.capture_id)
        current = (
            Refund.objects.select_for_update()
            .filter(pk=refund.pk)
            .first()
        )
        if current is None:
            current = (
                Refund.objects.select_for_update()
                .filter(request_id=refund.request_id)
                .first()
            )
        if current is None:
            return
        if current.status in (Refund.Status.INITIATED, Refund.Status.UNRESOLVED):
            current.status = Refund.Status.FAILED
            current.raw = {"error": exc.payload}
            current.save(update_fields=["status", "raw", "updated_at"])


def _settle_refund(refund, payload):
    """Apply a response, merging a webhook-adopted duplicate when necessary."""
    paypal_id = payload.get("id")
    with transaction.atomic():
        Capture.objects.select_for_update().get(pk=refund.capture_id)
        attempt = (
            Refund.objects.select_for_update()
            .filter(pk=refund.pk)
            .first()
        )
        if attempt is None:
            survivor = (
                Refund.objects.select_for_update()
                .filter(request_id=refund.request_id)
                .first()
            )
            if survivor is None:
                raise Refund.DoesNotExist(
                    f"refund attempt #{refund.pk} disappeared without a merge survivor"
                )
            return survivor, False
        adopted = None
        if paypal_id:
            adopted = (
                Refund.objects.select_for_update()
                .filter(paypal_id=paypal_id)
                .exclude(pk=attempt.pk)
                .first()
            )
        if adopted is not None:
            survivor = _merge_refund_attempt(attempt, adopted)
            return survivor, False

        attempt.update_from_payload(payload)
        return attempt, attempt.is_successful


def _merge_refund_attempt(attempt, survivor):
    """Fold a proved duplicate attempt into the already-observed remote row."""
    history = list((survivor.merge_metadata or {}).get("merged_attempts") or [])
    snapshot = {
        "pk": attempt.pk,
        "created_at": attempt.created_at.isoformat(),
        "request_id": attempt.request_id,
        "sent_body": attempt.sent_body,
        "amount": str(attempt.amount),
        "currency": attempt.currency,
    }
    history.append(snapshot)
    request_id = attempt.request_id
    attempt.request_id = None
    attempt.save(update_fields=["request_id", "updated_at"])

    survivor.request_id = request_id
    survivor.sent_body = attempt.sent_body
    survivor.note_to_payer = attempt.note_to_payer
    survivor.invoice_id = attempt.invoice_id
    survivor.merge_metadata = {
        **(survivor.merge_metadata or {}),
        "merged_attempts": history,
    }
    survivor.save(
        update_fields=[
            "request_id",
            "sent_body",
            "note_to_payer",
            "invoice_id",
            "merge_metadata",
            "updated_at",
        ]
    )
    attempt_pk = attempt.pk
    attempt.delete()
    logger.warning(
        "merged duplicate refund attempt %s into confirmed refund %s",
        attempt_pk,
        survivor.pk,
        extra={
            "paypal_issue": "refund_attempt_merged",
            "paypal_refund_id": survivor.paypal_id,
        },
    )
    refund_attempt_merged.send(
        sender=Refund,
        refund=survivor,
        attempt=snapshot,
    )
    return survivor


def merge_refund_attempt(attempt, survivor):
    """Merge a locally interrupted attempt into a proved remote refund.

    This is the explicit recovery path for legacy attempts that cannot be
    retried. The caller must first establish outside this library that both
    rows describe the same PayPal operation.
    """
    if attempt.pk == survivor.pk:
        raise PayPalError("a refund attempt cannot be merged into itself.")

    with transaction.atomic():
        Capture.objects.select_for_update().get(pk=attempt.capture_id)
        locked_attempt = Refund.objects.select_for_update().get(pk=attempt.pk)
        locked_survivor = Refund.objects.select_for_update().get(pk=survivor.pk)

        if locked_attempt.capture_id != locked_survivor.capture_id:
            raise PayPalError("refunds from different captures cannot be merged.")
        if locked_attempt.status not in (
            Refund.Status.INITIATED,
            Refund.Status.UNRESOLVED,
        ):
            raise PayPalError(
                f"refund #{locked_attempt.pk} is {locked_attempt.status}, not "
                "an unresolved attempt."
            )
        if not locked_survivor.paypal_id:
            raise PayPalError("the surviving refund must have a PayPal id.")
        if locked_survivor.request_id:
            raise PayPalError(
                "the surviving refund is already associated with a local attempt."
            )
        return _merge_refund_attempt(locked_attempt, locked_survivor)


def _notify_refunded(refund):
    capture = refund.capture
    payment_refunded.send(
        sender=Capture,
        capture=capture,
        order=capture.order,
        target=capture.order.target,
        refund=refund,
    )


def capture_id_from_refund(resource):
    """Return the parent capture id carried by a refund representation."""
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    if isinstance(related, dict) and related.get("capture_id"):
        return related["capture_id"]
    for link in resource.get("links") or []:
        if not isinstance(link, dict) or link.get("rel") != "up":
            continue
        href = (link.get("href") or "").rstrip("/")
        if "/captures/" in href:
            return href.rsplit("/", 1)[-1]
    return None


def adopt_remote_refund(capture, resource):
    """Record a remote refund without guessing which local attempt it is.

    When an interrupted local attempt exists, it becomes ``UNRESOLVED``. A
    later idempotent retry either proves both rows are the same and merges them,
    or returns a distinct refund id.
    """
    refund_id = resource.get("id")
    if not refund_id:
        return None

    with transaction.atomic():
        locked_capture = Capture.objects.select_for_update().get(pk=capture.pk)
        refund = (
            Refund.objects.select_for_update().filter(paypal_id=refund_id).first()
        )
        if refund is not None:
            return refund.update_from_payload(resource)

        amount = locked_capture.amount
        currency = locked_capture.currency
        payload_amount = resource.get("amount")
        if isinstance(payload_amount, dict):
            try:
                amount, currency = parse_amount_payload(payload_amount)
            except PayPalAmountError:
                logger.warning(
                    "refund %s has an unreadable amount; recording the capture's",
                    refund_id,
                )

        refund = locked_capture.refunds.create(
            paypal_id=refund_id,
            status=Refund.Status.INITIATED,
            amount=amount,
            currency=currency,
        )
        refund.update_from_payload(resource)
        locked_capture.refunds.filter(status=Refund.Status.INITIATED).exclude(
            pk=refund.pk
        ).update(status=Refund.Status.UNRESOLVED)
        return refund


def settle_reconciled_refund(refund, payload):
    """Settle one unambiguous local/remote match found through its order."""
    settled, notify = _settle_refund(refund, payload)
    if settled.is_successful:
        settled.capture.sync_refund_status()
    if notify:
        _notify_refunded(settled)
    return settled


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
