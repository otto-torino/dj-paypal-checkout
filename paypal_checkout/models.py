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

from .exceptions import PayPalAmountError, PayPalError

__all__ = [
    "PayPalOrder",
    "Authorization",
    "Capture",
    "Refund",
    "Product",
    "Plan",
    "Subscription",
    "SubscriptionPayment",
    "SetupToken",
    "PaymentToken",
    "WebhookEvent",
]


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


def product_request_id(product_pk):
    """Idempotency key for creating a catalog product."""
    return f"product:{product_pk}:create"


def plan_request_id(plan_pk):
    """Idempotency key for creating a billing plan."""
    return f"plan:{plan_pk}:create"


def subscription_request_id(subscription_pk):
    """Idempotency key for creating a subscription.

    Per row, like every other create. Note that the lifecycle transitions
    (activate / suspend / cancel / revise) deliberately carry **no** key at all —
    see :mod:`paypal_checkout.subscriptions` for why.
    """
    return f"subscription:{subscription_pk}:create"


def setup_token_request_id(setup_token_pk):
    """Idempotency key for creating a temporary Vault setup token."""
    return f"setup-token:{setup_token_pk}:create"


def payment_token_request_id(payment_token_pk):
    """Idempotency key for exchanging a setup token for a permanent token."""
    return f"payment-token:{payment_token_pk}:create"


def _start_with_key(manager, key_builder, **fields):
    """Create a row, then give it the idempotency key derived from its pk.

    Two statements in one transaction, the same pattern as
    :meth:`PayPalOrderManager.start`: the key needs the pk, which only exists
    after the insert.
    """
    with transaction.atomic():
        instance = manager.create(**fields)
        instance.request_id = key_builder(instance.pk)
        instance.save(update_fields=["request_id", "updated_at"])
    return instance


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

    @staticmethod
    def target_fields(target):
        if target is None:
            return {}
        return {
            "content_type": ContentType.objects.get_for_model(target),
            "object_id": str(target.pk),
        }


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
            Refund.Status.COMPLETED,
            Refund.Status.PENDING,
            Refund.Status.INITIATED,
            Refund.Status.UNRESOLVED,
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

    def start_refund(
        self, *, amount=None, note_to_payer="", invoice_id="", sent_body=None
    ):
        """Persist one refund attempt after taking the capture's row lock.

        The pending lookup, available-amount check and insert are one critical
        section. A retry is deliberately a separate operation: silently reusing
        an attempt would let new arguments change an old request.
        """
        with transaction.atomic():
            capture = type(self).objects.select_for_update().get(pk=self.pk)
            pending = (
                capture.refunds.filter(
                    status__in=(Refund.Status.INITIATED, Refund.Status.UNRESOLVED)
                )
                .order_by("pk")
                .first()
            )
            if pending is not None:
                raise PayPalError(
                    f"capture {capture.paypal_id or capture.pk} already has unresolved "
                    f"refund attempt #{pending.pk}; call retry_refund(client, refund) "
                    "with that row instead of starting a new request."
                )

            requested = capture.amount if amount is None else amount
            available = capture.refundable_amount
            if requested > available:
                unresolved = list(
                    capture.refunds.filter(
                        status=Refund.Status.UNRESOLVED
                    ).values_list("pk", flat=True)
                )
                review = (
                    f" Unresolved refund attempt(s) {unresolved} still reserve funds; "
                    "retry or review them first."
                    if unresolved
                    else ""
                )
                raise PayPalAmountError(
                    f"cannot refund {requested} {capture.currency} of capture "
                    f"{capture.paypal_id or capture.pk}: only {available} "
                    f"{capture.currency} is still refundable (captured "
                    f"{capture.amount}, already refunded or in flight "
                    f"{capture.reserved_refund_amount}).{review}"
                )

            refund = capture.refunds.create(
                status=Refund.Status.INITIATED,
                amount=requested,
                currency=capture.currency,
                note_to_payer=note_to_payer or "",
                invoice_id=invoice_id or "",
                sent_body=sent_body,
            )
            refund.request_id = refund_request_id(capture.order_id, refund.pk)
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
        #: Local-only: a remote refund exists, but its relationship to this
        #: interrupted attempt has not yet been proved.
        UNRESOLVED = "UNRESOLVED", "Unresolved"
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
    # NULL means a pre-migration attempt whose original full/partial request
    # shape cannot be reconstructed safely. New attempts persist {} for a full
    # refund and the canonical JSON object for every other request.
    sent_body = models.JSONField(null=True, blank=True)
    merge_metadata = models.JSONField(default=dict, blank=True)
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


class ProductManager(models.Manager):
    def start(self, *, name, live, product_type=None, description=""):
        """Persist a product row and its key before calling PayPal."""
        return _start_with_key(
            self,
            product_request_id,
            name=name,
            product_type=product_type or Product.Type.SERVICE,
            description=description or "",
            live=live,
        )


class Product(models.Model):
    """A catalog product — what a billing plan bills for."""

    class Type(models.TextChoices):
        PHYSICAL = "PHYSICAL", "Physical goods"
        DIGITAL = "DIGITAL", "Digital goods"
        SERVICE = "SERVICE", "Service"

    paypal_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    request_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    name = models.CharField(max_length=127)
    product_type = models.CharField(max_length=16, choices=Type, default=Type.SERVICE)
    description = models.TextField(blank=True)
    live = models.BooleanField(default=False)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ProductManager()

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.paypal_id or f"{self.name} #{self.pk}"

    def update_from_payload(self, payload, *, save=True):
        paypal_id = payload.get("id")
        if paypal_id:
            self.paypal_id = paypal_id
        name = payload.get("name")
        if name:
            self.name = name
        product_type = payload.get("type")
        if product_type in self.Type.values:
            self.product_type = product_type
        self.raw = payload
        if save:
            self.save(
                update_fields=["paypal_id", "name", "product_type", "raw", "updated_at"]
            )
        return self


class PlanManager(models.Manager):
    def start(self, *, name, live, product=None, product_paypal_id=""):
        """Persist a plan row and its key before calling PayPal."""
        return _start_with_key(
            self,
            plan_request_id,
            name=name,
            product=product,
            product_paypal_id=product_paypal_id or (product.paypal_id if product else ""),
            live=live,
        )


class Plan(PendingAttemptMixin):
    """A billing plan: the price and cadence subscriptions are created against."""

    class Status(models.TextChoices):
        #: Local-only: the row exists, PayPal has not been called yet.
        INITIATED = "INITIATED", "Initiated locally"
        CREATED = "CREATED", "Created"
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    paypal_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    request_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    product = models.ForeignKey(
        Product,
        related_name="plans",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="Null when the plan bills a product that is not in the local catalog.",
    )
    product_paypal_id = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=127)
    status = models.CharField(max_length=24, choices=Status, default=Status.INITIATED)
    live = models.BooleanField(default=False)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PlanManager()

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("status", "live"))]

    def __str__(self):
        return self.paypal_id or f"{self.name} #{self.pk}"

    @property
    def accepts_subscriptions(self):
        """Only an ACTIVE plan can have subscriptions created against it."""
        return self.status == self.Status.ACTIVE

    def update_from_payload(self, payload, *, save=True):
        paypal_id = payload.get("id")
        if paypal_id:
            self.paypal_id = paypal_id
        name = payload.get("name")
        if name:
            self.name = name
        status = payload.get("status")
        if status in self.Status.values:
            self.status = status
        product_id = payload.get("product_id")
        if product_id:
            self.product_paypal_id = product_id
        self.raw = payload
        if save:
            self.save(
                update_fields=[
                    "paypal_id",
                    "name",
                    "status",
                    "product_paypal_id",
                    "raw",
                    "updated_at",
                ]
            )
        return self


class SubscriptionQuerySet(models.QuerySet):
    def pending(self):
        """Started locally but never confirmed by PayPal."""
        return self.filter(status=Subscription.Status.INITIATED)

    def active(self):
        return self.filter(status=Subscription.Status.ACTIVE)

    def for_target(self, target):
        return self.filter(
            content_type=ContentType.objects.get_for_model(target),
            object_id=str(target.pk),
        )


class SubscriptionManager(models.Manager.from_queryset(SubscriptionQuerySet)):
    def start(self, *, live, plan=None, plan_paypal_id="", quantity=1, target=None,
              custom_id=""):
        """Persist a subscription row and its key before calling PayPal."""
        return _start_with_key(
            self,
            subscription_request_id,
            plan=plan,
            plan_paypal_id=plan_paypal_id or (plan.paypal_id if plan else ""),
            quantity=quantity,
            custom_id=custom_id or "",
            live=live,
            **Subscription.target_fields(target),
        )


class Subscription(TargetMixin, PendingAttemptMixin):
    """A subscription: the recurring counterpart of :class:`PayPalOrder`."""

    class Status(models.TextChoices):
        #: Local-only: the row exists, PayPal has not been called yet.
        INITIATED = "INITIATED", "Initiated locally"
        APPROVAL_PENDING = "APPROVAL_PENDING", "Approval pending"
        APPROVED = "APPROVED", "Approved"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"

    paypal_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    request_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    plan = models.ForeignKey(
        Plan,
        related_name="subscriptions",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="Null when subscribing to a plan that is not in the local catalog.",
    )
    plan_paypal_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=24, choices=Status, default=Status.INITIATED)
    quantity = models.PositiveIntegerField(default=1)
    subscriber_email = models.EmailField(blank=True)
    custom_id = models.CharField(max_length=127, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    next_billing_at = models.DateTimeField(null=True, blank=True)
    live = models.BooleanField(default=False)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SubscriptionManager()

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("content_type", "object_id")),
            models.Index(fields=("status", "live")),
        ]

    def __str__(self):
        return self.paypal_id or f"{self.get_status_display()} #{self.pk}"

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @property
    def is_billable(self):
        """Whether PayPal will keep charging it."""
        return self.status in (self.Status.ACTIVE, self.Status.APPROVED)

    @property
    def paid_amount(self):
        """Total of the completed payments recorded locally."""
        total = self.payments.filter(
            status=SubscriptionPayment.Status.COMPLETED
        ).aggregate(total=models.Sum("amount"))["total"]
        return total if total is not None else Decimal("0.00")

    def approve_url(self):
        """The link the buyer must follow to approve the subscription."""
        for link in self.raw.get("links") or []:
            if isinstance(link, dict) and link.get("rel") == "approve":
                return link.get("href")
        return None

    def update_from_payload(self, payload, *, save=True):
        fields = ["paypal_id", "status", "raw", "updated_at"]
        paypal_id = payload.get("id")
        if paypal_id:
            self.paypal_id = paypal_id
        status = payload.get("status")
        if status in self.Status.values:
            self.status = status
        plan_id = payload.get("plan_id")
        if plan_id:
            self.plan_paypal_id = plan_id
            fields.append("plan_paypal_id")
        quantity = payload.get("quantity")
        if quantity:
            try:
                self.quantity = int(quantity)
                fields.append("quantity")
            except (TypeError, ValueError):
                pass
        subscriber = payload.get("subscriber")
        if isinstance(subscriber, dict) and subscriber.get("email_address"):
            self.subscriber_email = subscriber["email_address"]
            fields.append("subscriber_email")
        custom_id = payload.get("custom_id")
        if custom_id:
            self.custom_id = custom_id
            fields.append("custom_id")
        starts_at = parse_datetime(payload.get("start_time") or "")
        if starts_at is not None:
            self.starts_at = starts_at
            fields.append("starts_at")
        billing_info = payload.get("billing_info")
        if isinstance(billing_info, dict):
            next_billing = parse_datetime(billing_info.get("next_billing_time") or "")
            if next_billing is not None:
                self.next_billing_at = next_billing
                fields.append("next_billing_at")
        self.raw = payload
        if save:
            self.save(update_fields=list(dict.fromkeys(fields)))
        return self


class SubscriptionPayment(models.Model):
    """One recurring payment of a subscription.

    Recorded from ``PAYMENT.SALE.COMPLETED``, whose resource carries a
    ``billing_agreement_id`` — that is the subscription. Unique ``paypal_id``
    means a redelivered webhook cannot count the same money twice.
    """

    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"
        PENDING = "PENDING", "Pending"
        REFUNDED = "REFUNDED", "Refunded"
        PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially refunded"
        DENIED = "DENIED", "Denied"
        FAILED = "FAILED", "Failed"

    subscription = models.ForeignKey(
        Subscription, related_name="payments", on_delete=models.CASCADE
    )
    paypal_id = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=24, choices=Status, default=Status.COMPLETED)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("status",))]

    def __str__(self):
        return self.paypal_id

    @property
    def is_successful(self):
        return self.status == self.Status.COMPLETED

    def update_from_payload(self, payload, *, save=True):
        status = payload.get("state") or payload.get("status")
        if status and status.upper() in self.Status.values:
            self.status = status.upper()
        self.raw = payload
        if save:
            self.save(update_fields=["status", "raw", "updated_at"])
        return self


def _vault_payment_source(payload):
    """Return the payment-source type without assuming a particular instrument."""
    payment_source = payload.get("payment_source")
    if not isinstance(payment_source, dict) or not payment_source:
        return ""
    return str(next(iter(payment_source)))[:32]


def _vault_customer(payload):
    customer = payload.get("customer")
    return customer if isinstance(customer, dict) else {}


_SENSITIVE_VAULT_KEYS = frozenset(
    {"number", "security_code", "card_number", "cvv", "cvv2"}
)


def _sanitize_vault_payload(value):
    """Copy a Vault payload while refusing to persist card secrets."""
    if isinstance(value, dict):
        return {
            key: _sanitize_vault_payload(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_VAULT_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_vault_payload(item) for item in value]
    return value


class SetupTokenQuerySet(models.QuerySet):
    def pending(self):
        """Setup tokens that PayPal has not yet confirmed."""
        return self.filter(status=SetupToken.Status.INITIATED)

    def for_target(self, target):
        return self.filter(
            content_type=ContentType.objects.get_for_model(target),
            object_id=str(target.pk),
        )


class SetupTokenManager(models.Manager.from_queryset(SetupTokenQuerySet)):
    def start(self, *, live, target=None, merchant_customer_id=""):
        """Persist the setup-token attempt and key before calling PayPal."""
        return _start_with_key(
            self,
            setup_token_request_id,
            live=live,
            merchant_customer_id=merchant_customer_id or "",
            **SetupToken.target_fields(target),
        )


class SetupToken(TargetMixin, PendingAttemptMixin):
    """A temporary Payment Method Tokens v3 setup token."""

    class Status(models.TextChoices):
        INITIATED = "INITIATED", "Initiated locally"
        CREATED = "CREATED", "Created"
        PAYER_ACTION_REQUIRED = "PAYER_ACTION_REQUIRED", "Payer action required"
        APPROVED = "APPROVED", "Approved"
        VAULTED = "VAULTED", "Vaulted"
        TOKENIZED = "TOKENIZED", "Tokenized"

    paypal_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    request_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    status = models.CharField(max_length=32, choices=Status, default=Status.INITIATED)
    customer_id = models.CharField(max_length=64, blank=True)
    merchant_customer_id = models.CharField(max_length=255, blank=True)
    payment_source_type = models.CharField(max_length=32, blank=True)
    live = models.BooleanField(default=False)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SetupTokenManager()

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("content_type", "object_id")),
            models.Index(fields=("status", "live")),
            models.Index(fields=("customer_id", "live")),
        ]

    def __str__(self):
        return self.paypal_id or f"{self.get_status_display()} #{self.pk}"

    def approve_url(self):
        """The PayPal-hosted approval URL, when payer action is required."""
        for link in self.raw.get("links") or []:
            if isinstance(link, dict) and link.get("rel") == "approve":
                return link.get("href")
        return None

    def update_from_payload(self, payload, *, save=True):
        fields = ["paypal_id", "status", "customer_id", "merchant_customer_id",
                  "payment_source_type", "raw", "updated_at"]
        paypal_id = payload.get("id")
        if paypal_id:
            self.paypal_id = paypal_id
        status = payload.get("status")
        if status in self.Status.values:
            self.status = status
        customer = _vault_customer(payload)
        if customer.get("id"):
            self.customer_id = customer["id"]
        if customer.get("merchant_customer_id"):
            self.merchant_customer_id = customer["merchant_customer_id"]
        source_type = _vault_payment_source(payload)
        if source_type:
            self.payment_source_type = source_type
        self.raw = _sanitize_vault_payload(payload)
        if save:
            self.save(update_fields=fields)
        return self


class PaymentTokenQuerySet(models.QuerySet):
    def active(self):
        return self.filter(status=PaymentToken.Status.ACTIVE)

    def for_customer(self, customer_id, *, live=None):
        queryset = self.filter(customer_id=customer_id)
        return queryset if live is None else queryset.filter(live=live)

    def for_target(self, target):
        return self.filter(
            content_type=ContentType.objects.get_for_model(target),
            object_id=str(target.pk),
        )


class PaymentTokenManager(models.Manager.from_queryset(PaymentTokenQuerySet)):
    def start(
        self,
        *,
        live,
        setup_token=None,
        target=None,
        customer_id="",
        merchant_customer_id="",
    ):
        """Persist the payment-token attempt and key before calling PayPal."""
        if setup_token is not None:
            existing = self.filter(setup_token=setup_token).first()
            if existing is not None and existing.is_unconfirmed:
                return existing
        if target is None and setup_token is not None:
            target = setup_token.target
        return _start_with_key(
            self,
            payment_token_request_id,
            live=live,
            setup_token=setup_token,
            customer_id=customer_id
            or (setup_token.customer_id if setup_token is not None else ""),
            merchant_customer_id=merchant_customer_id
            or (
                setup_token.merchant_customer_id
                if setup_token is not None
                else ""
            ),
            **PaymentToken.target_fields(target),
        )


class PaymentToken(TargetMixin, PendingAttemptMixin):
    """A reusable vaulted payment method.

    Only PayPal's token and masked response metadata are stored. Applications
    must never put a PAN or security code in ``raw``.
    """

    class Status(models.TextChoices):
        INITIATED = "INITIATED", "Initiated locally"
        ACTIVE = "ACTIVE", "Active"
        DELETION_PENDING = "DELETION_PENDING", "Deletion pending"
        DELETED = "DELETED", "Deleted"

    setup_token = models.OneToOneField(
        SetupToken,
        related_name="payment_token",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    paypal_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    request_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    status = models.CharField(max_length=32, choices=Status, default=Status.INITIATED)
    customer_id = models.CharField(max_length=64, blank=True)
    merchant_customer_id = models.CharField(max_length=255, blank=True)
    payment_source_type = models.CharField(max_length=32, blank=True)
    live = models.BooleanField(default=False)
    raw = models.JSONField(default=dict, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PaymentTokenManager()

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("content_type", "object_id")),
            models.Index(fields=("status", "live")),
            models.Index(fields=("customer_id", "live")),
        ]

    def __str__(self):
        return self.paypal_id or f"{self.get_status_display()} #{self.pk}"

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    def update_from_payload(self, payload, *, save=True):
        fields = ["paypal_id", "status", "customer_id", "merchant_customer_id",
                  "payment_source_type", "raw", "deleted_at", "updated_at"]
        paypal_id = payload.get("id")
        if paypal_id:
            self.paypal_id = paypal_id
        self.status = self.Status.ACTIVE
        self.deleted_at = None
        customer = _vault_customer(payload)
        if customer.get("id"):
            self.customer_id = customer["id"]
        if customer.get("merchant_customer_id"):
            self.merchant_customer_id = customer["merchant_customer_id"]
        source_type = _vault_payment_source(payload)
        if source_type:
            self.payment_source_type = source_type
        self.raw = _sanitize_vault_payload(payload)
        if save:
            self.save(update_fields=fields)
        return self

    def mark_deletion_pending(self, payload=None):
        self.status = self.Status.DELETION_PENDING
        if payload is not None:
            self.raw = _sanitize_vault_payload(payload)
        self.save(update_fields=["status", "raw", "updated_at"])
        return self

    def mark_deleted(self, payload=None):
        self.status = self.Status.DELETED
        self.deleted_at = timezone.now()
        if payload is not None:
            self.raw = _sanitize_vault_payload(payload)
        self.save(update_fields=["status", "deleted_at", "raw", "updated_at"])
        return self
