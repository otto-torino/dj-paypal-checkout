from django.urls import path

from . import views

urlpatterns = [
    path("", views.checkout, name="checkout"),
    path("paypal/create/", views.create, name="paypal-create"),
    path("paypal/<str:paypal_id>/capture/", views.capture, name="paypal-capture"),
]
