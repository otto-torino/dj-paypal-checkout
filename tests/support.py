"""Test helpers.

Every HTTP interaction in the suite goes through :class:`FakePayPal`, an
``httpx`` mock transport — no test may ever touch the real API, not even the
sandbox.
"""

from collections import defaultdict, deque

import httpx

from paypal_checkout.auth import TOKEN_PATH
from paypal_checkout.config import PayPalConfig


def make_config(**overrides):
    """A config suitable for tests: dummy creds, no backoff sleeps."""
    values = {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "live": False,
        "retry_backoff": 0,
        "max_retries": 2,
    }
    values.update(overrides)
    return PayPalConfig(**values)


class FakePayPal:
    """Queue canned responses per path and record what was requested.

    The token endpoint answers automatically unless responses are queued for
    it, so tests only describe the calls they actually care about.
    """

    def __init__(self, *, expires_in=32400):
        self.expires_in = expires_in
        self.requests = []
        self.token_requests = []
        self._queues = defaultdict(deque)

    def queue(self, path, *responses):
        self._queues[path].extend(responses)
        return self

    def queue_token(self, *responses):
        return self.queue(TOKEN_PATH, *responses)

    @property
    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request):
        path = request.url.path
        self.requests.append(request)
        queued = self._queues[path]

        if path == TOKEN_PATH:
            self.token_requests.append(request)
            if queued:
                return self._respond(queued.popleft())
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{len(self.token_requests)}",
                    "token_type": "Bearer",
                    "expires_in": self.expires_in,
                },
            )

        if not queued:
            raise AssertionError(f"Unexpected request: {request.method} {path}")
        return self._respond(queued.popleft())

    @staticmethod
    def _respond(queued):
        # A queued entry may be an exception to raise instead of a response,
        # which is how transport failures are simulated.
        if isinstance(queued, Exception):
            raise queued
        return queued

    def api_requests(self, path=None):
        """Requests other than token requests, optionally filtered by path."""
        return [
            request
            for request in self.requests
            if request.url.path != TOKEN_PATH
            and (path is None or request.url.path == path)
        ]
