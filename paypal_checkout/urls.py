"""URLs to include in your project.

.. code-block:: python

   path("paypal/", include("paypal_checkout.urls")),

The resulting ``/paypal/webhook/`` is the URL to register as a webhook in the
PayPal dashboard; put the id it gives you in ``PAYPAL['WEBHOOK_ID']``.
"""

from django.urls import path

from .webhooks.views import ProcessWebhookView

app_name = "paypal_checkout"

urlpatterns = [
    path("webhook/", ProcessWebhookView.as_view(), name="webhook"),
]
