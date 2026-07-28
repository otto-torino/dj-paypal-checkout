Runnable demo
=============

``example/`` is a complete two-endpoint checkout against the PayPal **sandbox**.
Get sandbox credentials from a REST app in the `PayPal developer dashboard
<https://developer.paypal.com/dashboard/>`__, then:

.. code-block:: bash

   export PAYPAL_CLIENT_ID=...
   export PAYPAL_CLIENT_SECRET=...
   ./run_demo.sh

The script creates a venv, installs the library, migrates, creates an
``admin``/``password`` superuser and serves:

* http://127.0.0.1:8000/ — the checkout page
* http://127.0.0.1:8000/admin/ — the read-only PayPal admin

What it demonstrates
--------------------

**The server owns the amount.** ``shop/views.py`` reads the total from its own
``Order`` row; the browser only ever receives a PayPal order id. There is no
request parameter that could change what the buyer pays.

**Two endpoints, nothing more.** ``paypal/create/`` calls
:func:`~paypal_checkout.orders.create_order` and returns ``{"orderId": ...}``;
``paypal/<id>/capture/`` calls :func:`~paypal_checkout.orders.capture_order`.

**Business logic hangs off signals.** ``shop/receivers.py`` marks the shop order
paid on ``payment_captured``, guarded so that receiving the same outcome twice
(capture response *and* webhook) does not double-apply anything.

**Strict idempotency is on.** ``demo/settings.py`` sets
``STRICT_IDEMPOTENCY: True``, the posture this library is heading towards, which
the order helpers satisfy by construction.

The checkout page also lists the local ``PayPalOrder`` rows with their
idempotency keys, so you can watch a row appear *before* PayPal is called.

Testing a payment
-----------------

Log in with a sandbox personal account (Testing Tools → Sandbox Accounts in the
dashboard) when the PayPal window opens. Nothing is charged.
