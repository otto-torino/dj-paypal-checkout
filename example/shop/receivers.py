"""Business logic lives here, not in the views.

The same handler runs whether the outcome came from the capture call or (from
M3) from a webhook — which is exactly why it has to be idempotent.
"""

import logging

from django.dispatch import receiver

from paypal_checkout import payment_captured, payment_denied

logger = logging.getLogger("shop")


@receiver(payment_captured)
def mark_order_paid(sender, capture, order, target, **kwargs):
    if target is None:
        return
    if target.paid:
        # Already handled — the capture response and its webhook describe one
        # event, and PayPal retries webhooks.
        return
    target.paid = True
    target.save(update_fields=["paid"])
    logger.info("order %s paid with capture %s", target.reference, capture.paypal_id)


@receiver(payment_denied)
def log_denied(sender, capture, order, target, **kwargs):
    logger.warning(
        "capture %s denied for order %s", capture.request_id, target and target.reference
    )
