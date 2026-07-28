from django.apps import AppConfig


class PayPalCheckoutConfig(AppConfig):
    name = "paypal_checkout"
    label = "paypal_checkout"
    verbose_name = "PayPal Checkout"
    default_auto_field = "django.db.models.BigAutoField"
