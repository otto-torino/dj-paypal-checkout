"""Signals — where business logic belongs.

Hang your logic here rather than in a view, so it runs the same way whether the
outcome arrived from a capture call or (M3) from a webhook.

**Handlers must be idempotent.** The same outcome can legitimately reach you
more than once: a capture call and its confirming webhook describe one event,
and PayPal retries webhooks. Mark your own order paid with a guard, do not
increment counters blindly.

Payment signals send ``sender=Capture`` and provide ``capture``, ``order`` and
``target`` (the host project's object, or ``None`` when the order was not
linked to one). Subscription and Vault signals use their corresponding local
models as senders and document their payloads below. Always accept ``**kwargs``
so receivers remain compatible as signal context grows.

When emitted by a webhook, receivers run synchronously inside the webhook's
database transaction. Keep them fast and limit them to idempotent database
writes. Persist external work in the host application's transactional outbox;
email or network calls cannot be rolled back if a later handler fails.
"""

from django.dispatch import Signal

__all__ = [
    "payment_captured",
    "payment_denied",
    "payment_refunded",
    "refund_attempt_merged",
    "subscription_activated",
    "subscription_suspended",
    "subscription_cancelled",
    "subscription_expired",
    "subscription_payment_completed",
    "subscription_payment_failed",
    "payment_token_created",
    "payment_token_deleted",
]

#: Money captured successfully. ``capture.status == COMPLETED``.
payment_captured = Signal()

#: The capture was refused (``DECLINED``/``FAILED``). No money moved.
payment_denied = Signal()

#: A capture was refunded, fully or partially. Carries ``refund`` when this
#: project initiated it; a webhook about someone else's refund does not.
payment_refunded = Signal()

#: A locally interrupted refund attempt was proved to be the same operation as
#: an already-observed PayPal refund. Carries the surviving ``refund`` and an
#: ``attempt`` metadata snapshot. This is an operational signal: no money moved.
refund_attempt_merged = Signal()


# -- subscriptions ----------------------------------------------------------
#
# These send ``sender=Subscription`` with ``subscription`` and ``target``.
# The lifecycle ones also pass ``reason`` when one is known.
#
# Note they fire from **two** places: the wrapper that performed the transition
# and the matching webhook. A subscription cancelled through the library will
# therefore signal twice, which is the same idempotency requirement as for
# payments — guard your handlers.

#: Billing started or resumed.
subscription_activated = Signal()

#: Billing paused; the subscription still exists.
subscription_suspended = Signal()

#: Ended for good.
subscription_cancelled = Signal()

#: Ran to the end of its billing cycles.
subscription_expired = Signal()

#: A recurring payment succeeded. Carries ``payment`` (a
#: :class:`~paypal_checkout.models.SubscriptionPayment`).
subscription_payment_completed = Signal()

#: A recurring payment failed. PayPal retries according to the plan's
#: ``payment_preferences``, and suspends the subscription once the allowed
#: failures run out.
subscription_payment_failed = Signal()


# -- Vault ------------------------------------------------------------------
#
# These send ``sender=PaymentToken`` with ``payment_token`` and ``target``.

#: A reusable payment method was saved successfully.
payment_token_created = Signal()

#: A saved payment method was removed from PayPal's vault.
payment_token_deleted = Signal()
