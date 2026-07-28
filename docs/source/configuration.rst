Configuration
=============

All configuration lives in a single ``PAYPAL`` dict in your settings. The
library reads it exclusively through :mod:`paypal_checkout.config`; no other
module touches ``django.conf.settings``.

.. code-block:: python

   PAYPAL = {
       # Required. Credentials from the PayPal developer dashboard.
       "CLIENT_ID": os.environ["PAYPAL_CLIENT_ID"],
       "CLIENT_SECRET": os.environ["PAYPAL_CLIENT_SECRET"],

       # False -> api-m.sandbox.paypal.com, True -> api-m.paypal.com
       "LIVE": False,

       # Id of the webhook registered for your endpoint. Required to verify
       # incoming webhook signatures.
       "WEBHOOK_ID": os.environ.get("PAYPAL_WEBHOOK_ID", ""),

       # Default currency for created orders (ISO-4217).
       "CURRENCY": "EUR",
   }

Never commit credentials. Read them from the environment.

Reference
---------

==================== ============== ===========================================
Key                  Default        Meaning
==================== ============== ===========================================
CLIENT_ID            *(required)*   PayPal REST app client id.
CLIENT_SECRET        *(required)*   PayPal REST app secret.
LIVE                 ``False``      ``True`` targets the live API, not sandbox.
WEBHOOK_ID           ``""``         Registered webhook id, to verify signatures.
CURRENCY             ``"EUR"``      Default currency, 3-letter ISO-4217 code.
TIMEOUT              ``30.0``       Per-request timeout in seconds.
MAX_RETRIES          ``2``          Extra attempts for requests safe to repeat.
RETRY_BACKOFF        ``0.5``        Base of the exponential backoff, in seconds.
STRICT_IDEMPOTENCY   ``True``       Refuse a write with no ``request_id``
                                    instead of only warning (see below).
WEBHOOK_VERIFY_MODE  ``"offline"``  Verify signatures locally, or ``"api"`` to
                                    ask PayPal. See :doc:`webhooks`.
CACHE_ALIAS          ``"default"``  Django cache alias storing access tokens.
TOKEN_LEEWAY         ``300``        Refresh the token this long before expiry.
==================== ============== ===========================================

Misconfiguration raises
:class:`~paypal_checkout.exceptions.PayPalConfigurationError`, which is also an
``ImproperlyConfigured`` — so it surfaces like any other Django settings
problem, at the point of first use. Unknown keys are rejected rather than
silently ignored, so a typo cannot leave you on a default you did not intend.

Environments
------------

``LIVE`` is the *only* thing that decides which API is called. There is no
code path that can pick the wrong environment:

.. code-block:: python

   >>> get_config().base_url
   'https://api-m.sandbox.paypal.com'
   >>> get_config(live=True).base_url
   'https://api-m.paypal.com'

Strict idempotency
------------------

A mutating request sent without ``request_id`` cannot be retried safely, so by
default it is **refused**: the client raises
:class:`~paypal_checkout.exceptions.PayPalIdempotencyError` *before* anything
reaches PayPal, not even the token request.

You should not have to think about this. Every helper in
:mod:`paypal_checkout.orders` and :mod:`paypal_checkout.payments` supplies a
persisted key, and operations that genuinely need none — like verifying a webhook
signature — declare
:attr:`Idempotency.NOT_APPLICABLE <paypal_checkout.client.Idempotency>`. Strict
mode therefore only trips when a call site using the raw client has not decided
how its operation is identified. That per-operation policy is also what keeps it
from becoming a false-positive factory in production; see :doc:`usage`.

Turning it off downgrades the refusal to a structured warning:

.. code-block:: python

   PAYPAL = {..., "STRICT_IDEMPOTENCY": False}   # temporary, and alert on it

Treat that as a migration aid, not a resting state, and keep the value **the same
in every environment**. Strict in CI but lenient in production would mean a green
test suite validating a guarantee production does not have — that divergence is
itself the bug.

Both the refusal and the warning are silent when ``MAX_RETRIES`` is ``0`` — with
retries disabled there is no retry for a missing key to make unsafe.

Observability
-------------

A plain warning is easy to filter out and ignore, so the record is structured:
``paypal_method``, ``paypal_endpoint`` and ``paypal_issue`` are attached to the
``LogRecord``, which makes it usable as a metric rather than as prose.

``paypal_endpoint`` is a templated, id-free path
(``/v2/checkout/orders/{id}/capture``), so it has low cardinality and carries no
per-transaction identifier. **No request body, credentials, headers or query
string are ever logged.**

.. code-block:: python

   import logging

   class IdempotencyMetric(logging.Filter):
       def filter(self, record):
           issue = getattr(record, "paypal_issue", None)
           if issue:
               statsd.increment(
                   f"paypal.{issue}",
                   tags=[f"method:{record.paypal_method}",
                         f"endpoint:{record.paypal_endpoint}"],
               )
           return True

   LOGGING = {
       "version": 1,
       "filters": {"paypal_metric": {"()": IdempotencyMetric}},
       "loggers": {
           "paypal_checkout.client": {"level": "WARNING", "filters": ["paypal_metric"]},
       },
   }

Multiple accounts
-----------------

:func:`~paypal_checkout.config.get_config` accepts overrides using the
lower-case field names, which is how you talk to more than one PayPal account
from the same project:

.. code-block:: python

   from paypal_checkout import PayPalClient, get_config

   marketplace = get_config(client_id="...", client_secret="...")
   with PayPalClient(marketplace) as client:
       ...

Access tokens
-------------

Tokens are cached in the Django cache (``CACHE_ALIAS``) under a key derived
from a hash of the credentials plus the environment. Consequences worth
knowing:

* sandbox and live tokens can never be confused;
* rotating the secret invalidates the cached token immediately;
* the raw client id never appears in a cache key;
* a token whose remaining life is below ``TOKEN_LEEWAY`` is not cached at all.
