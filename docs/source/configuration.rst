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

================== ============== =============================================
Key                Default        Meaning
================== ============== =============================================
CLIENT_ID          *(required)*   PayPal REST app client id.
CLIENT_SECRET      *(required)*   PayPal REST app secret.
LIVE               ``False``      ``True`` targets the live API, not sandbox.
WEBHOOK_ID         ``""``         Registered webhook id, to verify signatures.
CURRENCY           ``"EUR"``      Default currency, 3-letter ISO-4217 code.
TIMEOUT            ``30.0``       Per-request timeout in seconds.
MAX_RETRIES        ``2``          Extra attempts for requests safe to repeat.
RETRY_BACKOFF      ``0.5``        Base of the exponential backoff, in seconds.
STRICT_IDEMPOTENCY ``False``      Raise instead of warning on a write with no
                                  ``request_id`` (see below).
CACHE_ALIAS        ``"default"``  Django cache alias storing access tokens.
TOKEN_LEEWAY       ``300``        Refresh the token this long before expiry.
================== ============== =============================================

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

A mutating request sent without ``request_id`` cannot be retried safely, so the
client logs a warning on the ``paypal_checkout.client`` logger and carries on
with a single attempt. Turning ``STRICT_IDEMPOTENCY`` on promotes that warning
to a :class:`~paypal_checkout.exceptions.PayPalIdempotencyError`, raised
*before* anything reaches PayPal:

.. code-block:: python

   # settings/dev.py, settings/ci.py, settings/staging.py
   PAYPAL = {**PAYPAL, "STRICT_IDEMPOTENCY": True}

Use it everywhere except production, so a call site that forgot its
``request_id`` is caught by a test rather than by a PayPal outage. It is off by
default on purpose: refusing to even attempt a capture is worse than attempting
it once, and a single attempt is always safe.

Both the warning and the error are silent when ``MAX_RETRIES`` is ``0`` — with
retries disabled there is no retry for a missing key to make unsafe.

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
