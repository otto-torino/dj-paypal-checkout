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
from ..models import (
    Authorization,
    Capture,
    PayPalOrder,
    Refund,
    Subscription,
    SubscriptionPayment,
)
from ..money import parse_amount, parse_amount_payload
from ..signals import (
    payment_captured,
    payment_denied,
    payment_refunded,
    subscription_activated,
    subscription_cancelled,
    subscription_expired,
    subscription_payment_completed,
    subscription_payment_failed,
    subscription_suspended,
)

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


# -- subscriptions ----------------------------------------------------------


def _find_subscription(event, paypal_id):
    """Locate the local subscription, or decide it is not ours.

    Unlike captures there is no enclosing order to ask, so "unknown id" simply
    means someone else's subscription — nothing to retry.
    """
    if not paypal_id:
        return None
    subscription = Subscription.objects.filter(paypal_id=paypal_id).first()
    if subscription is None:
        logger.info(
            "subscription %s is not ours, ignoring %s", paypal_id, event.event_id
        )
    return subscription


def _apply_subscription_status(event, status, signal=None):
    resource = event.resource
    subscription = _find_subscription(event, resource.get("id"))
    if subscription is None:
        return
    subscription.update_from_payload(resource)
    if subscription.status != status:
        # The payload did not carry the status the event name implies; trust the
        # event, since that is what PayPal is telling us happened.
        subscription.status = status
        subscription.save(update_fields=["status", "updated_at"])
    if signal is not None:
        signal.send(
            sender=Subscription,
            subscription=subscription,
            target=subscription.target,
            reason=resource.get("status_change_note") or "",
        )


@register_handler("BILLING.SUBSCRIPTION.CREATED")
def handle_subscription_created(event):
    """Not billable yet: it still needs the buyer's approval."""
    subscription = _find_subscription(event, event.resource.get("id"))
    if subscription is not None:
        subscription.update_from_payload(event.resource)


@register_handler("BILLING.SUBSCRIPTION.UPDATED")
def handle_subscription_updated(event):
    subscription = _find_subscription(event, event.resource.get("id"))
    if subscription is not None:
        subscription.update_from_payload(event.resource)


@register_handler("BILLING.SUBSCRIPTION.ACTIVATED", "BILLING.SUBSCRIPTION.RE-ACTIVATED")
def handle_subscription_activated(event):
    _apply_subscription_status(
        event, Subscription.Status.ACTIVE, subscription_activated
    )


@register_handler("BILLING.SUBSCRIPTION.SUSPENDED")
def handle_subscription_suspended(event):
    _apply_subscription_status(
        event, Subscription.Status.SUSPENDED, subscription_suspended
    )


@register_handler("BILLING.SUBSCRIPTION.CANCELLED")
def handle_subscription_cancelled(event):
    _apply_subscription_status(
        event, Subscription.Status.CANCELLED, subscription_cancelled
    )


@register_handler("BILLING.SUBSCRIPTION.EXPIRED")
def handle_subscription_expired(event):
    _apply_subscription_status(
        event, Subscription.Status.EXPIRED, subscription_expired
    )


@register_handler("BILLING.SUBSCRIPTION.PAYMENT.FAILED")
def handle_subscription_payment_failed(event):
    """PayPal will retry per the plan, then suspend once the failures run out."""
    resource = event.resource
    subscription = _find_subscription(event, resource.get("id"))
    if subscription is None:
        return
    subscription.update_from_payload(resource)
    subscription_payment_failed.send(
        sender=Subscription,
        subscription=subscription,
        target=subscription.target,
        raw=resource,
    )


@register_handler("PAYMENT.SALE.COMPLETED")
def handle_subscription_payment(event):
    """A recurring charge. The link to the subscription is ``billing_agreement_id``.

    A sale without one is a one-off payment from the legacy Payments API, not our
    business.
    """
    resource = event.resource
    subscription_id = resource.get("billing_agreement_id")
    if not subscription_id:
        logger.info("sale %s carries no subscription, ignoring", resource.get("id"))
        return
    subscription = _find_subscription(event, subscription_id)
    if subscription is None:
        return

    sale_id = resource.get("id")
    if not sale_id:
        return

    amount, currency = _sale_amount(resource, subscription)
    payment, created = SubscriptionPayment.objects.get_or_create(
        paypal_id=sale_id,
        defaults={
            "subscription": subscription,
            "amount": amount,
            "currency": currency,
            "raw": resource,
        },
    )
    payment.update_from_payload(resource)
    subscription_payment_completed.send(
        sender=Subscription,
        subscription=subscription,
        target=subscription.target,
        payment=payment,
        created=created,
    )


def _sale_amount(resource, subscription):
    """Amount of a sale resource, which nests it differently from a capture."""
    payload_amount = resource.get("amount")
    if isinstance(payload_amount, dict):
        # Sales use {"total": "9.99", "currency": "EUR"}, not currency_code/value.
        total = payload_amount.get("total")
        currency = payload_amount.get("currency") or payload_amount.get("currency_code")
        if total is not None and currency:
            try:
                return parse_amount(total), str(currency).upper()
            except PayPalAmountError:
                logger.warning("sale %s has an unreadable amount", resource.get("id"))
        return parse_amount_payload(payload_amount)
    raise PayPalAmountError(
        f"sale {resource.get('id') or '<unknown>'} has no readable amount"
    )
