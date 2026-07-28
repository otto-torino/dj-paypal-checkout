Webhooks
========

Webhooks are the authoritative signal that money moved. A capture response tells
you what PayPal *said* at that moment; the webhook is what survives your process
dying halfway through.

Setup
-----

Mount the endpoint:

.. code-block:: python

   # urls.py
   path("paypal/", include("paypal_checkout.urls")),

Register the resulting ``/paypal/webhook/`` URL as a webhook in the `PayPal
dashboard <https://developer.paypal.com/dashboard/>`__, then put the id PayPal
gives you in settings:

.. code-block:: python

   PAYPAL = {..., "WEBHOOK_ID": os.environ["PAYPAL_WEBHOOK_ID"]}

The webhook id is not optional decoration: it is part of the signed message, so
it is what binds a delivery to *your* webhook rather than someone else's.

.. warning::

   Nothing may consume the request body before the view. Verification runs on
   the exact bytes PayPal signed, and any middleware that reads, re-encodes or
   rewrites the body will make every signature fail.

Verification
------------

PayPal signs with RSA-SHA256 over
``<transmission_id>|<transmission_time>|<webhook_id>|<crc32(raw_body)>``.

``WEBHOOK_VERIFY_MODE`` chooses how that is checked:

``offline`` (default)
   Verified locally against the certificate from ``PAYPAL-CERT-URL``. Needs the
   ``crypto`` extra (``pip install "dj-paypal-checkout[crypto]"``). The cert URL
   is validated as an HTTPS paypal.com host **before** it is fetched — without
   that check a forged header could point the verifier at an attacker's
   certificate and every forgery would pass. The certificate is cached for a day.

``api``
   Asks ``/v1/notifications/verify-webhook-signature``. Simpler, one extra call
   per webhook, and it needs the event re-serialised as JSON, so it inherits the
   fragility described above.

There is deliberately **no** "try offline, fall back to the API" mode. A
signature that fails must be rejected, not re-checked by something that might
say yes. If the offline path cannot run at all (no ``cryptography``, cert
unreachable) the request is refused rather than waved through.

What the endpoint answers, and why
----------------------------------

The status code is the contract with PayPal's retry machinery:

=========  ===========================================================
``400``    Not trustworthy: missing headers, bad signature, unreadable
           body, no event id. Nothing is stored, nothing is retried.
``200``    Stored **and** finished — including a duplicate delivery of
           an event already processed.
``500``    Stored but **not** finished: a handler raised. PayPal retries.
=========  ===========================================================

The ``500`` case is the interesting one. ``WebhookEvent.event_id`` is unique,
which is what stops double-processing — but a row that exists and was never
processed is *not* a duplicate to skip, it is unfinished work, so a retry is
allowed to pick it up. Answering ``200`` on a handler failure would drop a
payment confirmation for good.

This is what makes the race safe: if a webhook overtakes your own capture
response, the handler raises
:class:`~paypal_checkout.exceptions.PayPalWebhookNotReady`, the endpoint answers
``500``, and PayPal's retry a minute later finds the row and succeeds. Free
reconciliation.

Handled events
--------------

Out of the box: ``PAYMENT.CAPTURE.COMPLETED``, ``.DENIED``, ``.PENDING``,
``.REFUNDED``/``.REVERSED``, ``CHECKOUT.ORDER.APPROVED``/``.COMPLETED``, and
``PAYMENT.AUTHORIZATION.CREATED``/``.VOIDED``. They update the local row and
send the same :doc:`signals <usage>` the capture call sends.

An event nobody handles is stored and acknowledged — not an error.

An event about a capture or order this project does not know is ignored, which
matters if another integration shares the PayPal account.

Your own handlers
-----------------

.. code-block:: python

   from paypal_checkout.webhooks import register_handler

   @register_handler("BILLING.SUBSCRIPTION.CANCELLED")
   def on_cancelled(event):
       event.event_type      # "BILLING.SUBSCRIPTION.CANCELLED"
       event.resource        # the event's resource object
       event.payload         # the whole delivered event

Raising propagates to a ``500`` and a retry, so raise for work that genuinely
has not been done and return normally for anything you have decided to ignore.

Reconciling
-----------

.. code-block:: python

   from paypal_checkout.models import WebhookEvent

   WebhookEvent.objects.filter(processed_at__isnull=True)   # still unfinished

Each carries ``last_error``. A permanently failing handler will keep PayPal
retrying for as long as PayPal retries (days), which is visible rather than
silent — watch that queryset.
