"""Event type → handler registry.

Handlers turn a stored :class:`~paypal_checkout.models.WebhookEvent` into local
state and signals. They must be **idempotent**: PayPal retries deliveries, and
an event often describes something a capture call already told us.

A handler that raises makes the endpoint answer 5xx, so PayPal retries — which
is the right outcome for work that genuinely has not been done yet. Raise
:class:`~paypal_checkout.exceptions.PayPalWebhookNotReady` for the specific case
of "this is ours, but the row is not here yet".
"""

import logging

from ..exceptions import PayPalAmountError, PayPalWebhookNotReady
from ..models import Authorization, Capture, PayPalOrder, Refund
from ..money import parse_amount_payload
from ..signals import payment_captured, payment_denied, payment_refunded

__all__ = [
    "register_handler",
    "unregister_handlers",
    "get_handlers",
    "dispatch",
    "registered_event_types",
]

logger = logging.getLogger(__name__)

_HANDLERS = {}


def register_handler(*event_types):
    """Register a callable for one or more PayPal event types.

    .. code-block:: python

        @register_handler("PAYMENT.CAPTURE.COMPLETED")
        def on_capture(event):
            ...
    """

    def decorator(func):
        for event_type in event_types:
            _HANDLERS.setdefault(event_type, []).append(func)
        return func

    return decorator


def unregister_handlers(event_type):
    """Remove every handler for ``event_type`` and return them.

    Use it to replace a built-in handler with your own, or to undo a
    registration in tests.
    """
    return _HANDLERS.pop(event_type, [])


def get_handlers(event_type):
    return list(_HANDLERS.get(event_type, ()))


def registered_event_types():
    return sorted(_HANDLERS)


def dispatch(event):
    """Run every handler registered for ``event``'s type.

    An event nobody handles is not an error: PayPal delivers whatever the
    webhook subscription covers, and storing it is already useful.
    """
    handlers = get_handlers(event.event_type)
    if not handlers:
        logger.info("no handler for %s (%s), stored only", event.event_type, event.event_id)
        return 0
    for handler in handlers:
        handler(event)
    return len(handlers)


def _related_ids(resource):
    """The ids PayPal nests in a capture/refund/authorization resource."""
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    return related if isinstance(related, dict) else {}


def _related_order_id(resource):
    return _related_ids(resource).get("order_id")


def _is_ours_but_missing(resource):
    """True when the resource belongs to an order we own but the row is absent."""
    order_id = _related_order_id(resource)
    return bool(order_id) and PayPalOrder.objects.filter(paypal_id=order_id).exists()


def _find_capture(event):
    """Locate the local capture this event is about.

    Three outcomes, and the difference matters:

    * found — update it;
    * not found, and the order is unknown to us — not our payment, ignore it;
    * not found, but we *do* know the order — the webhook overtook our own
      capture response, so raise and let PayPal retry.
    """
    resource = event.resource
    paypal_id = resource.get("id")
    if not paypal_id:
        return None

    capture = Capture.objects.filter(paypal_id=paypal_id).first()
    if capture is not None:
        return capture

    if _is_ours_but_missing(resource):
        raise PayPalWebhookNotReady(
            f"capture {paypal_id} belongs to order {_related_order_id(resource)}, "
            "which we know, but the capture row is not stored yet — retry."
        )
    logger.info("capture %s is not ours, ignoring %s", paypal_id, event.event_id)
    return None


@register_handler("PAYMENT.CAPTURE.COMPLETED")
def handle_capture_completed(event):
    capture = _find_capture(event)
    if capture is None:
        return
    capture.update_from_payload(event.resource)
    payment_captured.send(
        sender=Capture, capture=capture, order=capture.order, target=capture.order.target
    )


@register_handler("PAYMENT.CAPTURE.DENIED")
def handle_capture_denied(event):
    capture = _find_capture(event)
    if capture is None:
        return
    capture.update_from_payload(event.resource)
    payment_denied.send(
        sender=Capture, capture=capture, order=capture.order, target=capture.order.target
    )


@register_handler("PAYMENT.CAPTURE.PENDING")
def handle_capture_pending(event):
    """Not an outcome yet — record it and wait for the next event."""
    capture = _find_capture(event)
    if capture is not None:
        capture.update_from_payload(event.resource)


@register_handler("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED")
def handle_capture_refunded(event):
    """For these events the resource is the **refund**, not the capture.

    So the capture is found through ``related_ids.capture_id``, and a refund we
    have never seen is adopted rather than dropped — that is how a refund issued
    straight from the PayPal dashboard ends up in the local records.
    """
    resource = event.resource
    refund_id = resource.get("id")
    related = _related_ids(resource)
    capture_id = related.get("capture_id")

    capture_id = capture_id or _capture_id_from_links(resource)
    capture = (
        Capture.objects.filter(paypal_id=capture_id).first() if capture_id else None
    )
    if capture is None:
        if _is_ours_but_missing(resource):
            raise PayPalWebhookNotReady(
                f"refund {refund_id} belongs to order {_related_order_id(resource)}, "
                "which we know, but its capture row is not stored yet — retry."
            )
        logger.info("refund %s is not ours, ignoring %s", refund_id, event.event_id)
        return

    refund = _adopt_refund(capture, resource)
    capture.sync_refund_status()
    payment_refunded.send(
        sender=Capture,
        capture=capture,
        order=capture.order,
        target=capture.order.target,
        refund=refund,
    )


def _capture_id_from_links(resource):
    """Fall back to the ``up`` link, which points at the refunded capture."""
    for link in resource.get("links") or []:
        if not isinstance(link, dict) or link.get("rel") != "up":
            continue
        href = (link.get("href") or "").rstrip("/")
        if "/captures/" in href:
            return href.rsplit("/", 1)[-1]
    return None


def _adopt_refund(capture, resource):
    """Find or create the local refund row this event describes."""
    refund_id = resource.get("id")
    if not refund_id:
        return None
    refund = Refund.objects.filter(paypal_id=refund_id).first()
    if refund is not None:
        return refund.update_from_payload(resource)

    amount = capture.amount
    currency = capture.currency
    payload_amount = resource.get("amount")
    if isinstance(payload_amount, dict):
        try:
            amount, currency = parse_amount_payload(payload_amount)
        except PayPalAmountError:
            logger.warning(
                "refund %s has an unreadable amount; recording the capture's",
                refund_id,
            )
    # No request_id: we did not initiate this one (a dashboard refund, say).
    refund = capture.refunds.create(
        paypal_id=refund_id,
        status=Refund.Status.INITIATED,
        amount=amount,
        currency=currency,
    )
    return refund.update_from_payload(resource)


@register_handler("CHECKOUT.ORDER.APPROVED", "CHECKOUT.ORDER.COMPLETED")
def handle_order_event(event):
    resource = event.resource
    paypal_id = resource.get("id")
    if not paypal_id:
        return
    order = PayPalOrder.objects.filter(paypal_id=paypal_id).first()
    if order is None:
        logger.info("order %s is not ours, ignoring %s", paypal_id, event.event_id)
        return
    order.update_from_payload(resource)


@register_handler(
    "PAYMENT.AUTHORIZATION.CREATED",
    "PAYMENT.AUTHORIZATION.VOIDED",
)
def handle_authorization_event(event):
    resource = event.resource
    paypal_id = resource.get("id")
    if not paypal_id:
        return
    authorization = Authorization.objects.filter(paypal_id=paypal_id).first()
    if authorization is None:
        logger.info("authorization %s is not ours, ignoring %s", paypal_id, event.event_id)
        return
    authorization.update_from_payload(resource)
