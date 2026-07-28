"""The admin must be a window, never an editor: payment state is PayPal's."""

from decimal import Decimal

from django.contrib import admin as django_admin
from django.test import RequestFactory, TestCase

from paypal_checkout.admin import CaptureAdmin, CaptureInline, PayPalOrderAdmin
from paypal_checkout.models import Capture, PayPalOrder


class AdminRegistrationTests(TestCase):
    def test_models_are_registered(self):
        self.assertIsInstance(django_admin.site._registry[PayPalOrder], PayPalOrderAdmin)
        self.assertIsInstance(django_admin.site._registry[Capture], CaptureAdmin)


class ReadOnlyTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get("/")
        self.order_admin = PayPalOrderAdmin(PayPalOrder, django_admin.site)
        self.capture_admin = CaptureAdmin(Capture, django_admin.site)
        self.inline = CaptureInline(PayPalOrder, django_admin.site)

    def test_nothing_can_be_added_changed_or_deleted(self):
        for model_admin in (self.order_admin, self.capture_admin):
            with self.subTest(admin=type(model_admin).__name__):
                self.assertFalse(model_admin.has_add_permission(self.request))
                self.assertFalse(model_admin.has_change_permission(self.request))
                self.assertFalse(model_admin.has_delete_permission(self.request))

    def test_every_field_is_read_only(self):
        for model_admin in (self.order_admin, self.capture_admin):
            with self.subTest(admin=type(model_admin).__name__):
                fields = model_admin.get_fields(self.request)
                self.assertEqual(tuple(fields), tuple(model_admin.readonly_fields))

    def test_inline_is_read_only(self):
        self.assertFalse(self.inline.has_add_permission(self.request))
        self.assertFalse(self.inline.can_delete)
        self.assertEqual(tuple(self.inline.fields), tuple(self.inline.readonly_fields))

    def test_request_id_is_visible_for_support(self):
        """Answering "which key did we send?" is the point of this admin."""
        self.assertIn("request_id", self.order_admin.readonly_fields)
        self.assertIn("request_id", self.capture_admin.readonly_fields)
        self.assertIn("request_id", self.order_admin.search_fields)


class EnvironmentColumnTests(TestCase):
    def setUp(self):
        self.order_admin = PayPalOrderAdmin(PayPalOrder, django_admin.site)

    def test_sandbox_and_live_are_distinguishable_at_a_glance(self):
        sandbox = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=False)
        live = PayPalOrder.objects.start(amount=Decimal("1.00"), currency="EUR", live=True)

        self.assertEqual(self.order_admin.environment(sandbox), "sandbox")
        self.assertEqual(self.order_admin.environment(live), "live")
