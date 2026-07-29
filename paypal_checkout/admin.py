"""Read-mostly admin.

PayPal is the source of truth, so nothing here is editable: the admin exists to
answer support questions ("did this capture go through, and with which
idempotency key?"), not to let anyone hand-edit payment state.
"""

from django.contrib import admin

from .models import (
    Authorization,
    Capture,
    PaymentToken,
    PayPalOrder,
    Plan,
    Product,
    Refund,
    Subscription,
    SubscriptionPayment,
    SetupToken,
    WebhookEvent,
)


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class CaptureInline(ReadOnlyInline):
    model = Capture
    fields = ("paypal_id", "status", "amount", "currency", "request_id", "created_at")
    readonly_fields = fields


class AuthorizationInline(ReadOnlyInline):
    model = Authorization
    fields = ("paypal_id", "status", "amount", "currency", "expires_at", "request_id")
    readonly_fields = fields


class RefundInline(ReadOnlyInline):
    model = Refund
    fields = ("paypal_id", "status", "amount", "currency", "request_id", "created_at")
    readonly_fields = fields


@admin.register(PayPalOrder)
class PayPalOrderAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "status",
        "intent",
        "amount",
        "currency",
        "environment",
        "target",
        "created_at",
    )
    list_filter = ("status", "intent", "live", "currency")
    search_fields = ("paypal_id", "request_id", "object_id")
    date_hierarchy = "created_at"
    inlines = (AuthorizationInline, CaptureInline)
    readonly_fields = (
        "paypal_id",
        "request_id",
        "intent",
        "status",
        "amount",
        "currency",
        "live",
        "content_type",
        "object_id",
        "target",
        "raw",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Environment", ordering="live")
    def environment(self, obj):
        return "live" if obj.live else "sandbox"

    def get_fields(self, request, obj=None):
        return self.readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Authorization)
class AuthorizationAdmin(admin.ModelAdmin):
    list_display = ("__str__", "order", "status", "amount", "currency", "expires_at")
    list_filter = ("status", "currency")
    search_fields = ("paypal_id", "request_id", "order__paypal_id")
    date_hierarchy = "created_at"
    inlines = (CaptureInline,)
    readonly_fields = (
        "order",
        "paypal_id",
        "request_id",
        "status",
        "amount",
        "currency",
        "expires_at",
        "raw",
        "created_at",
        "updated_at",
    )

    def get_fields(self, request, obj=None):
        return self.readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Capture)
class CaptureAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "order",
        "status",
        "amount",
        "currency",
        "refunded_amount",
        "created_at",
    )
    list_filter = ("status", "currency", "final_capture")
    search_fields = ("paypal_id", "request_id", "order__paypal_id")
    date_hierarchy = "created_at"
    inlines = (RefundInline,)
    readonly_fields = (
        "order",
        "authorization",
        "paypal_id",
        "request_id",
        "status",
        "amount",
        "currency",
        "final_capture",
        "raw",
        "created_at",
        "updated_at",
    )

    def get_fields(self, request, obj=None):
        return self.readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = (
        "event_id",
        "event_type",
        "processed",
        "environment",
        "received_at",
    )
    list_filter = ("event_type", "live", "resource_type")
    search_fields = ("event_id", "transmission_id", "event_type", "summary")
    date_hierarchy = "received_at"
    readonly_fields = (
        "event_id",
        "event_type",
        "resource_type",
        "summary",
        "transmission_id",
        "live",
        "occurred_at",
        "received_at",
        "processed_at",
        "last_error",
        "payload",
    )

    @admin.display(boolean=True, description="Processed", ordering="processed_at")
    def processed(self, obj):
        return obj.is_processed

    @admin.display(description="Environment", ordering="live")
    def environment(self, obj):
        return "live" if obj.live else "sandbox"

    def get_fields(self, request, obj=None):
        return self.readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ("__str__", "capture", "status", "amount", "currency", "created_at")
    list_filter = ("status", "currency")
    search_fields = ("paypal_id", "request_id", "capture__paypal_id", "invoice_id")
    date_hierarchy = "created_at"
    readonly_fields = (
        "capture",
        "paypal_id",
        "request_id",
        "status",
        "amount",
        "currency",
        "note_to_payer",
        "invoice_id",
        "raw",
        "created_at",
        "updated_at",
    )

    def get_fields(self, request, obj=None):
        return self.readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SubscriptionPaymentInline(ReadOnlyInline):
    model = SubscriptionPayment
    fields = ("paypal_id", "status", "amount", "currency", "created_at")
    readonly_fields = fields


class ReadOnlyModelAdmin(admin.ModelAdmin):
    """Shared read-only behaviour for the catalog and subscription admins."""

    def get_fields(self, request, obj=None):
        return self.readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Environment", ordering="live")
    def environment(self, obj):
        return "live" if obj.live else "sandbox"


@admin.register(Product)
class ProductAdmin(ReadOnlyModelAdmin):
    list_display = ("__str__", "name", "product_type", "environment", "created_at")
    list_filter = ("product_type", "live")
    search_fields = ("paypal_id", "request_id", "name")
    date_hierarchy = "created_at"
    readonly_fields = (
        "paypal_id",
        "request_id",
        "name",
        "product_type",
        "description",
        "live",
        "raw",
        "created_at",
        "updated_at",
    )


@admin.register(Plan)
class PlanAdmin(ReadOnlyModelAdmin):
    list_display = ("__str__", "name", "status", "product", "environment", "created_at")
    list_filter = ("status", "live")
    search_fields = ("paypal_id", "request_id", "name", "product_paypal_id")
    date_hierarchy = "created_at"
    readonly_fields = (
        "paypal_id",
        "request_id",
        "product",
        "product_paypal_id",
        "name",
        "status",
        "live",
        "raw",
        "created_at",
        "updated_at",
    )


@admin.register(Subscription)
class SubscriptionAdmin(ReadOnlyModelAdmin):
    list_display = (
        "__str__",
        "status",
        "plan_paypal_id",
        "subscriber_email",
        "next_billing_at",
        "paid_amount",
        "environment",
        "target",
    )
    list_filter = ("status", "live")
    search_fields = (
        "paypal_id",
        "request_id",
        "plan_paypal_id",
        "subscriber_email",
        "custom_id",
        "object_id",
    )
    date_hierarchy = "created_at"
    inlines = (SubscriptionPaymentInline,)
    readonly_fields = (
        "paypal_id",
        "request_id",
        "plan",
        "plan_paypal_id",
        "status",
        "quantity",
        "subscriber_email",
        "custom_id",
        "starts_at",
        "next_billing_at",
        "live",
        "content_type",
        "object_id",
        "target",
        "raw",
        "created_at",
        "updated_at",
    )


@admin.register(SubscriptionPayment)
class SubscriptionPaymentAdmin(ReadOnlyModelAdmin):
    list_display = ("paypal_id", "subscription", "status", "amount", "currency", "created_at")
    list_filter = ("status", "currency")
    search_fields = ("paypal_id", "subscription__paypal_id")
    date_hierarchy = "created_at"
    readonly_fields = (
        "subscription",
        "paypal_id",
        "status",
        "amount",
        "currency",
        "raw",
        "created_at",
        "updated_at",
    )


@admin.register(SetupToken)
class SetupTokenAdmin(ReadOnlyModelAdmin):
    list_display = (
        "__str__",
        "status",
        "payment_source_type",
        "customer_id",
        "environment",
        "target",
        "created_at",
    )
    list_filter = ("status", "payment_source_type", "live")
    search_fields = (
        "paypal_id",
        "request_id",
        "customer_id",
        "merchant_customer_id",
        "object_id",
    )
    date_hierarchy = "created_at"
    readonly_fields = (
        "paypal_id",
        "request_id",
        "status",
        "customer_id",
        "merchant_customer_id",
        "payment_source_type",
        "live",
        "content_type",
        "object_id",
        "target",
        "raw",
        "created_at",
        "updated_at",
    )


@admin.register(PaymentToken)
class PaymentTokenAdmin(ReadOnlyModelAdmin):
    list_display = (
        "__str__",
        "status",
        "payment_source_type",
        "customer_id",
        "environment",
        "target",
        "created_at",
    )
    list_filter = ("status", "payment_source_type", "live")
    search_fields = (
        "paypal_id",
        "request_id",
        "customer_id",
        "merchant_customer_id",
        "object_id",
    )
    date_hierarchy = "created_at"
    readonly_fields = (
        "setup_token",
        "paypal_id",
        "request_id",
        "status",
        "customer_id",
        "merchant_customer_id",
        "payment_source_type",
        "live",
        "content_type",
        "object_id",
        "target",
        "deleted_at",
        "raw",
        "created_at",
        "updated_at",
    )
