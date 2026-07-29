"""Catalog products, billing plans and subscriptions."""

import json
from datetime import timezone as dt_timezone
from decimal import Decimal

import httpx
from django.test import TestCase

from paypal_checkout.exceptions import PayPalError, PayPalIdempotencyError
from paypal_checkout.models import (
    Plan,
    Product,
    Subscription,
    SubscriptionPayment,
    plan_request_id,
    product_request_id,
    subscription_request_id,
)
from paypal_checkout.subscriptions import (
    PLANS_PATH,
    PRODUCTS_PATH,
    SUBSCRIPTIONS_PATH,
    activate_plan,
    activate_subscription,
    cancel_subscription,
    create_plan,
    create_product,
    create_subscription,
    deactivate_plan,
    fetch_plan,
    fetch_product,
    fetch_subscription,
    refresh_plan,
    refresh_product,
    refresh_subscription,
    revise_subscription,
    suspend_subscription,
)
from paypal_checkout.signals import (
    subscription_activated,
    subscription_cancelled,
    subscription_suspended,
)

from .support import catch_signal, make_config
from .test_app.models import ShopOrder
from .test_orders import ClientMixin

PRODUCT_ID = "PROD-XYAB12ABSB7868434"
PLAN_ID = "P-5ML4271244454362WXNWU5NQ"
SUB_ID = "I-BW452GLLEP1G"

BILLING_CYCLES = [
    {
        "frequency": {"interval_unit": "MONTH", "interval_count": 1},
        "tenure_type": "REGULAR",
        "sequence": 1,
        "total_cycles": 0,
        "pricing_scheme": {"fixed_price": {"value": "9.99", "currency_code": "EUR"}},
    }
]


def product_response(paypal_id=PRODUCT_ID, name="Membership"):
    return {"id": paypal_id, "name": name, "type": "SERVICE"}


def plan_response(status="ACTIVE", paypal_id=PLAN_ID, product_id=PRODUCT_ID):
    return {
        "id": paypal_id,
        "product_id": product_id,
        "name": "Monthly",
        "status": status,
        "billing_cycles": BILLING_CYCLES,
    }


def subscription_response(
    status="APPROVAL_PENDING", paypal_id=SUB_ID, plan_id=PLAN_ID, **extra
):
    payload = {
        "id": paypal_id,
        "plan_id": plan_id,
        "status": status,
        "quantity": "1",
        "start_time": "2026-08-01T00:00:00Z",
        "subscriber": {"email_address": "buyer@example.com"},
        "links": [
            {"rel": "approve", "href": "https://www.sandbox.paypal.com/webapps/billing/x"},
            {"rel": "self", "href": f"https://api.paypal.com/v1/billing/subscriptions/{paypal_id}"},
        ],
    }
    payload.update(extra)
    return payload


class RequestIdSchemeTests(TestCase):
    def test_creates_are_keyed_per_row(self):
        self.assertEqual(product_request_id(3), "product:3:create")
        self.assertEqual(plan_request_id(4), "plan:4:create")
        self.assertEqual(subscription_request_id(5), "subscription:5:create")


class ProductTests(ClientMixin, TestCase):
    def test_create_persists_the_row_and_its_key(self):
        self.fake.queue(PRODUCTS_PATH, httpx.Response(201, json=product_response()))

        product = create_product(self.client, name="Membership")

        self.assertEqual(product.paypal_id, PRODUCT_ID)
        self.assertEqual(product.product_type, Product.Type.SERVICE)
        self.assertEqual(
            self.fake.api_requests(PRODUCTS_PATH)[0].headers["paypal-request-id"],
            f"product:{product.pk}:create",
        )

    def test_type_and_description_are_forwarded(self):
        self.fake.queue(
            PRODUCTS_PATH,
            httpx.Response(201, json={**product_response(), "type": "DIGITAL"}),
        )

        product = create_product(
            self.client, name="Ebook", product_type=Product.Type.DIGITAL,
            description="A book",
        )

        body = json.loads(self.fake.api_requests(PRODUCTS_PATH)[0].read())
        self.assertEqual(body["type"], "DIGITAL")
        self.assertEqual(body["description"], "A book")
        self.assertEqual(product.product_type, Product.Type.DIGITAL)

    def test_extra_fields_are_passed_through(self):
        self.fake.queue(PRODUCTS_PATH, httpx.Response(201, json=product_response()))

        create_product(self.client, name="Membership", category="SOFTWARE")

        body = json.loads(self.fake.api_requests(PRODUCTS_PATH)[0].read())
        self.assertEqual(body["category"], "SOFTWARE")

    def test_fetch_and_refresh(self):
        self.fake.queue(
            f"{PRODUCTS_PATH}/{PRODUCT_ID}",
            httpx.Response(200, json=product_response()),
            httpx.Response(200, json=product_response(name="Renamed")),
        )

        self.assertEqual(fetch_product(self.client, PRODUCT_ID)["id"], PRODUCT_ID)

        product = Product.objects.start(name="Membership", live=False)
        product.update_from_payload(product_response())
        refresh_product(self.client, product)

        product.refresh_from_db()
        self.assertEqual(product.name, "Renamed")

    def test_refresh_needs_a_confirmed_product(self):
        product = Product.objects.start(name="Membership", live=False)

        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            refresh_product(self.client, product)

    def test_unknown_type_in_the_payload_is_ignored(self):
        product = Product.objects.start(name="Membership", live=False)
        product.update_from_payload({"id": PRODUCT_ID, "type": "SOMETHING"})

        product.refresh_from_db()
        self.assertEqual(product.product_type, Product.Type.SERVICE)

    def test_update_can_omit_the_id_and_defer_saving(self):
        product = Product.objects.start(name="Membership", live=False)

        returned = product.update_from_payload(
            {"name": "Renamed", "type": "DIGITAL"}, save=False
        )

        self.assertIs(returned, product)
        self.assertIsNone(product.paypal_id)
        self.assertEqual(product.name, "Renamed")
        product.refresh_from_db()
        self.assertEqual(product.name, "Membership")

    def test_str(self):
        product = Product.objects.start(name="Membership", live=False)
        self.assertIn("Membership", str(product))
        product.update_from_payload(product_response())
        self.assertEqual(str(product), PRODUCT_ID)


class PlanTests(ClientMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.product = Product.objects.start(name="Membership", live=False)
        self.product.update_from_payload(product_response())

    def test_create_from_a_local_product(self):
        self.fake.queue(PLANS_PATH, httpx.Response(201, json=plan_response("CREATED")))

        plan = create_plan(
            self.client, name="Monthly", billing_cycles=BILLING_CYCLES,
            product=self.product,
        )

        self.assertEqual(plan.paypal_id, PLAN_ID)
        self.assertEqual(plan.status, Plan.Status.CREATED)
        self.assertEqual(plan.product, self.product)
        body = json.loads(self.fake.api_requests(PLANS_PATH)[0].read())
        self.assertEqual(body["product_id"], PRODUCT_ID)
        self.assertEqual(body["billing_cycles"], BILLING_CYCLES)

    def test_create_from_a_bare_product_id(self):
        self.fake.queue(PLANS_PATH, httpx.Response(201, json=plan_response("CREATED")))

        plan = create_plan(
            self.client, name="Monthly", billing_cycles=BILLING_CYCLES,
            product_id=PRODUCT_ID,
        )

        self.assertIsNone(plan.product)
        self.assertEqual(plan.product_paypal_id, PRODUCT_ID)

    def test_a_plan_needs_a_product(self):
        with self.assertRaisesMessage(PayPalError, "needs a product"):
            create_plan(self.client, name="Monthly", billing_cycles=BILLING_CYCLES)

        self.assertEqual(self.fake.requests, [])
        self.assertEqual(Plan.objects.count(), 0)

    def test_product_and_product_id_are_mutually_exclusive(self):
        with self.assertRaisesMessage(PayPalError, "not both"):
            create_plan(
                self.client,
                name="Monthly",
                billing_cycles=BILLING_CYCLES,
                product=self.product,
                product_id=PRODUCT_ID,
            )

    def test_a_local_product_must_be_confirmed(self):
        product = Product.objects.start(name="Pending", live=False)

        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            create_plan(
                self.client,
                name="Monthly",
                billing_cycles=BILLING_CYCLES,
                product=product,
            )

    def test_a_local_product_must_match_the_client_environment(self):
        self.product.live = True
        self.product.save(update_fields=["live"])

        with self.assertRaisesMessage(PayPalError, "belongs to live"):
            create_plan(
                self.client,
                name="Monthly",
                billing_cycles=BILLING_CYCLES,
                product=self.product,
            )

    def test_payment_preferences_and_extras_are_forwarded(self):
        self.fake.queue(PLANS_PATH, httpx.Response(201, json=plan_response("CREATED")))

        create_plan(
            self.client, name="Monthly", billing_cycles=BILLING_CYCLES,
            product=self.product,
            payment_preferences={"payment_failure_threshold": 3},
            description="Monthly membership",
        )

        body = json.loads(self.fake.api_requests(PLANS_PATH)[0].read())
        self.assertEqual(body["payment_preferences"]["payment_failure_threshold"], 3)
        self.assertEqual(body["description"], "Monthly membership")

    def test_the_persisted_key_is_sent(self):
        self.fake.queue(PLANS_PATH, httpx.Response(201, json=plan_response("CREATED")))

        plan = create_plan(
            self.client, name="Monthly", billing_cycles=BILLING_CYCLES,
            product=self.product,
        )

        self.assertEqual(
            self.fake.api_requests(PLANS_PATH)[0].headers["paypal-request-id"],
            f"plan:{plan.pk}:create",
        )

    def test_activate_and_deactivate_on_a_204(self):
        plan = self._plan("CREATED")
        self.fake.queue(f"{PLANS_PATH}/{PLAN_ID}/activate", httpx.Response(204))
        self.fake.queue(f"{PLANS_PATH}/{PLAN_ID}/deactivate", httpx.Response(204))

        activate_plan(self.client, plan)
        plan.refresh_from_db()
        self.assertEqual(plan.status, Plan.Status.ACTIVE)
        self.assertTrue(plan.accepts_subscriptions)

        deactivate_plan(self.client, plan)
        plan.refresh_from_db()
        self.assertEqual(plan.status, Plan.Status.INACTIVE)
        self.assertFalse(plan.accepts_subscriptions)

    def test_a_returned_payload_is_preferred_over_the_implied_status(self):
        plan = self._plan("CREATED")
        self.fake.queue(
            f"{PLANS_PATH}/{PLAN_ID}/activate",
            httpx.Response(200, json=plan_response("ACTIVE")),
        )

        activate_plan(self.client, plan)

        plan.refresh_from_db()
        self.assertEqual(plan.status, Plan.Status.ACTIVE)
        self.assertEqual(plan.raw["id"], PLAN_ID)

    def test_transitions_carry_no_idempotency_key(self):
        """They are legitimately repeatable, so a fixed key would be a trap."""
        plan = self._plan("CREATED")
        self.fake.queue(f"{PLANS_PATH}/{PLAN_ID}/activate", httpx.Response(204))

        activate_plan(self.client, plan)

        request = self.fake.api_requests(f"{PLANS_PATH}/{PLAN_ID}/activate")[0]
        self.assertNotIn("paypal-request-id", request.headers)

    def test_transitions_are_silent_under_strict_idempotency(self):
        from paypal_checkout.client import PayPalClient

        plan = self._plan("CREATED")
        strict = PayPalClient(
            make_config(strict_idempotency=True), transport=self.fake.transport
        )
        self.addCleanup(strict.close)
        self.fake.queue(f"{PLANS_PATH}/{PLAN_ID}/activate", httpx.Response(204))

        with self.assertNoLogs("paypal_checkout.client", level="WARNING"):
            activate_plan(strict, plan)

    def test_transitions_need_a_confirmed_plan(self):
        plan = Plan.objects.start(name="Monthly", live=False, product=self.product)

        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            activate_plan(self.client, plan)

    def test_fetch_and_refresh(self):
        self.fake.queue(
            f"{PLANS_PATH}/{PLAN_ID}",
            httpx.Response(200, json=plan_response()),
            httpx.Response(200, json=plan_response("INACTIVE")),
        )
        self.assertEqual(fetch_plan(self.client, PLAN_ID)["id"], PLAN_ID)

        plan = self._plan("ACTIVE")
        refresh_plan(self.client, plan)

        plan.refresh_from_db()
        self.assertEqual(plan.status, Plan.Status.INACTIVE)

    def test_unknown_status_is_ignored(self):
        plan = self._plan("ACTIVE")
        plan.update_from_payload({"id": PLAN_ID, "status": "WEIRD"})

        plan.refresh_from_db()
        self.assertEqual(plan.status, Plan.Status.ACTIVE)

    def test_update_can_omit_the_id_and_defer_saving(self):
        plan = self._plan("ACTIVE")

        returned = plan.update_from_payload(
            {"name": "Renamed", "status": "INACTIVE"}, save=False
        )

        self.assertIs(returned, plan)
        self.assertEqual(plan.paypal_id, PLAN_ID)
        self.assertEqual(plan.name, "Renamed")
        plan.refresh_from_db()
        self.assertEqual(plan.name, "Monthly")
        self.assertEqual(plan.status, Plan.Status.ACTIVE)

    def test_str_and_unconfirmed(self):
        plan = Plan.objects.start(name="Monthly", live=False, product=self.product)
        self.assertIn("Monthly", str(plan))
        self.assertTrue(plan.is_unconfirmed)

        plan.update_from_payload(plan_response())
        self.assertEqual(str(plan), PLAN_ID)
        self.assertFalse(plan.is_unconfirmed)

    def _plan(self, status):
        plan = Plan.objects.start(name="Monthly", live=False, product=self.product)
        plan.update_from_payload(plan_response(status))
        return plan


class SubscriptionMixin(ClientMixin):
    def setUp(self):
        super().setUp()
        self.product = Product.objects.start(name="Membership", live=False)
        self.product.update_from_payload(product_response())
        self.plan = Plan.objects.start(name="Monthly", live=False, product=self.product)
        self.plan.update_from_payload(plan_response("ACTIVE"))

    def make_subscription(self, status="ACTIVE"):
        subscription = Subscription.objects.start(live=False, plan=self.plan)
        subscription.update_from_payload(subscription_response(status))
        return subscription


class CreateSubscriptionTests(SubscriptionMixin, TestCase):
    def test_create_from_a_local_plan(self):
        self.fake.queue(SUBSCRIPTIONS_PATH, httpx.Response(201, json=subscription_response()))

        subscription = create_subscription(self.client, plan=self.plan)

        self.assertEqual(subscription.paypal_id, SUB_ID)
        self.assertEqual(subscription.status, Subscription.Status.APPROVAL_PENDING)
        self.assertEqual(subscription.plan, self.plan)
        self.assertEqual(subscription.subscriber_email, "buyer@example.com")
        self.assertFalse(subscription.is_active)

    def test_the_approval_url_is_exposed(self):
        self.fake.queue(SUBSCRIPTIONS_PATH, httpx.Response(201, json=subscription_response()))

        subscription = create_subscription(self.client, plan=self.plan)

        self.assertEqual(
            subscription.approve_url(),
            "https://www.sandbox.paypal.com/webapps/billing/x",
        )

    def test_no_approve_link_returns_none(self):
        payload = subscription_response()
        payload["links"] = [{"rel": "self", "href": "https://x"}, "not-a-dict"]
        self.fake.queue(SUBSCRIPTIONS_PATH, httpx.Response(201, json=payload))

        subscription = create_subscription(self.client, plan=self.plan)

        self.assertIsNone(subscription.approve_url())

    def test_the_persisted_key_is_sent(self):
        self.fake.queue(SUBSCRIPTIONS_PATH, httpx.Response(201, json=subscription_response()))

        subscription = create_subscription(self.client, plan=self.plan)

        self.assertEqual(
            self.fake.api_requests(SUBSCRIPTIONS_PATH)[0].headers["paypal-request-id"],
            f"subscription:{subscription.pk}:create",
        )

    def test_optional_fields_are_forwarded(self):
        self.fake.queue(SUBSCRIPTIONS_PATH, httpx.Response(201, json=subscription_response()))

        create_subscription(
            self.client,
            plan=self.plan,
            quantity=3,
            custom_id="CUST-1",
            subscriber={"email_address": "buyer@example.com"},
            application_context={"return_url": "https://example.com/ok"},
            shipping_amount=Decimal("2.50"),
            start_time="2026-09-01T00:00:00Z",
            auto_renewal=True,
        )

        body = json.loads(self.fake.api_requests(SUBSCRIPTIONS_PATH)[0].read())
        self.assertEqual(body["quantity"], "3")
        self.assertEqual(body["custom_id"], "CUST-1")
        self.assertEqual(body["shipping_amount"], {"currency_code": "EUR", "value": "2.50"})
        self.assertEqual(body["start_time"], "2026-09-01T00:00:00Z")
        self.assertEqual(body["application_context"]["return_url"], "https://example.com/ok")
        self.assertIs(body["auto_renewal"], True)

    def test_a_bare_plan_id_works(self):
        self.fake.queue(SUBSCRIPTIONS_PATH, httpx.Response(201, json=subscription_response()))

        subscription = create_subscription(self.client, plan_id=PLAN_ID)

        self.assertIsNone(subscription.plan)
        self.assertEqual(subscription.plan_paypal_id, PLAN_ID)

    def test_a_subscription_needs_a_plan(self):
        with self.assertRaisesMessage(PayPalError, "needs a plan"):
            create_subscription(self.client)

        self.assertEqual(self.fake.requests, [])
        self.assertEqual(Subscription.objects.count(), 0)

    def test_plan_and_plan_id_are_mutually_exclusive(self):
        with self.assertRaisesMessage(PayPalError, "not both"):
            create_subscription(self.client, plan=self.plan, plan_id=PLAN_ID)

    def test_quantity_must_be_a_positive_integer(self):
        for quantity in (0, True, "1"):
            with self.subTest(quantity=quantity):
                with self.assertRaisesMessage(PayPalError, "positive integer"):
                    create_subscription(self.client, plan=self.plan, quantity=quantity)

    def test_a_local_plan_must_match_the_client_environment(self):
        self.plan.live = True
        self.plan.save(update_fields=["live"])

        with self.assertRaisesMessage(PayPalError, "belongs to live"):
            create_subscription(self.client, plan=self.plan)

    def test_an_inactive_plan_is_refused_before_calling(self):
        """PayPal would refuse it anyway; failing here says why."""
        self.plan.status = Plan.Status.CREATED
        self.plan.save(update_fields=["status"])

        with self.assertRaisesMessage(PayPalError, "not ACTIVE"):
            create_subscription(self.client, plan=self.plan)

        self.assertEqual(self.fake.requests, [])
        self.assertEqual(Subscription.objects.count(), 0)

    def test_a_bare_plan_id_skips_the_status_check(self):
        """We know nothing about a plan that is not in the local catalog."""
        self.fake.queue(SUBSCRIPTIONS_PATH, httpx.Response(201, json=subscription_response()))

        create_subscription(self.client, plan_id="P-UNKNOWN")

        self.assertEqual(len(self.fake.api_requests(SUBSCRIPTIONS_PATH)), 1)

    def test_the_target_is_linked(self):
        self.fake.queue(SUBSCRIPTIONS_PATH, httpx.Response(201, json=subscription_response()))
        shop_order = ShopOrder.objects.create(reference="SUB-1", total=Decimal("9.99"))

        subscription = create_subscription(self.client, plan=self.plan, target=shop_order)

        subscription.refresh_from_db()
        self.assertEqual(subscription.target, shop_order)
        self.assertEqual(list(Subscription.objects.for_target(shop_order)), [subscription])

    def test_failure_leaves_the_row_discoverable(self):
        self.fake.queue(SUBSCRIPTIONS_PATH, *[httpx.Response(500, json={"name": "X"})] * 3)

        with self.assertRaises(Exception):
            create_subscription(self.client, plan=self.plan)

        self.assertEqual(len(Subscription.objects.pending()), 1)


class SubscriptionLifecycleTests(SubscriptionMixin, TestCase):
    def test_suspend(self):
        subscription = self.make_subscription()
        path = f"{SUBSCRIPTIONS_PATH}/{SUB_ID}/suspend"
        self.fake.queue(path, httpx.Response(204))

        with catch_signal(subscription_suspended) as received:
            suspend_subscription(self.client, subscription, reason="Late payment")

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.Status.SUSPENDED)
        self.assertFalse(subscription.is_billable)
        self.assertEqual(json.loads(self.fake.api_requests(path)[0].read())["reason"], "Late payment")
        self.assertEqual(received[0]["reason"], "Late payment")

    def test_activate(self):
        subscription = self.make_subscription("SUSPENDED")
        self.fake.queue(f"{SUBSCRIPTIONS_PATH}/{SUB_ID}/activate", httpx.Response(204))

        with catch_signal(subscription_activated) as received:
            activate_subscription(self.client, subscription)

        subscription.refresh_from_db()
        self.assertTrue(subscription.is_active)
        self.assertEqual(len(received), 1)

    def test_cancel(self):
        subscription = self.make_subscription()
        self.fake.queue(f"{SUBSCRIPTIONS_PATH}/{SUB_ID}/cancel", httpx.Response(204))

        with catch_signal(subscription_cancelled) as received:
            cancel_subscription(self.client, subscription)

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.Status.CANCELLED)
        self.assertEqual(received[0]["subscription"], subscription)

    def test_transitions_can_be_repeated_without_a_replayed_response(self):
        """Suspend → activate → suspend is a normal life, so no fixed key."""
        subscription = self.make_subscription()
        self.fake.queue(f"{SUBSCRIPTIONS_PATH}/{SUB_ID}/suspend", httpx.Response(204), httpx.Response(204))
        self.fake.queue(f"{SUBSCRIPTIONS_PATH}/{SUB_ID}/activate", httpx.Response(204))

        suspend_subscription(self.client, subscription)
        activate_subscription(self.client, subscription)
        suspend_subscription(self.client, subscription)

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.Status.SUSPENDED)
        for request in self.fake.api_requests():
            self.assertNotIn("paypal-request-id", request.headers)

    def test_transitions_need_a_confirmed_subscription(self):
        subscription = Subscription.objects.start(live=False, plan=self.plan)

        for action in (activate_subscription, suspend_subscription, cancel_subscription):
            with self.subTest(action=action.__name__):
                with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
                    action(self.client, subscription)

        self.assertEqual(self.fake.requests, [])

    def test_transitions_refuse_the_wrong_environment(self):
        subscription = self.make_subscription()
        subscription.live = True
        subscription.save(update_fields=["live"])

        with self.assertRaisesMessage(PayPalError, "belongs to live"):
            suspend_subscription(self.client, subscription)

    def test_the_target_travels_with_the_signal(self):
        shop_order = ShopOrder.objects.create(reference="SUB-1", total=Decimal("9.99"))
        subscription = Subscription.objects.start(
            live=False, plan=self.plan, target=shop_order
        )
        subscription.update_from_payload(subscription_response("ACTIVE"))
        self.fake.queue(f"{SUBSCRIPTIONS_PATH}/{SUB_ID}/cancel", httpx.Response(204))

        with catch_signal(subscription_cancelled) as received:
            cancel_subscription(self.client, subscription)

        self.assertEqual(received[0]["target"], shop_order)


class ReviseSubscriptionTests(SubscriptionMixin, TestCase):
    def test_revise_the_quantity(self):
        subscription = self.make_subscription()
        path = f"{SUBSCRIPTIONS_PATH}/{SUB_ID}/revise"
        self.fake.queue(path, httpx.Response(200, json=subscription_response("ACTIVE", quantity="5")))

        revise_subscription(self.client, subscription, quantity=5)

        subscription.refresh_from_db()
        self.assertEqual(subscription.quantity, 5)
        self.assertEqual(json.loads(self.fake.api_requests(path)[0].read())["quantity"], "5")

    def test_revise_the_plan_relinks_the_local_row(self):
        subscription = self.make_subscription()
        other = Plan.objects.start(name="Yearly", live=False, product=self.product)
        other.update_from_payload(plan_response("ACTIVE", paypal_id="P-OTHER"))
        self.fake.queue(
            f"{SUBSCRIPTIONS_PATH}/{SUB_ID}/revise",
            httpx.Response(200, json=subscription_response("ACTIVE", plan_id="P-OTHER")),
        )

        revise_subscription(self.client, subscription, plan=other)

        subscription.refresh_from_db()
        self.assertEqual(subscription.plan, other)
        self.assertEqual(subscription.plan_paypal_id, "P-OTHER")

    def test_revise_with_a_bare_plan_id(self):
        subscription = self.make_subscription()
        self.fake.queue(
            f"{SUBSCRIPTIONS_PATH}/{SUB_ID}/revise",
            httpx.Response(200, json=subscription_response("ACTIVE", plan_id="P-OTHER")),
        )

        revise_subscription(self.client, subscription, plan_id="P-OTHER")

        subscription.refresh_from_db()
        self.assertEqual(subscription.plan, self.plan, "the local link is left alone")
        self.assertEqual(subscription.plan_paypal_id, "P-OTHER")

    def test_extras_are_forwarded(self):
        subscription = self.make_subscription()
        path = f"{SUBSCRIPTIONS_PATH}/{SUB_ID}/revise"
        self.fake.queue(path, httpx.Response(200, json=subscription_response("ACTIVE")))

        revise_subscription(
            self.client, subscription, quantity=2,
            shipping_amount={"currency_code": "EUR", "value": "1.00"},
        )

        body = json.loads(self.fake.api_requests(path)[0].read())
        self.assertEqual(body["shipping_amount"]["value"], "1.00")

    def test_revising_nothing_is_refused(self):
        subscription = self.make_subscription()

        with self.assertRaisesMessage(PayPalError, "needs a plan or a quantity"):
            revise_subscription(self.client, subscription)

    def test_plan_and_plan_id_are_mutually_exclusive(self):
        subscription = self.make_subscription()

        with self.assertRaisesMessage(PayPalError, "not both"):
            revise_subscription(
                self.client,
                subscription,
                plan=self.plan,
                plan_id=PLAN_ID,
            )

    def test_a_local_replacement_plan_must_be_active(self):
        subscription = self.make_subscription()
        replacement = Plan.objects.start(
            name="Yearly", live=False, product=self.product
        )
        replacement.update_from_payload(
            plan_response("INACTIVE", paypal_id="P-INACTIVE")
        )

        with self.assertRaisesMessage(PayPalError, "not ACTIVE"):
            revise_subscription(self.client, subscription, plan=replacement)

    def test_quantity_must_be_positive(self):
        subscription = self.make_subscription()

        with self.assertRaisesMessage(PayPalError, "positive integer"):
            revise_subscription(self.client, subscription, quantity=0)

        self.assertEqual(self.fake.requests, [])

    def test_revise_needs_a_confirmed_subscription(self):
        subscription = Subscription.objects.start(live=False, plan=self.plan)

        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            revise_subscription(self.client, subscription, quantity=2)


class FetchAndRefreshSubscriptionTests(SubscriptionMixin, TestCase):
    def test_fetch(self):
        self.fake.queue(
            f"{SUBSCRIPTIONS_PATH}/{SUB_ID}", httpx.Response(200, json=subscription_response())
        )
        self.assertEqual(fetch_subscription(self.client, SUB_ID)["id"], SUB_ID)

    def test_refresh_updates_billing_dates(self):
        subscription = self.make_subscription()
        self.fake.queue(
            f"{SUBSCRIPTIONS_PATH}/{SUB_ID}",
            httpx.Response(
                200,
                json=subscription_response(
                    "ACTIVE",
                    billing_info={"next_billing_time": "2026-09-01T10:00:00Z"},
                ),
            ),
        )

        refresh_subscription(self.client, subscription)

        subscription.refresh_from_db()
        self.assertIsNotNone(subscription.next_billing_at)
        self.assertEqual(subscription.next_billing_at.tzinfo, dt_timezone.utc)
        self.assertIsNotNone(subscription.starts_at)

    def test_refresh_needs_a_confirmed_subscription(self):
        subscription = Subscription.objects.start(live=False, plan=self.plan)

        with self.assertRaisesMessage(PayPalError, "has no PayPal id"):
            refresh_subscription(self.client, subscription)


class SubscriptionModelTests(SubscriptionMixin, TestCase):
    def test_unknown_status_and_bad_quantity_are_ignored(self):
        subscription = self.make_subscription()

        subscription.update_from_payload(
            {"id": SUB_ID, "status": "WEIRD", "quantity": "many"}
        )

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)
        self.assertEqual(subscription.quantity, 1)

    def test_missing_id_does_not_blank_an_existing_one(self):
        subscription = self.make_subscription()
        subscription.update_from_payload({"status": "SUSPENDED"})

        subscription.refresh_from_db()
        self.assertEqual(subscription.paypal_id, SUB_ID)

    def test_malformed_billing_info_and_subscriber_are_tolerated(self):
        subscription = self.make_subscription()

        subscription.update_from_payload(
            {
                "id": SUB_ID,
                "billing_info": "not-a-dict",
                "subscriber": "not-a-dict",
                "start_time": "not-a-date",
            }
        )

        subscription.refresh_from_db()
        self.assertEqual(subscription.subscriber_email, "buyer@example.com")

    def test_an_invalid_next_billing_date_is_ignored(self):
        subscription = self.make_subscription()
        previous = subscription.next_billing_at

        subscription.update_from_payload(
            {"id": SUB_ID, "billing_info": {"next_billing_time": "not-a-date"}}
        )

        subscription.refresh_from_db()
        self.assertEqual(subscription.next_billing_at, previous)

    def test_save_can_be_deferred(self):
        subscription = self.make_subscription()
        subscription.update_from_payload({"id": SUB_ID, "status": "CANCELLED"}, save=False)

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)

    def test_str_and_pending(self):
        subscription = Subscription.objects.start(live=False, plan=self.plan)
        self.assertIn("Initiated locally", str(subscription))
        self.assertEqual(list(Subscription.objects.pending()), [subscription])

        subscription.update_from_payload(subscription_response("ACTIVE"))
        self.assertEqual(str(subscription), SUB_ID)
        self.assertEqual(list(Subscription.objects.active()), [subscription])

    def test_paid_amount_counts_completed_payments_only(self):
        subscription = self.make_subscription()
        SubscriptionPayment.objects.create(
            subscription=subscription, paypal_id="SALE-1",
            amount=Decimal("9.99"), currency="EUR",
        )
        SubscriptionPayment.objects.create(
            subscription=subscription, paypal_id="SALE-2",
            amount=Decimal("9.99"), currency="EUR",
            status=SubscriptionPayment.Status.DENIED,
        )

        self.assertEqual(subscription.paid_amount, Decimal("9.99"))

    def test_paid_amount_with_no_payments(self):
        self.assertEqual(self.make_subscription().paid_amount, Decimal("0.00"))

    def test_payments_die_with_the_subscription(self):
        subscription = self.make_subscription()
        SubscriptionPayment.objects.create(
            subscription=subscription, paypal_id="SALE-1",
            amount=Decimal("9.99"), currency="EUR",
        )

        subscription.delete()

        self.assertEqual(SubscriptionPayment.objects.count(), 0)

    def test_payment_str_and_status_parsing(self):
        subscription = self.make_subscription()
        payment = SubscriptionPayment.objects.create(
            subscription=subscription, paypal_id="SALE-1",
            amount=Decimal("9.99"), currency="EUR",
        )

        self.assertEqual(str(payment), "SALE-1")
        self.assertTrue(payment.is_successful)

        # Sales use lower-case "state", not "status".
        payment.update_from_payload({"state": "refunded"})
        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.REFUNDED)

        payment.update_from_payload({"state": "unknown-thing"})
        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.REFUNDED)

    def test_payment_save_can_be_deferred(self):
        subscription = self.make_subscription()
        payment = SubscriptionPayment.objects.create(
            subscription=subscription, paypal_id="SALE-1",
            amount=Decimal("9.99"), currency="EUR",
        )

        payment.update_from_payload({"state": "denied"}, save=False)

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.COMPLETED)


class StrictModeTests(SubscriptionMixin, TestCase):
    def test_every_create_satisfies_strict_mode(self):
        from paypal_checkout.client import PayPalClient

        product_id = "PROD-STRICT"
        plan_id = "P-STRICT"
        subscription_id = "I-STRICT"
        strict = PayPalClient(
            make_config(strict_idempotency=True), transport=self.fake.transport
        )
        self.addCleanup(strict.close)
        self.fake.queue(
            PRODUCTS_PATH,
            httpx.Response(201, json=product_response(product_id)),
        )
        self.fake.queue(
            PLANS_PATH,
            httpx.Response(
                201,
                json=plan_response(
                    "CREATED", paypal_id=plan_id, product_id=product_id
                ),
            ),
        )
        self.fake.queue(
            SUBSCRIPTIONS_PATH,
            httpx.Response(
                201,
                json=subscription_response(
                    paypal_id=subscription_id, plan_id=plan_id
                ),
            ),
        )

        with self.assertNoLogs("paypal_checkout.client", level="WARNING"):
            product = create_product(strict, name="Membership")
            plan = create_plan(
                strict, name="Monthly", billing_cycles=BILLING_CYCLES, product=product
            )
            plan.status = Plan.Status.ACTIVE
            plan.save(update_fields=["status"])
            create_subscription(strict, plan=plan)

    def test_a_raw_create_without_a_key_is_still_refused(self):
        from paypal_checkout.client import PayPalClient

        strict = PayPalClient(
            make_config(strict_idempotency=True), transport=self.fake.transport
        )
        self.addCleanup(strict.close)

        with self.assertRaises(PayPalIdempotencyError):
            strict.post(SUBSCRIPTIONS_PATH, json={"plan_id": PLAN_ID})
