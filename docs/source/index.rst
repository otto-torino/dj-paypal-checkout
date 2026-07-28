dj-paypal-checkout
==================

A modern, REST-first PayPal integration for Django: PayPal **Orders v2**
checkout, refunds and **verified webhooks**, with models, signals and admin.

.. warning::

   **In development — not released yet.** The package is a skeleton; the
   client, models and webhook handling are being implemented milestone by
   milestone (see ``PROGRESS.md`` in the repository). Nothing here is usable
   in production, and the API may change without notice until 0.1.0.

Why not ``django-paypal``?
--------------------------

The long-standing ``django-paypal`` package is built on PayPal **Payments
Standard with IPN/PDT** — the Classic stack. PayPal now recommends webhooks
for all new integrations, and IPN is not fired by newer payment products.
This library targets the current REST APIs instead:

* Orders v2 for checkout
* Payments v2 for captures and refunds
* Webhooks with signature verification (no IPN)
* Subscriptions v1 (planned, after 0.1.0)

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   installation
   configuration
   usage
   api_reference

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
