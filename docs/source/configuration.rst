Configuration
=============

.. warning::

   Planned interface — not implemented yet. Names may still change before
   0.1.0.

All configuration lives in a single ``PAYPAL`` dict in your settings. The
library reads it exclusively through ``paypal_checkout.config``; no other
module touches ``django.conf.settings``.

.. code-block:: python

   PAYPAL = {
       # Credentials from the PayPal developer dashboard.
       "CLIENT_ID": os.environ["PAYPAL_CLIENT_ID"],
       "CLIENT_SECRET": os.environ["PAYPAL_CLIENT_SECRET"],

       # False -> api-m.sandbox.paypal.com, True -> api-m.paypal.com
       "LIVE": False,

       # Id of the webhook registered for your endpoint. Required to verify
       # incoming webhook signatures.
       "WEBHOOK_ID": os.environ["PAYPAL_WEBHOOK_ID"],

       # Default currency for created orders.
       "CURRENCY": "EUR",
   }

Never commit credentials. Read them from the environment.
