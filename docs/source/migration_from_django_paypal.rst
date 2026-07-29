Migrating from django-paypal
============================

The packages solve different protocols. ``django-paypal`` renders Payments
Standard forms and receives IPN/PDT messages; ``dj-paypal-checkout`` creates
Orders v2 server-side and receives verified REST webhooks. This is an
integration migration, not a package-name replacement.

Do not remove IPN first
-----------------------

Existing Payments Standard transactions can still produce delayed IPNs,
refunds or reversals. Keep the old IPN URL and handlers running while new
checkouts move to Orders v2. Only retire them after the business's refund and
dispute window for legacy transactions has passed.

Install the new application alongside the old one:

.. code-block:: python

   INSTALLED_APPS = [
       # "paypal.standard.ipn",  # retain during the transition
       "paypal_checkout",
   ]

   urlpatterns = [
       # path("paypal/ipn/", include("paypal.standard.ipn.urls")),
       path("paypal/rest/", include("paypal_checkout.urls")),
   ]

Run ``python manage.py migrate`` and configure ``PAYPAL`` as described in
:doc:`configuration`. Register ``/paypal/rest/webhook/`` in the PayPal
developer dashboard and store its id as ``PAYPAL["WEBHOOK_ID"]``.

Map the old concepts
--------------------

==============================================  ==============================================
``django-paypal``                               ``dj-paypal-checkout``
==============================================  ==============================================
``PayPalPaymentsForm``                          Project checkout UI + Orders v2 create endpoint
``notify_url`` and IPN endpoint                 Verified REST webhook endpoint
``valid_ipn_received``                          Payment/refund/subscription signals
``PayPalIPN`` row                               ``WebhookEvent`` plus typed local resource rows
``payment_status == "Completed"``               ``Capture.status == "COMPLETED"``
``txn_id``                                      Capture/refund PayPal ids
``invoice`` / ``custom``                        Host object through ``target`` and API metadata
``PAYPAL_TEST``                                 ``PAYPAL["LIVE"]`` (inverse meaning)
==============================================  ==============================================

The identifiers are not interchangeable. A legacy IPN ``txn_id`` is not an
Orders v2 order or capture id, so do not manufacture ``PayPalOrder`` rows from
old IPN history. Keep the legacy table read-only for historical audit.

Move checkout creation server-side
----------------------------------

Payments Standard form fields are browser-controlled, so old IPN handlers must
re-check receiver, amount and currency. Orders v2 changes that boundary: compute
the amount from the application's own object and call
:func:`paypal_checkout.orders.create_order` on the server.

.. code-block:: python

   from paypal_checkout import PayPalClient
   from paypal_checkout.orders import create_order

   def create_paypal_order(request, checkout):
       with PayPalClient() as client:
           order = create_order(
               client,
               amount=checkout.total,
               currency=checkout.currency,
               target=checkout,
           )
       return JsonResponse({"order_id": order.paypal_id})

The browser receives only ``order_id``. It must not submit the authoritative
amount. Capture through :func:`paypal_checkout.orders.capture_order`, again
looking up the local order rather than trusting arbitrary client data.

Move business effects to idempotent receivers
---------------------------------------------

Replace a ``valid_ipn_received`` handler with receivers for the outcome that
actually matters:

.. code-block:: python

   from django.dispatch import receiver
   from paypal_checkout.signals import payment_captured, payment_refunded

   @receiver(payment_captured)
   def mark_paid(sender, *, target, **kwargs):
       if target is not None and not target.paid:
           target.paid = True
           target.save(update_fields=["paid"])

The guard is required. The direct capture response and its webhook can both send
``payment_captured``, and PayPal can redeliver a webhook. Receivers must set
state idempotently, never increment counters blindly.

Do not copy the old IPN validation code into the new receiver. REST helpers
validate the local amount before creating an order; webhook signatures are
verified before handlers run; typed rows retain PayPal's payload for audit.
Application-specific fulfilment checks still belong in the receiver.

Run both paths, then cut over
-----------------------------

Use this sequence:

1. Deploy models, settings and the verified webhook endpoint without changing
   checkout.
2. Confirm sandbox webhook delivery and alert on unprocessed
   :class:`~paypal_checkout.models.WebhookEvent` rows.
3. Deploy the Orders v2 checkout for new transactions while retaining the IPN
   endpoint for legacy ones.
4. Compare application state, PayPal activity and local capture/refund rows.
5. Stop creating Payments Standard transactions.
6. After the legacy operational window, remove the old URL, signal handlers,
   application entry and dependency. Preserve historical IPN rows according to
   the application's retention policy.

Rollback during steps 1–4 means routing new checkout attempts back to the old
form; it does not mean deleting Orders v2 or webhook records already received.
