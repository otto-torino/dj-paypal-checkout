Usage
=====

.. warning::

   Planned interface — not implemented yet. This page records the intended
   shape of the API so the milestones have a target to hit.

The flow
--------

1. Your server creates a PayPal order (**never** trust an amount coming from
   the browser — compute it from your own order).
2. The JS SDK v6 button takes the buyer through approval.
3. Your server captures the order.
4. A verified webhook confirms the capture and is the authoritative signal
   that the money moved; the capture response alone is not.

Both a synchronous and an asynchronous client will be provided with the same
surface.

Signals
-------

Business logic hangs off signals rather than views, so it runs the same way
whether a payment is confirmed by a capture call or by a webhook:

* ``payment_captured``
* ``payment_denied``
* ``payment_refunded``

Handlers must be idempotent: PayPal retries webhooks, and the same event may
be delivered more than once.
