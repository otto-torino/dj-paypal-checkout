from decimal import Decimal

from django.db import models


class ShopOrder(models.Model):
    """Stand-in for a host project's own order.

    From M2 on, this is the object a `PayPalOrder` points at through its
    generic FK — it exists so tests exercise the real linking mechanism
    rather than a fake one.
    """

    reference = models.CharField(max_length=32, unique=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=3, default="EUR")
    paid = models.BooleanField(default=False)

    def __str__(self):
        return self.reference
