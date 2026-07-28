from decimal import Decimal

from django.db import models


class Order(models.Model):
    """The shop's own order — the thing the buyer is actually paying for.

    dj-paypal-checkout never owns this: a ``PayPalOrder`` points at it through a
    generic FK, and this model stays entirely yours.
    """

    reference = models.CharField(max_length=32, unique=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, default="EUR")
    paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.reference} ({'paid' if self.paid else 'unpaid'})"
