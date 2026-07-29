"""Subscriptions v1: catalog products, billing plans, subscriptions.

Same discipline as :mod:`paypal_checkout.orders` — the row and its persisted key
exist before the create call, and every write declares a policy.

**Idempotency here has a third shape, and the reason matters.** Creates carry a
persisted key like everywhere else. The lifecycle transitions — activate,
suspend, cancel, revise — carry **no key at all** and are declared
:attr:`~paypal_checkout.client.Idempotency.OPTIONAL`, because:

* they are legitimately repeatable (suspend → activate → suspend is a normal
  life), so a key fixed per subscription would make PayPal replay the first
  transition for ever — the same trap as a fixed capture key;
* giving each transition its own row purely to hold a key would be bookkeeping
  with no payoff: nothing here moves money, and PayPal refuses an invalid
  transition on state grounds (a second cancel of a cancelled subscription is a
  422, not a second charge).

So ``OPTIONAL`` is not laziness: it says "a key would help, its absence is not a
defect", which is exactly the situation. The consequence is that a transition is
not retried on a 5xx — call it again yourself, it is safe.

Synchronous only, like the order helpers.
"""

import logging

from .client import Idempotency
from .exceptions import PayPalError
from .models import Plan, Product, Subscription
from .money import amount_payload
from .signals import (
    subscription_activated,
    subscription_cancelled,
    subscription_suspended,
)

__all__ = [
    "PRODUCTS_PATH",
    "PLANS_PATH",
    "SUBSCRIPTIONS_PATH",
    "create_product",
    "fetch_product",
    "refresh_product",
    "create_plan",
    "fetch_plan",
    "refresh_plan",
    "activate_plan",
    "deactivate_plan",
    "create_subscription",
    "fetch_subscription",
    "refresh_subscription",
    "activate_subscription",
    "suspend_subscription",
    "cancel_subscription",
    "revise_subscription",
]

logger = logging.getLogger(__name__)

PRODUCTS_PATH = "/v1/catalogs/products"
PLANS_PATH = "/v1/billing/plans"
SUBSCRIPTIONS_PATH = "/v1/billing/subscriptions"


def _require_paypal_id(instance, kind):
    if not instance.paypal_id:
        raise PayPalError(
            f"{instance!r} has no PayPal id: this {kind} was started locally but "
            "PayPal never confirmed it. Reconcile it before continuing."
        )
    return instance.paypal_id


def _require_same_environment(client, instance, kind):
    if instance.live != client.config.live:
        client_environment = "live" if client.config.live else "sandbox"
        instance_environment = "live" if instance.live else "sandbox"
        raise PayPalError(
            f"{kind} {instance!r} belongs to {instance_environment}, but the "
            f"client is configured for {client_environment}."
        )


def _require_positive_quantity(quantity):
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise PayPalError("quantity must be a positive integer.")


# -- catalog products -------------------------------------------------------


def create_product(client, *, name, product_type=None, description=None, **extra):
    """Create a catalog product and return its :class:`Product` row."""
    product = Product.objects.start(
        name=name,
        product_type=product_type,
        description=description or "",
        live=client.config.live,
    )
    body = {"name": name, "type": product.product_type}
    if description:
        body["description"] = description
    body.update(extra)

    payload = client.post(
        PRODUCTS_PATH,
        json=body,
        request_id=product.request_id,
        idempotency=Idempotency.REQUIRED,
    )
    return product.update_from_payload(payload)


def fetch_product(client, paypal_id):
    """Read a product straight from PayPal, with no local row involved."""
    return client.get(f"{PRODUCTS_PATH}/{paypal_id}")


def refresh_product(client, product):
    payload = client.get(f"{PRODUCTS_PATH}/{_require_paypal_id(product, 'product')}")
    return product.update_from_payload(payload)


# -- billing plans ----------------------------------------------------------


def create_plan(
    client,
    *,
    name,
    billing_cycles,
    product=None,
    product_id=None,
    payment_preferences=None,
    **extra
):
    """Create a billing plan and return its :class:`Plan` row.

    ``billing_cycles`` is passed through as PayPal defines it — the shape is rich
    (fixed vs infinite, trial cycles, tiered pricing) and wrapping it would only
    get in the way. Pass ``product`` (a local row) or ``product_id``.
    """
    if product is not None and product_id is not None:
        raise PayPalError("pass product or product_id, not both.")
    if product is not None:
        _require_same_environment(client, product, "product")
        product_paypal_id = _require_paypal_id(product, "product")
    else:
        product_paypal_id = product_id
    if not product_paypal_id:
        raise PayPalError("a plan needs a product: pass product= or product_id=.")

    plan = Plan.objects.start(
        name=name,
        product=product,
        product_paypal_id=product_paypal_id,
        live=client.config.live,
    )
    body = {
        "product_id": product_paypal_id,
        "name": name,
        "billing_cycles": billing_cycles,
    }
    if payment_preferences is not None:
        body["payment_preferences"] = payment_preferences
    body.update(extra)

    payload = client.post(
        PLANS_PATH,
        json=body,
        request_id=plan.request_id,
        idempotency=Idempotency.REQUIRED,
    )
    return plan.update_from_payload(payload)


def fetch_plan(client, paypal_id):
    return client.get(f"{PLANS_PATH}/{paypal_id}")


def refresh_plan(client, plan):
    _require_same_environment(client, plan, "plan")
    payload = client.get(f"{PLANS_PATH}/{_require_paypal_id(plan, 'plan')}")
    return plan.update_from_payload(payload)


def activate_plan(client, plan):
    """Activate a plan so subscriptions can be created against it."""
    return _plan_transition(client, plan, "activate", Plan.Status.ACTIVE)


def deactivate_plan(client, plan):
    """Deactivate a plan. Existing subscriptions keep billing."""
    return _plan_transition(client, plan, "deactivate", Plan.Status.INACTIVE)


def _plan_transition(client, plan, action, resulting_status):
    _require_same_environment(client, plan, "plan")
    paypal_id = _require_paypal_id(plan, "plan")
    payload = client.post(
        f"{PLANS_PATH}/{paypal_id}/{action}",
        idempotency=Idempotency.OPTIONAL,
    )
    if payload:
        return plan.update_from_payload(payload)
    # PayPal answers 204 with no body, so the new state is known but not returned.
    plan.status = resulting_status
    plan.save(update_fields=["status", "updated_at"])
    return plan


# -- subscriptions ----------------------------------------------------------


def create_subscription(
    client,
    *,
    plan=None,
    plan_id=None,
    quantity=1,
    target=None,
    custom_id=None,
    subscriber=None,
    application_context=None,
    shipping_amount=None,
    start_time=None,
    **extra
):
    """Create a subscription and return its :class:`Subscription` row.

    The buyer still has to approve it: the row comes back in
    ``APPROVAL_PENDING``, and :meth:`Subscription.approve_url` is the link to send
    them to. It becomes ``ACTIVE`` when PayPal says so — through the
    ``BILLING.SUBSCRIPTION.ACTIVATED`` webhook, not through this call.
    """
    if plan is not None and plan_id is not None:
        raise PayPalError("pass plan or plan_id, not both.")
    _require_positive_quantity(quantity)
    if plan is not None:
        _require_same_environment(client, plan, "plan")
        plan_paypal_id = _require_paypal_id(plan, "plan")
    else:
        plan_paypal_id = plan_id
    if not plan_paypal_id:
        raise PayPalError("a subscription needs a plan: pass plan= or plan_id=.")
    if plan is not None and not plan.accepts_subscriptions:
        raise PayPalError(
            f"plan {plan_paypal_id} is {plan.status}, not ACTIVE: PayPal will refuse "
            "subscriptions against it. Call activate_plan() first."
        )

    subscription = Subscription.objects.start(
        plan=plan,
        plan_paypal_id=plan_paypal_id,
        quantity=quantity,
        target=target,
        custom_id=custom_id or "",
        live=client.config.live,
    )

    body = {"plan_id": plan_paypal_id, "quantity": str(quantity)}
    if custom_id:
        body["custom_id"] = custom_id
    if subscriber:
        body["subscriber"] = subscriber
    if application_context:
        body["application_context"] = application_context
    if shipping_amount is not None:
        body["shipping_amount"] = amount_payload(
            shipping_amount, client.config.currency
        )
    if start_time:
        body["start_time"] = start_time
    body.update(extra)

    payload = client.post(
        SUBSCRIPTIONS_PATH,
        json=body,
        request_id=subscription.request_id,
        idempotency=Idempotency.REQUIRED,
    )
    return subscription.update_from_payload(payload)


def fetch_subscription(client, paypal_id):
    return client.get(f"{SUBSCRIPTIONS_PATH}/{paypal_id}")


def refresh_subscription(client, subscription):
    _require_same_environment(client, subscription, "subscription")
    paypal_id = _require_paypal_id(subscription, "subscription")
    payload = client.get(f"{SUBSCRIPTIONS_PATH}/{paypal_id}")
    return subscription.update_from_payload(payload)


def activate_subscription(client, subscription, *, reason="Reactivated by the merchant"):
    """Resume a suspended subscription."""
    return _subscription_transition(
        client, subscription, "activate", reason,
        Subscription.Status.ACTIVE, subscription_activated,
    )


def suspend_subscription(client, subscription, *, reason="Suspended by the merchant"):
    """Pause billing without ending the subscription."""
    return _subscription_transition(
        client, subscription, "suspend", reason,
        Subscription.Status.SUSPENDED, subscription_suspended,
    )


def cancel_subscription(client, subscription, *, reason="Cancelled by the merchant"):
    """End a subscription. This cannot be undone — create a new one instead."""
    return _subscription_transition(
        client, subscription, "cancel", reason,
        Subscription.Status.CANCELLED, subscription_cancelled,
    )


def _subscription_transition(client, subscription, action, reason, status, signal):
    _require_same_environment(client, subscription, "subscription")
    paypal_id = _require_paypal_id(subscription, "subscription")
    client.post(
        f"{SUBSCRIPTIONS_PATH}/{paypal_id}/{action}",
        json={"reason": reason},
        idempotency=Idempotency.OPTIONAL,
    )
    # These answer 204 with no body: the new state is implied by success.
    subscription.status = status
    subscription.save(update_fields=["status", "updated_at"])
    signal.send(
        sender=Subscription,
        subscription=subscription,
        target=subscription.target,
        reason=reason,
    )
    return subscription


def revise_subscription(client, subscription, *, plan=None, plan_id=None,
                        quantity=None, **extra):
    """Change the plan or the quantity of an existing subscription.

    PayPal answers with the revised subscription. A change in what the buyer pays
    needs their approval again, so check
    :meth:`Subscription.approve_url` on the result.
    """
    _require_same_environment(client, subscription, "subscription")
    paypal_id = _require_paypal_id(subscription, "subscription")
    if plan is not None and plan_id is not None:
        raise PayPalError("pass plan or plan_id, not both.")
    body = dict(extra)
    if plan is not None:
        _require_same_environment(client, plan, "plan")
        if not plan.accepts_subscriptions:
            raise PayPalError(
                f"plan {plan.paypal_id or plan.pk} is {plan.status}, not ACTIVE."
            )
        new_plan_id = _require_paypal_id(plan, "plan")
    else:
        new_plan_id = plan_id
    if new_plan_id:
        body["plan_id"] = new_plan_id
    if quantity is not None:
        _require_positive_quantity(quantity)
        body["quantity"] = str(quantity)
    if not body:
        raise PayPalError("revise_subscription needs a plan or a quantity to change.")

    payload = client.post(
        f"{SUBSCRIPTIONS_PATH}/{paypal_id}/revise",
        json=body,
        idempotency=Idempotency.OPTIONAL,
    )
    subscription.update_from_payload(payload)
    if plan is not None:
        subscription.plan = plan
        subscription.save(update_fields=["plan", "updated_at"])
    return subscription
