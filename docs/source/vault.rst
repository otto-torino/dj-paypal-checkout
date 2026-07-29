Vault
=====

Payment Method Tokens v3 saves a payment method for later use. The server-side
flow uses two different resources:

* a temporary :class:`~paypal_checkout.models.SetupToken`, populated and
  approved through PayPal-hosted UI;
* a permanent :class:`~paypal_checkout.models.PaymentToken`, created by
  exchanging the approved setup token.

Account requirements
--------------------

Vault is not enabled for every merchant automatically. Card saving additionally
requires approval for Advanced Credit and Debit Card Payments. Confirm
eligibility in the PayPal developer dashboard before building the browser UI.
An API ``NOT_ENABLED_TO_VAULT_PAYMENT_SOURCE`` response means the account lacks
the required permission; it is not a retryable application error.

The authoritative references are PayPal's `Payment Method Tokens v3 API
<https://developer.paypal.com/docs/api/payment-tokens/v3/>`__ and `save cards
for later guide
<https://developer.paypal.com/docs/checkout/save-payment-methods/purchase-later/js-sdk/cards/>`__.

Safe setup-token flow
---------------------

Create an empty card setup token on the server:

.. code-block:: python

   from paypal_checkout import PayPalClient
   from paypal_checkout.vault import create_setup_token

   with PayPalClient() as client:
       setup_token = create_setup_token(
           client,
           payment_source={"card": {}},
           customer={"merchant_customer_id": str(request.user.pk)},
           target=request.user,
       )

   return JsonResponse({"setup_token": setup_token.paypal_id})

Pass only that public setup-token id to PayPal's Card Fields component. Card
Fields sends the PAN and security code directly to PayPal. They must never pass
through a Django form, JSON endpoint, log or model.

``create_setup_token`` enforces that boundary: a request containing ``number``,
``security_code``, ``card_number``, ``cvv`` or ``cvv2`` is refused before any
row or HTTP request is created. Stored PayPal responses are sanitized again as
defence in depth.

After the browser has populated and, when required, approved the setup token,
refresh and exchange it:

.. code-block:: python

   from paypal_checkout.vault import (
       create_payment_token,
       refresh_setup_token,
   )

   with PayPalClient() as client:
       refresh_setup_token(client, setup_token)
       payment_token = create_payment_token(
           client,
           setup_token=setup_token,
       )

   payment_token.paypal_id   # the reusable vault id
   payment_token.customer_id # PayPal-generated customer id

Both create operations persist their local row and ``PayPal-Request-Id`` before
calling PayPal. An interrupted request therefore remains visible, and retrying
the same operation can reuse the same key.

Read, list and delete
---------------------

.. code-block:: python

   from paypal_checkout.vault import (
       delete_payment_token,
       list_payment_tokens,
       refresh_payment_token,
   )

   with PayPalClient() as client:
       methods = list_payment_tokens(client, payment_token.customer_id)
       refresh_payment_token(client, payment_token)
       delete_payment_token(client, payment_token)

Deletion keeps the local row as a ``DELETED`` tombstone: the token itself is no
longer usable, but the audit trail remains.

Webhooks
--------

Subscribe the configured PayPal webhook to:

* ``VAULT.PAYMENT-TOKEN.CREATED``;
* ``VAULT.PAYMENT-TOKEN.DELETION-INITIATED``;
* ``VAULT.PAYMENT-TOKEN.DELETED``.

Verified events update or adopt the local payment-token row. Created and deleted
events send :data:`~paypal_checkout.signals.payment_token_created` and
:data:`~paypal_checkout.signals.payment_token_deleted`. As with payment and
subscription signals, receivers must be idempotent because a direct API response
and a webhook can describe the same outcome.

If a created event overtakes the payment-token API response, the handler leaves
the pending row untouched and asks PayPal to retry. Once the response has stored
the token id, the retry updates that exact row; it never guesses from customer
metadata or creates a conflicting duplicate.

Environment isolation
---------------------

Every setup and payment token records ``live``. A sandbox client is refused when
given a live token and vice versa. The same PayPal id must never be assumed to
refer to an interchangeable resource across environments.
