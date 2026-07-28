dj-paypal-checkout
==================

A modern, REST-first PayPal integration for Django: PayPal **Orders v2**
checkout, refunds and **verified webhooks**, with models, signals and admin.

.. note::

   **Version 0.1.0.** One-off payments are covered end to end: Orders v2
   create/authorize/capture, refunds and voids, verified webhooks, models that
   survive an interrupted call, signals, a reconciliation command and a read-only
   admin. Subscriptions, Vault and Card Fields are not implemented yet.

   It has not been run against live PayPal traffic, and the API may still change
   on minor versions before 1.0.

Why not ``django-paypal``?
--------------------------

The long-standing ``django-paypal`` package is built on PayPal **Payments
Standard with IPN/PDT** — the Classic stack. PayPal now recommends webhooks
for all new integrations, and IPN is not fired by newer payment products.
This library targets the current REST APIs instead:

* Orders v2 for checkout
* Payments v2 for captures and refunds
* Webhooks with signature verification (no IPN)
* Subscriptions v1 (not implemented yet)

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   installation
   configuration
   usage
   webhooks
   demo
   api_reference

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
