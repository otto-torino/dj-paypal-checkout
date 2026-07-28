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

from decimal import Decimal

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

__all__ = ["PayPalOrder", "Authorization", "Capture", "Refund", "WebhookEvent"]


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


def authorization_request_id(order_pk, authorization_pk):
    """Idempotency key for one authorization *attempt*.

    Per-attempt for the same reason as captures: an authorization can be denied,
    and a fixed key would make PayPal replay that denial for ever.
    """
    return f"order:{order_pk}:authorize:{authorization_pk}"


def refund_request_id(order_pk, refund_pk):
    """Idempotency key for one refund.

    Keyed on the refund row, so several partial refunds of the same capture are
    distinct operations — a key fixed per capture would make the second partial
    refund replay the first.
    """
    return f"order:{order_pk}:refund:{refund_pk}"


def void_request_id(order_pk, authorization_pk):
    """Idempotency key for voiding an authorization.

    The one key in this library that is *not* per attempt, and deliberately so:
    voiding is single-shot — there is no such thing as a legitimate second void
    of the same authorization — so the authorization row itself identifies the
    operation. It is also the one key not stored on a row: a future change to the
    naming scheme could only make a retry look new to PayPal, and since voiding
    an already-voided authorization is refused rather than repeated, that cannot
    move money. Contrast :func:`capture_request_id`, where both properties matter.
    """
    return f"order:{order_pk}:void:{authorization_pk}"


class PendingAttemptMixin(models.Model):
    """Shared behaviour for rows that exist before PayPal is called."""

    class Meta:
        abstract = True

    @property
    def is_unconfirmed(self):
        """The outcome is unknown — it may or may not have reached PayPal."""
        return self.status == self.Status.INITIATED


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
        """A direct capture attempt started but never confirmed, if any.

        Its outcome is unknown: it may have reached PayPal. Recovery must reuse
        *this* row's key rather than starting a new attempt. Captures made
        against an authorization are that authorization's business.
        """
        return (
            self.captures.filter(
                status=Capture.Status.INITIATED, authorization__isnull=True
            )
            .order_by("pk")
            .first()
        )

    def start_capture(self, *, amount=None, currency=None, final_capture=True):
        """Persist a capture attempt and return it, key included.

        An unconfirmed attempt is reused rather than duplicated — that is what
        makes recovery after a crash safe. A new attempt (after a decline, say)
        gets its own row and therefore its own key.
        """
        pending = self.pending_capture()
        if pending is not None:
            return pending
        return _start_capture(
            self, authorization=None, amount=amount, currency=currency,
            final_capture=final_capture,
        )

    def pending_authorization(self):
        """An authorization attempt started but never confirmed, if any."""
        return (
            self.authorizations.filter(status=Authorization.Status.INITIATED)
            .order_by("pk")
            .first()
        )

    def start_authorization(self, *, amount=None, currency=None):
        """Persist an authorization attempt and return it, key included."""
        pending = self.pending_authorization()
        if pending is not None:
            return pending
        with transaction.atomic():
            authorization = self.authorizations.create(
                status=Authorization.Status.INITIATED,
                amount=self.amount if amount is None else amount,
                currency=(currency or self.currency).upper(),
            )
            authorization.request_id = authorization_request_id(
                self.pk, authorization.pk
            )
            authorization.save(update_fields=["request_id", "updated_at"])
        return authorization

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


def _start_capture(order, *, authorization, amount, currency, final_capture):
    """Create a capture row and give it its key, in one transaction."""
    with transaction.atomic():
        capture = order.captures.create(
            authorization=authorization,
            status=Capture.Status.INITIATED,
            amount=order.amount if amount is None else amount,
            currency=(currency or order.currency).upper(),
            final_capture=final_capture,
        )
        capture.request_id = capture_request_id(order.pk, capture.pk)
        capture.save(update_fields=["request_id", "updated_at"])
    return capture


class Authorization(PendingAttemptMixin):
    """One authorization attempt against a :class:`PayPalOrder`.

    Only relevant for ``intent=AUTHORIZE``: the money is held, then captured
    later against the authorization rather than against the order.
    """

    class Status(models.TextChoices):
        #: Local-only: the row exists, the outcome is not known yet.
        INITIATED = "INITIATED", "Initiated locally"
        CREATED = "CREATED", "Created"
        CAPTURED = "CAPTURED", "Captured"
        PARTIALLY_CAPTURED = "PARTIALLY_CAPTURED", "Partially captured"
        DENIED = "DENIED", "Denied"
        EXPIRED = "EXPIRED", "Expired"
        PENDING = "PENDING", "Pending"
        VOIDED = "VOIDED", "Voided"

    order = models.ForeignKey(
        "PayPalOrder", related_name="authorizations", on_delete=models.CASCADE
    )
    paypal_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    request_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status, default=Status.INITIATED)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    expires_at = models.DateTimeField(
        null=True, blank=True, help_text="PayPal's expiration_time for the hold."
    )
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("status",))]

    def __str__(self):
        return self.paypal_id or f"{self.get_status_display()} #{self.pk}"

    def pending_capture(self):
        """A capture of *this* authorization that was never confirmed."""
        return self.captures.filter(status=Capture.Status.INITIATED).order_by("pk").first()

    def start_capture(self, *, amount=None, currency=None, final_capture=True):
        """Persist a capture attempt against this authorization."""
        pending = self.pending_capture()
        if pending is not None:
            return pending
        return _start_capture(
            self.order,
            authorization=self,
            amount=self.amount if amount is None else amount,
            currency=currency or self.currency,
            final_capture=final_capture,
        )

    def update_from_payload(self, payload, *, save=True):
        """Merge a PayPal authorization payload into this row."""
        paypal_id = payload.get("id")
        if paypal_id:
            self.paypal_id = paypal_id
        status = payload.get("status")
        if status in self.Status.values:
            self.status = status
        expiration = payload.get("expiration_time")
        if expiration:
            parsed = parse_datetime(expiration)
            if parsed is not None:
                self.expires_at = parsed
        self.raw = payload
        if save:
            self.save(
                update_fields=["paypal_id", "status", "expires_at", "raw", "updated_at"]
            )
        return self


class Capture(PendingAttemptMixin):
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
    #: Set when the money was captured against an authorization rather than
    #: directly against the order.
    authorization = models.ForeignKey(
        Authorization,
        related_name="captures",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
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

    @property
    def refunded_amount(self):
        """How much has actually been refunded (completed refunds only)."""
        return self._refund_total(Refund.Status.COMPLETED)

    @property
    def reserved_refund_amount(self):
        """Refunded, in flight, *or* of unknown outcome.

        The conservative figure: an ``INITIATED`` refund may well have reached
        PayPal, so it has to count against what is still refundable.
        """
        return self._refund_total(
            Refund.Status.COMPLETED, Refund.Status.PENDING, Refund.Status.INITIATED
        )

    @property
    def refundable_amount(self):
        return self.amount - self.reserved_refund_amount

    def _refund_total(self, *statuses):
        total = self.refunds.filter(status__in=statuses).aggregate(
            total=models.Sum("amount")
        )["total"]
        return total if total is not None else Decimal("0.00")

    def pending_refund(self):
        """A refund started but never confirmed, if any."""
        return self.refunds.filter(status=Refund.Status.INITIATED).order_by("pk").first()

    def start_refund(self, *, amount=None, note_to_payer="", invoice_id=""):
        """Persist a refund attempt and return it, key included.

        Like captures, an unconfirmed attempt is reused rather than duplicated:
        its outcome is unknown, so a retry must carry the same key.
        """
        pending = self.pending_refund()
        if pending is not None:
            return pending
        with transaction.atomic():
            refund = self.refunds.create(
                status=Refund.Status.INITIATED,
                amount=self.amount if amount is None else amount,
                currency=self.currency,
                note_to_payer=note_to_payer or "",
                invoice_id=invoice_id or "",
            )
            refund.request_id = refund_request_id(self.order_id, refund.pk)
            refund.save(update_fields=["request_id", "updated_at"])
        return refund

    def sync_refund_status(self, *, save=True):
        """Reflect completed refunds in the capture's own status.

        PayPal says the same thing through ``PAYMENT.CAPTURE.REFUNDED``; this
        keeps the row honest between the refund call and that webhook.
        """
        refunded = self.refunded_amount
        if not refunded:
            return self
        status = (
            self.Status.REFUNDED
            if refunded >= self.amount
            else self.Status.PARTIALLY_REFUNDED
        )
        if status != self.status:
            self.status = status
            if save:
                self.save(update_fields=["status", "updated_at"])
        return self

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


class WebhookEvent(models.Model):
    """A webhook PayPal delivered, and whether we finished acting on it.

    ``event_id`` is unique, which is what stops a retried delivery from being
    processed twice. ``processed_at`` is the other half: a row that exists but
    was never processed is *not* a duplicate to skip — it is unfinished work, so
    a retry is allowed to pick it up. Same reasoning as an unconfirmed capture.
    """

    event_id = models.CharField(
        max_length=64, unique=True, help_text="PayPal's event id — the dedupe key."
    )
    event_type = models.CharField(max_length=64, db_index=True)
    resource_type = models.CharField(max_length=64, blank=True)
    summary = models.TextField(blank=True)
    transmission_id = models.CharField(max_length=64, blank=True)
    live = models.BooleanField(default=False)
    payload = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(
        null=True, blank=True, help_text="PayPal's create_time for the event."
    )
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ("-received_at",)
        indexes = [models.Index(fields=("processed_at",))]

    def __str__(self):
        return f"{self.event_type} {self.event_id}"

    @property
    def is_processed(self):
        return self.processed_at is not None

    @property
    def resource(self):
        """The event's ``resource`` object, or an empty dict."""
        resource = self.payload.get("resource")
        return resource if isinstance(resource, dict) else {}

    def mark_processed(self):
        self.processed_at = timezone.now()
        self.last_error = ""
        self.save(update_fields=["processed_at", "last_error"])
        return self

    def mark_failed(self, error):
        self.processed_at = None
        self.last_error = str(error)[:2000]
        self.save(update_fields=["processed_at", "last_error"])
        return self


class Refund(PendingAttemptMixin):
    """One refund of a :class:`Capture`, full or partial."""

    class Status(models.TextChoices):
        #: Local-only: the row exists, the outcome is not known yet.
        INITIATED = "INITIATED", "Initiated locally"
        COMPLETED = "COMPLETED", "Completed"
        PENDING = "PENDING", "Pending"
        CANCELLED = "CANCELLED", "Cancelled"
        FAILED = "FAILED", "Failed"

    capture = models.ForeignKey(
        Capture, related_name="refunds", on_delete=models.CASCADE
    )
    paypal_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    request_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status, default=Status.INITIATED)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    note_to_payer = models.CharField(max_length=255, blank=True)
    invoice_id = models.CharField(max_length=127, blank=True)
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
        """Merge a PayPal refund payload into this row."""
        paypal_id = payload.get("id")
        if paypal_id:
            self.paypal_id = paypal_id
        status = payload.get("status")
        if status in self.Status.values:
            self.status = status
        self.raw = payload
        if save:
            self.save(update_fields=["paypal_id", "status", "raw", "updated_at"])
        return self
