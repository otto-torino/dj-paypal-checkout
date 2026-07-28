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

from ..exceptions import PayPalWebhookNotReady
from ..models import Authorization, Capture, PayPalOrder
from ..signals import payment_captured, payment_denied, payment_refunded

__all__ = ["register_handler", "get_handlers", "dispatch", "registered_event_types"]

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


def _related_order_id(resource):
    """The order id PayPal nests in a capture/authorization resource."""
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    return related.get("order_id")


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

    order_id = _related_order_id(resource)
    if order_id and PayPalOrder.objects.filter(paypal_id=order_id).exists():
        raise PayPalWebhookNotReady(
            f"capture {paypal_id} belongs to order {order_id}, which we know, but the "
            "capture row is not stored yet — retry."
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
    capture = _find_capture(event)
    if capture is None:
        return
    capture.update_from_payload(event.resource)
    payment_refunded.send(
        sender=Capture, capture=capture, order=capture.order, target=capture.order.target
    )


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
