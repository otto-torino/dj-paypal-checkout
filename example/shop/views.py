"""The two endpoints a PayPal checkout needs, and nothing more.

Note what is *not* here: the amount never arrives from the browser. The client
asks "start a payment for order X" and the server decides what X costs.
"""

from decimal import Decimal

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from paypal_checkout import PayPalClient
from paypal_checkout.models import PayPalOrder
from paypal_checkout.orders import capture_order, create_order

from .models import Order

#: A stand-in for a real cart.
CART_TOTAL = Decimal("12.34")


def checkout(request):
    order, _ = Order.objects.get_or_create(
        reference="DEMO-1", defaults={"total": CART_TOTAL, "currency": "EUR"}
    )
    return render(
        request,
        "shop/checkout.html",
        {"order": order, "paypal_orders": PayPalOrder.objects.for_target(order)},
    )


@require_POST
def create(request):
    """Start a PayPal order for the shop order and hand back only its id."""
    order = get_object_or_404(Order, reference="DEMO-1")
    with PayPalClient() as client:
        paypal_order = create_order(
            client,
            amount=order.total,      # from our own record, never from the request
            currency=order.currency,
            target=order,
        )
    return JsonResponse({"orderId": paypal_order.paypal_id})


@require_POST
def capture(request, paypal_id):
    """Capture the approved order; the signal marks the shop order paid."""
    paypal_order = get_object_or_404(PayPalOrder, paypal_id=paypal_id)
    with PayPalClient() as client:
        capture = capture_order(client, paypal_order)
    return JsonResponse(
        {
            "status": capture.status,
            "captureId": capture.paypal_id,
            "successful": capture.is_successful,
        }
    )
