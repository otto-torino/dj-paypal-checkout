"""Signals — where business logic belongs.

Hang your logic here rather than in a view, so it runs the same way whether the
outcome arrived from a capture call or (M3) from a webhook.

**Handlers must be idempotent.** The same outcome can legitimately reach you
more than once: a capture call and its confirming webhook describe one event,
and PayPal retries webhooks. Mark your own order paid with a guard, do not
increment counters blindly.

All signals send ``sender=Capture`` and provide ``capture``, ``order`` and
``target`` (the host project's object, or ``None`` when the order was not
linked to one).
"""

from django.dispatch import Signal

__all__ = ["payment_captured", "payment_denied", "payment_refunded"]

#: Money captured successfully. ``capture.status == COMPLETED``.
payment_captured = Signal()

#: The capture was refused (``DECLINED``/``FAILED``). No money moved.
payment_denied = Signal()

#: A capture was refunded, fully or partially. Sent from M4 onwards.
payment_refunded = Signal()
