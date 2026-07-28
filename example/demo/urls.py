from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Register https://<your-host>/paypal/webhook/ in the PayPal dashboard and
    # put the id it returns in PAYPAL['WEBHOOK_ID'].
    path("paypal/", include("paypal_checkout.urls")),
    path("", include("shop.urls")),
]
