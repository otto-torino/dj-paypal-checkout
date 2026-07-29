"""Signals — where business logic belongs.

Hang your logic here rather than in a view, so it runs the same way whether the
outcome arrived from a capture call or (M3) from a webhook.

**Handlers must be idempotent.** The same outcome can legitimately reach you
more than once: a capture call and its confirming webhook describe one event,
and PayPal retries webhooks. Mark your own order paid with a guard, do not
increment counters blindly.

All signals send ``sender=Capture`` and provide ``capture``, ``order`` and
``target`` (the host project's object, or ``None`` when the order was not
linked to one). ``payment_refunded`` additionally provides ``refund`` when the
refund is known — a webhook can report a refund this project never initiated, in
which case it is absent, so always accept ``**kwargs``.
"""

from django.dispatch import Signal

__all__ = [
    "payment_captured",
    "payment_denied",
    "payment_refunded",
    "subscription_activated",
    "subscription_suspended",
    "subscription_cancelled",
    "subscription_expired",
    "subscription_payment_completed",
    "subscription_payment_failed",
]

#: Money captured successfully. ``capture.status == COMPLETED``.
payment_captured = Signal()

#: The capture was refused (``DECLINED``/``FAILED``). No money moved.
payment_denied = Signal()

#: A capture was refunded, fully or partially. Carries ``refund`` when this
#: project initiated it; a webhook about someone else's refund does not.
payment_refunded = Signal()


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
