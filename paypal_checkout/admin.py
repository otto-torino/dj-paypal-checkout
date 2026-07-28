"""Read-mostly admin.

PayPal is the source of truth, so nothing here is editable: the admin exists to
answer support questions ("did this capture go through, and with which
idempotency key?"), not to let anyone hand-edit payment state.
"""

from django.contrib import admin

from .models import Authorization, Capture, PayPalOrder


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
    list_display = ("__str__", "order", "status", "amount", "currency", "created_at")
    list_filter = ("status", "currency", "final_capture")
    search_fields = ("paypal_id", "request_id", "order__paypal_id")
    date_hierarchy = "created_at"
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
