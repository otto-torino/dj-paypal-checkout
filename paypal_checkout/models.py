"""Local cache of PayPal state.

The database is not the source of truth — PayPal is — but it has to hold enough
to answer "did this happen?" after a crash. Two things make that possible:

* **A row exists before the call.** `PayPalOrder.objects.start()` and
  `order.start_capture()` write a row *first*, so an interrupted operation is
  discoverable afterwards instead of being lost.
* **The idempotency key lives on that row.** It is derived from primary keys
  (`order:42:capture:7`), so it is stable for one attempt and different for the
  next, and it is *stored* rather than recomputed — a future change to the naming
  scheme must not hand an in-flight recovery a different key.

Both models carry a generic FK to the host project's own object, so an order can
be traced back to a cart, an invoice, a subscription row, whatever it is.
"""

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models, transaction

__all__ = ["PayPalOrder", "Capture"]


def order_request_id(order_pk):
    """Idempotency key for creating an order."""
    return f"order:{order_pk}:create"


def capture_request_id(order_pk, capture_pk):
    """Idempotency key for one capture *attempt*.

    Keyed on the capture row, so a legitimate second attempt after a decline is
    a new row and therefore a new key — a fixed ``capture-<order_pk>`` would make
    PayPal replay the first, failed response forever.
    """
    return f"order:{order_pk}:capture:{capture_pk}"


class TargetMixin(models.Model):
    """Generic FK to whatever the host project calls an order."""

    content_type = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.SET_NULL
    )
    #: Char rather than integer, so UUID and other non-integer pks work too.
    object_id = models.CharField(max_length=255, null=True, blank=True)
    target = GenericForeignKey("content_type", "object_id")

    class Meta:
        abstract = True


class PayPalOrderQuerySet(models.QuerySet):
    def pending(self):
        """Started locally but never confirmed by PayPal — needs reconciling."""
        return self.filter(status=PayPalOrder.Status.INITIATED)

    def for_target(self, target):
        return self.filter(
            content_type=ContentType.objects.get_for_model(target),
            object_id=str(target.pk),
        )


class PayPalOrderManager(models.Manager.from_queryset(PayPalOrderQuerySet)):
    def start(self, *, amount, currency, live, intent=None, target=None, raw=None):
        """Persist an order row *before* talking to PayPal, and give it its key.

        Two statements in one transaction: the key is derived from the pk, which
        only exists after the insert.
        """
        with transaction.atomic():
            order = self.create(
                amount=amount,
                currency=str(currency).upper(),
                live=live,
                intent=intent or PayPalOrder.Intent.CAPTURE,
                status=PayPalOrder.Status.INITIATED,
                raw=raw or {},
                **self.model.target_fields(target),
            )
            order.request_id = order_request_id(order.pk)
            order.save(update_fields=["request_id", "updated_at"])
        return order


class PayPalOrder(TargetMixin):
    """A PayPal Orders v2 order."""

    class Status(models.TextChoices):
        #: Local-only: the row exists, PayPal has not been called yet.
        INITIATED = "INITIATED", "Initiated locally"
        CREATED = "CREATED", "Created"
        SAVED = "SAVED", "Saved"
        APPROVED = "APPROVED", "Approved"
        PAYER_ACTION_REQUIRED = "PAYER_ACTION_REQUIRED", "Payer action required"
        VOIDED = "VOIDED", "Voided"
        COMPLETED = "COMPLETED", "Completed"

    class Intent(models.TextChoices):
        CAPTURE = "CAPTURE", "Capture"
        AUTHORIZE = "AUTHORIZE", "Authorize"

    paypal_id = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text="Null until PayPal has answered the create call.",
    )
    request_id = models.CharField(
        max_length=128,
        unique=True,
        null=True,
        blank=True,
        help_text="PayPal-Request-Id used to create this order.",
    )
    intent = models.CharField(max_length=16, choices=Intent, default=Intent.CAPTURE)
    status = models.CharField(max_length=24, choices=Status, default=Status.INITIATED)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    live = models.BooleanField(
        default=False,
        help_text="Environment this order belongs to. Sandbox and live rows must "
        "never be read as interchangeable.",
    )
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PayPalOrderManager()

    class Meta:
        verbose_name = "PayPal order"
        verbose_name_plural = "PayPal orders"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("content_type", "object_id")),
            models.Index(fields=("status", "live")),
        ]

    def __str__(self):
        return self.paypal_id or f"{self.get_status_display()} #{self.pk}"

    @staticmethod
    def target_fields(target):
        if target is None:
            return {}
        return {
            "content_type": ContentType.objects.get_for_model(target),
            "object_id": str(target.pk),
        }

    @property
    def is_confirmed_by_paypal(self):
        return bool(self.paypal_id)

    def pending_capture(self):
        """A capture attempt that was started but never confirmed, if any.

        Its outcome is unknown: it may have reached PayPal. Recovery must reuse
        *this* row's key rather than starting a new attempt.
        """
        return self.captures.filter(status=Capture.Status.INITIATED).order_by("pk").first()

    def start_capture(self, *, amount=None, currency=None, final_capture=True):
        """Persist a capture attempt and return it, key included.

        An unconfirmed attempt is reused rather than duplicated — that is what
        makes recovery after a crash safe. A new attempt (after a decline, say)
        gets its own row and therefore its own key.
        """
        pending = self.pending_capture()
        if pending is not None:
            return pending
        with transaction.atomic():
            capture = self.captures.create(
                status=Capture.Status.INITIATED,
                amount=self.amount if amount is None else amount,
                currency=(currency or self.currency).upper(),
                final_capture=final_capture,
            )
            capture.request_id = capture_request_id(self.pk, capture.pk)
            capture.save(update_fields=["request_id", "updated_at"])
        return capture

    def update_from_payload(self, payload, *, save=True):
        """Merge a PayPal order payload into this row."""
        paypal_id = payload.get("id")
        if paypal_id:
            self.paypal_id = paypal_id
        status = payload.get("status")
        if status in self.Status.values:
            self.status = status
        intent = payload.get("intent")
        if intent in self.Intent.values:
            self.intent = intent
        self.raw = payload
        if save:
            self.save(
                update_fields=["paypal_id", "status", "intent", "raw", "updated_at"]
            )
        return self


class Capture(models.Model):
    """One capture attempt against a :class:`PayPalOrder`."""

    class Status(models.TextChoices):
        #: Local-only: the row exists, the outcome is not known yet.
        INITIATED = "INITIATED", "Initiated locally"
        COMPLETED = "COMPLETED", "Completed"
        DECLINED = "DECLINED", "Declined"
        PENDING = "PENDING", "Pending"
        FAILED = "FAILED", "Failed"
        REFUNDED = "REFUNDED", "Refunded"
        PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially refunded"

    order = models.ForeignKey(
        PayPalOrder, related_name="captures", on_delete=models.CASCADE
    )
    paypal_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    request_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status, default=Status.INITIATED)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    final_capture = models.BooleanField(default=True)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("status",))]

    def __str__(self):
        return self.paypal_id or f"{self.get_status_display()} #{self.pk}"

    @property
    def is_successful(self):
        return self.status == self.Status.COMPLETED

    def update_from_payload(self, payload, *, save=True):
        """Merge a PayPal capture payload into this row."""
        paypal_id = payload.get("id")
        if paypal_id:
            self.paypal_id = paypal_id
        status = payload.get("status")
        if status in self.Status.values:
            self.status = status
        if "final_capture" in payload:
            self.final_capture = bool(payload["final_capture"])
        self.raw = payload
        if save:
            self.save(
                update_fields=[
                    "paypal_id",
                    "status",
                    "final_capture",
                    "raw",
                    "updated_at",
                ]
            )
        return self
