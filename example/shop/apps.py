from django.apps import AppConfig


class ShopConfig(AppConfig):
    name = "shop"

    def ready(self):
        # Importing connects the signal receivers.
        from . import receivers  # noqa: F401
