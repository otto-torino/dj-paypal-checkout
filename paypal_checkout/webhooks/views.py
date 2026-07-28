"""The webhook endpoint.

PayPal retries **any** non-2xx response (up to about 25 times over three days),
so the status code does not decide *whether* it retries — it records what we did
with the delivery:

``400`` — not a webhook we can trust (missing headers, bad signature, unreadable
body, no event id). **Not persisted.** PayPal will still retry and get the same
answer, which is exactly right: a forged or malformed delivery never becomes
state.

``200`` — stored *and* finished. Also the answer to a duplicate delivery of an
event already processed.

``500`` — stored but **not** finished: a handler raised, or the event refers to a
row that has not been written yet. Here the retry is what we actually want, and
because a stored-but-unprocessed event is not treated as a duplicate, the retry
really does re-run the work. Answering 200 here would silently drop a payment
confirmation.

Concurrency: two simultaneous deliveries of the same event must not both run the
handlers. The unique ``event_id`` alone cannot prevent that — both would read
``processed_at IS NULL`` and proceed — so ownership is taken with a conditional
UPDATE inside the same transaction as the handlers. See :meth:`_claim`.
"""

import json
import logging

from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.dateparse import parse_datetime
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from ..config import get_config
from ..exceptions import PayPalWebhookError
from ..models import WebhookEvent
from .handlers import dispatch
from .verify import signature_headers, verify_webhook

__all__ = ["ProcessWebhookView"]

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class ProcessWebhookView(View):
    """Receive, verify, store and dispatch a PayPal webhook.

    Mount it at a URL you then register as a webhook in the PayPal dashboard,
    and put that webhook's id in ``PAYPAL['WEBHOOK_ID']``.

    Nothing may consume the request body before this view: verification runs on
    the exact bytes PayPal signed.
    """

    #: Injected by tests; production never passes a transport.
    transport = None

    def get_config(self):
        return get_config()

    def post(self, request, *args, **kwargs):
        config = self.get_config()
        body = request.body

        try:
            headers = signature_headers(request)
        except PayPalWebhookError as exc:
            logger.warning("rejected webhook: %s", exc)
            return HttpResponse(str(exc), status=400)

        try:
            event_payload = json.loads(body)
            if not isinstance(event_payload, dict):
                raise ValueError("event must be a JSON object")
        except ValueError as exc:
            logger.warning("rejected webhook: unreadable body (%s)", exc)
            return HttpResponse("unreadable body", status=400)

        try:
            verified = verify_webhook(
                config=config,
                headers=headers,
                body=body,
                event=event_payload,
                transport=self.transport,
            )
        except PayPalWebhookError as exc:
            # Verification could not be attempted — refuse rather than guess.
            logger.warning("rejected webhook: %s", exc)
            return HttpResponse(str(exc), status=400)

        if not verified:
            logger.warning(
                "rejected webhook %s: signature does not verify",
                headers["transmission_id"],
            )
            return HttpResponse("invalid signature", status=400)

        event_id = event_payload.get("id")
        if not event_id:
            return HttpResponse("event has no id", status=400)

        event = self._store(event_payload, headers, config)
        if event.is_processed:
            # Fast path for a duplicate; the claim below is the authority.
            return JsonResponse({"status": "duplicate", "event": event_id})

        try:
            with transaction.atomic():
                if not self._claim(event):
                    return JsonResponse({"status": "duplicate", "event": event_id})
                handled = dispatch(event)
        except Exception as exc:
            # The claim and any partial handler effects have been rolled back;
            # this write is outside that transaction so the error survives.
            event.mark_failed(exc)
            logger.exception("webhook %s failed, asking PayPal to retry", event_id)
            return JsonResponse({"status": "retry", "event": event_id}, status=500)

        return JsonResponse({"status": "processed", "event": event_id, "handlers": handled})

    def _claim(self, event):
        """Take ownership of an unprocessed event, atomically.

        A conditional UPDATE rather than a read: two simultaneous deliveries
        would both see ``processed_at IS NULL`` and both run the handlers. The
        UPDATE takes a write lock, so the loser matches zero rows and is treated
        as a duplicate — on SQLite and PostgreSQL alike, no ``SELECT FOR UPDATE``
        needed.

        It also runs *inside* the handlers' transaction, so ``processed_at``
        becomes visible only together with their effects: no window in which the
        work is applied but the event still looks unprocessed, and none in which
        it looks processed while the effects are missing.
        """
        claimed = WebhookEvent.objects.filter(
            pk=event.pk, processed_at__isnull=True
        ).update(processed_at=timezone.now(), last_error="")
        return bool(claimed)

    def _store(self, payload, headers, config):
        """Upsert the event row. Unique ``event_id`` is the dedupe key."""
        defaults = {
            "event_type": payload.get("event_type") or "",
            "resource_type": payload.get("resource_type") or "",
            "summary": payload.get("summary") or "",
            "transmission_id": headers["transmission_id"],
            "live": config.live,
            "payload": payload,
            "occurred_at": parse_datetime(payload.get("create_time") or "") or None,
        }
        try:
            event, created = WebhookEvent.objects.get_or_create(
                event_id=payload["id"], defaults=defaults
            )
        except IntegrityError:
            # Lost a race to create the row; the winner's copy is good.
            return WebhookEvent.objects.get(event_id=payload["id"])

        if not created and not event.is_processed and event.payload != payload:
            # A retry may carry a newer resource state; an identical redelivery
            # must not cost a pointless write (nor a lock a rival is waiting on).
            for field, value in defaults.items():
                setattr(event, field, value)
            event.save(update_fields=list(defaults))
        return event
