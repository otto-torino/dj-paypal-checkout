Subscriptions
=============

Subscriptions use PayPal's Products, Plans and Subscriptions v1 APIs. The
server-side flow is:

1. create a catalog product;
2. create and activate a billing plan for that product;
3. create a subscription and send the buyer to its approval URL;
4. use verified webhooks as the authoritative source for activation, later
   payments and lifecycle changes.

The helpers are synchronous, like the Orders helpers. The lower-level HTTP
client remains available in synchronous and asynchronous forms.

Products and plans
------------------

A product describes what is sold. A plan describes how and when it is billed:

.. code-block:: python

   from paypal_checkout import PayPalClient
   from paypal_checkout.subscriptions import (
       activate_plan,
       create_plan,
       create_product,
   )

   with PayPalClient() as client:
       product = create_product(
           client,
           name="Otto Pro",
           description="Monthly access to Otto Pro",
       )

       plan = create_plan(
           client,
           product=product,
           name="Otto Pro monthly",
           billing_cycles=[
               {
                   "frequency": {
                       "interval_unit": "MONTH",
                       "interval_count": 1,
                   },
                   "tenure_type": "REGULAR",
                   "sequence": 1,
                   "total_cycles": 0,
                   "pricing_scheme": {
                       "fixed_price": {
                           "value": "9.99",
                           "currency_code": "EUR",
                       }
                   },
               }
           ],
           payment_preferences={
               "auto_bill_outstanding": True,
               "payment_failure_threshold": 3,
           },
       )
       activate_plan(client, plan)

``billing_cycles`` and ``payment_preferences`` are passed through in PayPal's
native shape. This avoids a local schema becoming less expressive than the API,
especially for trial periods, finite plans and tiered pricing.

You may pass an existing PayPal id as ``product_id=`` instead of a local
:class:`~paypal_checkout.models.Product`. Local rows add environment checks,
stored API responses and visibility in the admin.

Creating and approving a subscription
-------------------------------------

Only an active local plan can accept new subscriptions:

.. code-block:: python

   from paypal_checkout.subscriptions import create_subscription

   subscription = create_subscription(
       client,
       plan=plan,
       quantity=1,
       target=customer_membership,
       custom_id=str(customer_membership.pk),
       application_context={
           "return_url": "https://example.com/paypal/approved/",
           "cancel_url": "https://example.com/paypal/cancelled/",
       },
   )

   return redirect(subscription.approve_url())

``target`` is optional. When supplied, it is stored as a generic relation to
your own model, like the target on a PayPal order. Signal receivers then receive
that object as ``target``.

The create response is normally ``APPROVAL_PENDING``. Redirecting the buyer is
not proof that billing started; wait for
``BILLING.SUBSCRIPTION.ACTIVATED``. A return URL is a user-experience route, not
an authoritative payment notification.

Lifecycle operations
--------------------

.. code-block:: python

   from paypal_checkout.subscriptions import (
       activate_subscription,
       cancel_subscription,
       revise_subscription,
       suspend_subscription,
   )

   suspend_subscription(client, subscription, reason="Customer requested a pause")
   activate_subscription(client, subscription, reason="Customer resumed")

   revised = revise_subscription(client, subscription, quantity=3)
   if revised.approve_url():
       # A pricing change may require buyer approval.
       ...

   cancel_subscription(client, subscription, reason="Customer closed the account")

Cancellation is final. Suspending pauses billing and can later be reversed by
activation. ``revise_subscription`` can change ``quantity`` or move the
subscription to another active plan; PayPal may require the buyer to approve the
revision.

Creates and transitions intentionally use different idempotency policies.
Product, plan and subscription creates persist a stable
``PayPal-Request-Id`` before calling PayPal, so recovery reuses the same
operation. Activate, suspend, cancel and revise are repeatable lifecycle
transitions and do not use a fixed key: a key fixed for the lifetime of the
subscription could replay an obsolete transition after its state changed.
Those operations are not retried automatically after a server or connection
error; refresh the subscription and decide whether to call the transition
again.

Webhooks and recurring payments
-------------------------------

Register these events on the same verified webhook endpoint used for checkout:

* ``BILLING.SUBSCRIPTION.CREATED``
* ``BILLING.SUBSCRIPTION.UPDATED``
* ``BILLING.SUBSCRIPTION.ACTIVATED``
* ``BILLING.SUBSCRIPTION.RE-ACTIVATED``
* ``BILLING.SUBSCRIPTION.SUSPENDED``
* ``BILLING.SUBSCRIPTION.CANCELLED``
* ``BILLING.SUBSCRIPTION.EXPIRED``
* ``BILLING.SUBSCRIPTION.PAYMENT.FAILED``
* ``PAYMENT.SALE.COMPLETED``

Lifecycle events refresh the local
:class:`~paypal_checkout.models.Subscription`. A completed sale creates one
:class:`~paypal_checkout.models.SubscriptionPayment`, unique by PayPal sale id,
so a redelivery cannot count the same charge twice.

.. code-block:: python

   subscription.is_active
   subscription.is_billable
   subscription.paid_amount
   subscription.payments.all()

Unknown subscription ids are ignored and acknowledged. A PayPal account can be
shared by several integrations, and subscription events do not carry an
enclosing local order that would let this library safely claim an unknown one.

Signals
-------

The built-in handlers emit:

* ``subscription_activated``
* ``subscription_suspended``
* ``subscription_cancelled``
* ``subscription_expired``
* ``subscription_payment_completed``
* ``subscription_payment_failed``

Lifecycle signals carry ``subscription``, ``target`` and ``reason``. Payment
completion also carries ``payment`` and ``created``; payment failure carries the
raw resource as ``raw``.

As with checkout signals, receivers must be idempotent. A transition called
through a helper and its later webhook can both describe the same outcome.

.. code-block:: python

   from django.dispatch import receiver
   from paypal_checkout import subscription_activated

   @receiver(subscription_activated)
   def enable_membership(sender, subscription, target, **kwargs):
       if target is not None and not target.is_enabled:
           target.is_enabled = True
           target.save(update_fields=["is_enabled"])

Admin and recovery
------------------

Products, plans, subscriptions and recurring payments are visible in the
read-only Django admin. Rows created locally exist before the corresponding
PayPal create call, including their idempotency key, so an interrupted operation
remains visible.

The current ``paypal_sync`` command reconciles Orders, not subscriptions. For an
unconfirmed subscription create or an uncertain lifecycle transition, inspect
the local row and PayPal account, then use ``refresh_subscription`` once its
PayPal id is known.
