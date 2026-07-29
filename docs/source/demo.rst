Runnable demo
=============

``example/`` is a complete two-endpoint checkout against the PayPal **sandbox**.
Get sandbox credentials from a REST app in the `PayPal developer dashboard
<https://developer.paypal.com/dashboard/>`__, then:

.. code-block:: bash

   cp example/.env.example example/.env
   # Edit example/.env and replace the two credential placeholders.
   ./run_demo.sh

``run_demo.sh`` loads ``example/.env`` automatically. At minimum it must contain
the sandbox app's ``PAYPAL_CLIENT_ID`` and ``PAYPAL_CLIENT_SECRET``, as shown in
``example/.env.example``. The real file is ignored by Git: never commit it and
never use live credentials for the demo. Environment variables exported by the
calling shell remain supported as an alternative.

The script creates a venv, installs the library, migrates, creates an
``admin``/``password`` superuser and serves:

* http://127.0.0.1:8000/ — the checkout page
* http://127.0.0.1:8000/admin/ — the read-only PayPal admin

Popup security header
---------------------

Django's :class:`~django.middleware.security.SecurityMiddleware` defaults
``Cross-Origin-Opener-Policy`` to ``same-origin``. That isolates a
cross-origin popup from ``window.opener`` and prevents the PayPal Web SDK from
finishing its handoff. The demo therefore sets:

.. code-block:: python

   SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin-allow-popups"

Apply the same setting in a host project that uses the PayPal popup. This is
the policy PayPal recommends for its Web SDK; do not disable COOP entirely.

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

Webhooks
--------

``/paypal/webhook/`` is mounted and ready. To exercise it locally, expose the
server through a tunnel, register that URL in the dashboard, and set the id it
returns:

.. code-block:: bash

   PAYPAL_WEBHOOK_ID=your-sandbox-webhook-id

Add that line to ``example/.env``. It is optional for the synchronous
create/approve/capture flow.

Then watch the ``WebhookEvent`` rows in the admin: ``processed`` and
``last_error`` show exactly what happened, and the same
``payment_captured`` receiver runs whether the confirmation arrived from the
capture call or from the webhook. See :doc:`webhooks`.

Scope
-----

The runnable demo deliberately remains a one-off checkout example. Subscription
support has more merchant-specific choices — plan cadence, trials, return URLs
and the host project's membership model — so a generic demo would imply policy
the library does not own. See :doc:`subscriptions` for the complete server-side
flow and use the read-only admin to inspect its local rows.

Testing a payment
-----------------

Log in with a sandbox personal account (Testing Tools → Sandbox Accounts in the
dashboard) when the PayPal window opens. Nothing is charged.
