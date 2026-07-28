Usage
=====

The flow
--------

1. Your server creates a PayPal order (**never** trust an amount coming from
   the browser — compute it from your own order).
2. The JS SDK v6 button takes the buyer through approval.
3. Your server captures the order.
4. A verified webhook confirms the capture and is the authoritative signal
   that the money moved; the capture response alone is not.

The client
----------

Steps 1 and 3 go through the HTTP client. Both a synchronous and an
asynchronous client are provided, with the same surface:

.. code-block:: python

   from paypal_checkout import PayPalClient

   with PayPalClient() as client:
       order = client.post(
           "/v2/checkout/orders",
           json={
               "intent": "CAPTURE",
               "purchase_units": [{"amount": {"currency_code": "EUR", "value": "10.00"}}],
           },
           request_id=f"order-{shop_order.pk}",
       )

.. code-block:: python

   from paypal_checkout import AsyncPayPalClient

   async with AsyncPayPalClient() as client:
       order = await client.get(f"/v2/checkout/orders/{order_id}")

Authentication is handled for you: the client fetches an OAuth2 token, caches
it, and on a ``401`` re-authenticates once and replays the request.

``request_id`` and why it matters
---------------------------------

``request_id`` is sent as the ``PayPal-Request-Id`` header, which PayPal uses
to deduplicate writes. It is also what makes a retry safe, so the client
treats it as the deciding factor:

* ``GET``/``HEAD``/``OPTIONS``/``PUT``/``DELETE`` — retried on ``429`` and
  ``5xx``, and on connection errors.
* ``POST``/``PATCH`` **with** ``request_id`` — retried, because PayPal will
  collapse the duplicate.
* ``POST``/``PATCH`` **without** ``request_id`` — **never** retried. A repeated
  capture could charge the buyer twice, so the error is raised instead.

The value must be **stable for the same operation** and **different for
different operations**, and it should be persisted before the call so that a
retry after a crash or a re-run job reuses it instead of minting a new one:

.. code-block:: text

   order:<pk>:authorize
   order:<pk>:capture:<capture-attempt>
   order:<pk>:refund:<refund-pk>

Two mistakes to avoid:

* a fresh ``uuid4()`` per attempt — PayPal sees a new request, and the
  protection is gone;
* a *fixed* string such as ``capture-<order_pk>`` — it would block a legitimate
  second attempt after a decline, because PayPal would replay the response of
  the first one. Hence the attempt counter above.

Note the two different layers. Retries *within* a single call already reuse one
id, including the replay after a ``401``. Recovering *across* a crash, a
restart or a re-run job is what needs a persistent, deterministic id from your
application — which is why the higher-level order/payment helpers will own it
rather than leaving it to the caller.

The client never invents an id for you: an auto-generated UUID would make
retries *look* safe while offering nothing after a crash, since the re-run would
mint a different one. A missing ``request_id`` is information — it says the
caller has not declared the persistent identity of the operation — so the client
reports it (a warning, or an error under ``STRICT_IDEMPOTENCY``) instead of
papering over it.

Errors
------

Everything raised inherits from
:class:`~paypal_checkout.exceptions.PayPalError`:

.. code-block:: python

   from paypal_checkout import PayPalAPIError, PayPalConnectionError

   try:
       capture = client.post(f"/v2/checkout/orders/{order_id}/capture",
                             request_id=f"capture-{shop_order.pk}")
   except PayPalConnectionError:
       # No response at all — the outcome is unknown; reconcile before retrying.
       ...
   except PayPalAPIError as exc:
       # exc.status_code, exc.name, exc.message, exc.details
       logger.error("PayPal refused the capture: %s (debug_id=%s)", exc, exc.debug_id)

``debug_id`` is the value PayPal support asks for, so it is on the exception
and in its ``str()``. The subclasses are
:class:`~paypal_checkout.exceptions.PayPalAuthenticationError` (401/403),
:class:`~paypal_checkout.exceptions.PayPalValidationError` (400/422),
:class:`~paypal_checkout.exceptions.PayPalNotFoundError` (404),
:class:`~paypal_checkout.exceptions.PayPalRateLimitError` (429, carrying
``retry_after``) and
:class:`~paypal_checkout.exceptions.PayPalServerError` (5xx).

Orders, models and signals
--------------------------

.. warning::

   Not implemented yet — planned for M2/M3, see ``PROGRESS.md``. The section
   below records the intended shape.

Business logic will hang off signals rather than views, so it runs the same way
whether a payment is confirmed by a capture call or by a webhook:

* ``payment_captured``
* ``payment_denied``
* ``payment_refunded``

Handlers must be idempotent: PayPal retries webhooks, and the same event may
be delivered more than once.
