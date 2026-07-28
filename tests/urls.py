"""URLconf for the test project."""

from django.urls import include, path

urlpatterns = [
    path("paypal/", include("paypal_checkout.urls")),
]
