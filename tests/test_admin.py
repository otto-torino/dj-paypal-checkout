"""The admin must be a window, never an editor: payment state is PayPal's."""

from decimal import Decimal

from django.contrib import admin as django_admin
from django.test import RequestFactory, TestCase

from paypal_checkout.admin import (
    AuthorizationAdmin,
    AuthorizationInline,
    CaptureAdmin,
    CaptureInline,
    PayPalOrderAdmin,
    RefundAdmin,
    RefundInline,
    WebhookEventAdmin,
)
from paypal_checkout.models import (
    Authorization,
    Capture,
    PayPalOrder,
    Refund,
    WebhookEvent,
)


class AdminRegistrationTests(TestCase):
    def test_models_are_registered(self):
        self.assertIsInstance(django_admin.site._registry[PayPalOrder], PayPalOrderAdmin)
        self.assertIsInstance(django_admin.site._registry[Capture], CaptureAdmin)
        self.assertIsInstance(
            django_admin.site._registry[Authorization], AuthorizationAdmin
        )
        self.assertIsInstance(
            django_admin.site._registry[WebhookEvent], WebhookEventAdmin
        )
        self.assertIsInstance(django_admin.site._registry[Refund], RefundAdmin)


class ReadOnlyTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get("/")
        self.admins = (
            PayPalOrderAdmin(PayPalOrder, django_admin.site),
            AuthorizationAdmin(Authorization, django_admin.site),
            CaptureAdmin(Capture, django_admin.site),
            RefundAdmin(Refund, django_admin.site),
            WebhookEventAdmin(WebhookEvent, django_admin.site),
        )
        self.inlines = (
            CaptureInline(PayPalOrder, django_admin.site),
            AuthorizationInline(PayPalOrder, django_admin.site),
            RefundInline(Capture, django_admin.site),
        )

    def test_nothing_can_be_added_changed_or_deleted(self):
        for model_admin in self.admins:
            with self.subTest(admin=type(model_admin).__name__):
                self.assertFalse(model_admin.has_add_permission(self.request))
                self.assertFalse(model_admin.has_change_permission(self.request))
                self.assertFalse(model_admin.has_delete_permission(self.request))

    def test_every_field_is_read_only(self):
        for model_admin in self.admins:
            with self.subTest(admin=type(model_admin).__name__):
                fields = model_admin.get_fields(self.request)
                self.assertEqual(tuple(fields), tuple(model_admin.readonly_fields))

    def test_inlines_are_read_only(self):
        for inline in self.inlines:
            with self.subTest(inline=type(inline).__name__):
                self.assertFalse(inline.has_add_permission(self.request))
                self.assertFalse(inline.can_delete)
                self.assertEqual(tuple(inline.fields), tuple(inline.readonly_fields))

    def test_request_id_is_visible_for_support(self):
        """Answering "which key did we send?" is the point of this admin."""
        for model_admin in self.admins:
            if model_admin.model is WebhookEvent:
                continue  # inbound: no key of ours, but the transmission id
            with self.subTest(admin=type(model_admin).__name__):
                self.assertIn("request_id", model_admin.readonly_fields)
                self.assertIn("request_id", model_admin.search_fields)

    def test_webhook_events_expose_their_transmission_id(self):
        webhook_admin = WebhookEventAdmin(WebhookEvent, django_admin.site)
        self.assertIn("transmission_id", webhook_admin.readonly_fields)
        self.assertIn("transmission_id", webhook_admin.search_fields)
        self.assertIn("last_error", webhook_admin.readonly_fields)


class EnvironmentColumnTests(TestCase):
    def setUp(self):
        self.order_admin = PayPalOrderAdmin(PayPalOrder, django_admin.site)

    def test_sandbox_and_live_are_distinguishable_at_a_glance(self):
        sandbox = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=False)
        live = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=True)

        self.assertEqual(self.order_admin.environment(sandbox), "sandbox")
        self.assertEqual(self.order_admin.environment(live), "live")

    def test_webhook_events_show_environment_and_processed_state(self):
        webhook_admin = WebhookEventAdmin(WebhookEvent, django_admin.site)
        event = WebhookEvent.objects.create(
            event_id="WH-1", event_type="PAYMENT.CAPTURE.COMPLETED", live=True
        )

        self.assertEqual(webhook_admin.environment(event), "live")
        self.assertFalse(webhook_admin.processed(event))

        event.mark_processed()
        self.assertTrue(webhook_admin.processed(event))
