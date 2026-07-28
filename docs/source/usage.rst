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

Declaring an idempotency policy
-------------------------------

The method heuristic above is only a fallback. An operation can declare what it
actually needs, which is a property of the operation rather than of the verb:

.. code-block:: python

   from paypal_checkout import Idempotency

   # Money moves: strict mode refuses this without a request_id.
   client.post(f"/v2/checkout/orders/{oid}/capture",
               request_id=f"order:{pk}:capture:1",
               idempotency=Idempotency.REQUIRED)

   # Side-effect-free POST: retryable with no key, never reported.
   client.post("/v1/notifications/verify-webhook-signature", json=payload,
               idempotency=Idempotency.NOT_APPLICABLE)

``OPTIONAL`` sits in between: it silences the report but does **not** make the
write repeatable — retry safety always follows the key, never the declaration.

Amounts
-------

Amounts go to PayPal as strings with a currency-correct number of decimals, and
:mod:`paypal_checkout.money` is the only thing that should build them:

.. code-block:: python

   >>> from decimal import Decimal
   >>> from paypal_checkout import amount_payload, format_amount
   >>> format_amount(Decimal("10.1"), "EUR")
   '10.10'
   >>> format_amount(1000, "JPY")     # HUF, JPY and TWD take no decimals
   '1000'
   >>> amount_payload(Decimal("10.50"), "EUR")
   {'currency_code': 'EUR', 'value': '10.50'}

Two things it refuses outright, both raising
:class:`~paypal_checkout.exceptions.PayPalAmountError`:

.. code-block:: python

   >>> format_amount(10.0, "EUR")        # a float amount is a bug
   PayPalAmountError: refusing to build an amount from the float 10.0 ...
   >>> format_amount(Decimal("10.005"), "EUR")   # would lose a digit
   PayPalAmountError: 10.005 cannot be expressed in EUR, which takes 2 decimals ...

Padding is fine (``10.1`` → ``"10.10"``); *dropping* digits is not. Rounding is
your decision, not the library's — silently rounding would charge the buyer
something other than what your own records say.

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

Orders
------

:mod:`paypal_checkout.orders` is the layer you should normally use: it builds the
amount, writes a row before calling PayPal, and supplies the persisted
idempotency key for you. Import it from the module (not from the package root —
it touches models, so it must be imported after the app registry is ready):

.. code-block:: python

   from paypal_checkout import PayPalClient
   from paypal_checkout.orders import create_order, capture_order

   with PayPalClient() as client:
       order = create_order(client, amount=cart.total, target=cart)
       # order.paypal_id goes to the JS SDK; the browser never sees the amount.

   # ...after the buyer approves...
   with PayPalClient() as client:
       capture = capture_order(client, order)
       if capture.is_successful:
           ...

``create_order`` accepts ``currency`` (defaults to ``PAYPAL['CURRENCY']``),
``intent``, ``target`` (any model instance — it is linked through a generic FK),
``application_context``, ``payment_source``, and ``purchase_units`` for orders it
cannot express on its own. When you pass your own units their total must match
``amount``, and a mismatch is refused before any call: the local row and PayPal
have to agree on what the buyer is charged.

``capture_order`` takes ``amount`` for a partial capture. Also available:
``refresh_order(client, order)`` to re-read an order into its row, and
``fetch_order(client, paypal_id)`` for a raw read with no local row.

These helpers are synchronous. The async client is available for direct calls,
but the wrappers are not async yet.

The front end
-------------

The template tags load the **JS SDK v6** for the configured environment and
publish its public configuration as JSON — sandbox and live are different hosts,
which is easy to get wrong by hand:

.. code-block:: html+django

   {% load paypal_checkout %}
   {% paypal_sdk %}

That renders the SDK ``<script>`` plus a
``<script type="application/json" id="paypal-sdk-config">`` block holding
``clientId``, ``environment``, ``currency`` and ``components``. Reading it from
JSON rather than templating values into code keeps the page safe even if a value
contains ``</script>``:

.. code-block:: javascript

   const cfg = JSON.parse(
       document.getElementById("paypal-sdk-config").textContent);

   const sdk = await window.paypal.createInstance({
       clientId: cfg.clientId,
       components: cfg.components,          // ["paypal-payments"]
   });

   const session = sdk.createPayPalOneTimePaymentSession({
       async onApprove(data) {
           await fetch(`/checkout/${data.orderId}/capture/`, {method: "POST"});
       },
       onCancel() {}, onError(error) { console.error(error); },
   });

   button.addEventListener("click", async () => {
       await session.start({presentationMode: "auto"}, createOrder());
   });

``createOrder()`` must resolve to ``{orderId: "..."}`` — your endpoint calls
:func:`~paypal_checkout.orders.create_order` and returns ``order.paypal_id``. The
browser never sees or sends the amount.

Only the client id, environment and currency are ever emitted; the secret cannot
reach a template. Pass ``{% paypal_sdk "paypal-payments,venmo-payments" %}`` for
more components, and use
:func:`paypal_checkout.templatetags.paypal_checkout.sdk_config` if you would
rather serve the same values from a view.

The tags stop there on purpose: wiring buttons to your own create and capture
endpoints is application code, since URLs, CSRF and error handling differ per
project.

Authorize now, capture later
----------------------------

With ``intent=AUTHORIZE`` the money is held rather than taken, and captured
afterwards against the authorization:

.. code-block:: python

   from paypal_checkout.models import PayPalOrder
   from paypal_checkout.orders import (
       create_order, authorize_order, capture_authorization,
   )

   order = create_order(client, amount=cart.total,
                        intent=PayPalOrder.Intent.AUTHORIZE, target=cart)
   # ...buyer approves...
   authorization = authorize_order(client, order)   # money held
   authorization.expires_at                         # PayPal's hold expiry

   # ...when you ship...
   capture = capture_authorization(client, authorization)

``capture_authorization`` takes ``amount`` for a partial capture, exactly like
``capture_order``, and fires the same signals. Captures made this way carry
``capture.authorization``; direct order captures leave it ``None``, and the two
pools are kept separate so a recovery never mistakes one for the other.

What survives a crash
---------------------

Every operation writes its row **first**, with status ``INITIATED``, so an
interrupted call is discoverable rather than lost:

.. code-block:: python

   from paypal_checkout.models import PayPalOrder

   PayPalOrder.objects.pending()        # started locally, never confirmed
   order.pending_capture()              # a capture attempt of unknown outcome
   order.pending_authorization()        # ditto, for an authorization
   capture.is_unconfirmed               # True while the outcome is unknown

An unconfirmed capture attempt is **reused** by the next ``capture_order`` call,
key and all — its outcome is unknown, so it may have reached PayPal, and reusing
the key lets PayPal deduplicate it. A *new* attempt after a decline gets its own
row and its own key, which is why the key carries the attempt
(``order:42:capture:7``) rather than being fixed per order.

If PayPal answers a capture without a capture object in it, the attempt is left
``INITIATED`` on purpose: money may have moved, and recording a guess would be
worse than recording "unknown".

Signals
-------

Business logic belongs here rather than in a view, so it runs the same way
whether the outcome arrived from a capture call or (from M3) from a webhook:

.. code-block:: python

   from django.dispatch import receiver
   from paypal_checkout import payment_captured

   @receiver(payment_captured)
   def mark_paid(sender, capture, order, target, **kwargs):
       if target and not target.paid:      # idempotent on purpose
           target.paid = True
           target.save(update_fields=["paid"])

* ``payment_captured`` — money captured (``COMPLETED``)
* ``payment_denied`` — refused (``DECLINED``/``FAILED``); no money moved
* ``payment_refunded`` — from M4

All three send ``sender=Capture`` with ``capture``, ``order`` and ``target``
(the linked object, or ``None``). A ``PENDING`` capture sends nothing: it is not
an outcome yet.

**Handlers must be idempotent.** The same outcome can legitimately reach you
twice — a capture call and its confirming webhook describe one event, and PayPal
retries webhooks. Guard your writes; never increment blindly.
