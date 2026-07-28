"""The webhook endpoint.

Response codes are the contract with PayPal's retry machinery, so they are
chosen deliberately:

``400`` — not a webhook we can trust (missing headers, bad signature, no event
id). Nothing is stored and nothing is retried.

``200`` — stored *and* finished. Also the answer to a duplicate delivery of an
event already processed.

``500`` — stored but **not** finished: a handler raised, or the event refers to a
row that has not been written yet. PayPal retries, and because a stored-but-
unprocessed event is not treated as a duplicate, the retry actually re-runs the
work. Answering 200 here would silently drop a payment confirmation.
"""

import json
import logging

from django.db import transaction
from django.http import HttpResponse, JsonResponse
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

        event, created = self._store(event_payload, headers, config)
        if not created and event.is_processed:
            # A genuine duplicate delivery.
            return JsonResponse({"status": "duplicate", "event": event_id})

        try:
            with transaction.atomic():
                handled = dispatch(event)
        except Exception as exc:
            event.mark_failed(exc)
            logger.exception("webhook %s failed, asking PayPal to retry", event_id)
            return JsonResponse({"status": "retry", "event": event_id}, status=500)

        event.mark_processed()
        return JsonResponse({"status": "processed", "event": event_id, "handlers": handled})

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
        event, created = WebhookEvent.objects.get_or_create(
            event_id=payload["id"], defaults=defaults
        )
        if not created and not event.is_processed:
            # Refresh the stored copy: a retry may carry a newer resource state.
            for field, value in defaults.items():
                setattr(event, field, value)
            event.save(update_fields=list(defaults))
        return event, created
