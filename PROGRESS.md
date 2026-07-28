# Progress — modern PayPal integration library for Django

Status: **greenfield / design phase**. The directory contains only an empty
`doc.md`; no git repo, no package skeleton yet.
Started: 2026-07-28.

Goal: a maintained, REST-first PayPal library for Django — Orders v2 checkout,
subscriptions, refunds and **webhooks** — replacing the IPN/PDT-era tooling.
Packaged and released like the other Otto libraries (`dj-editor-js`,
`django-baton`).

## Why a new library (landscape checked 2026-07-28)

- **`django-paypal` (spookylukey)** — the incumbent on PyPI. Built around
  **PayPal Payments Standard + IPN/PDT**, i.e. the Classic stack. PayPal now
  recommends webhooks for *all* new integrations and IPN does not fire for newer
  payment products. Not a base to build on; only useful as a migration source.
- **`dj-paypal` (HearthSim)** — dj-stripe-inspired, models + a
  `ProcessWebhookView`, closer to the right shape but narrow in scope and
  low activity.
- **`paypalrestsdk` / `paypal-checkout-serversdk`** — archived/legacy, do not use.
- **`paypal-server-sdk` (official, PyPI)** — v2.3.0, released 2026-06-05.
  Covers 5 controllers: Orders v2, Payments v2, Vault v3 (US only),
  Transaction Search v1, Subscriptions v1. Explicitly incomplete ("only 5 of
  PayPal's API endpoints"), **sync only**, and — importantly — **no webhook
  verification and no plans/products catalog**.
- **Client side**: JS SDK **v6** is current (standalone buttons, iframe-based
  integrations, `findEligibleMethods` for payment-method eligibility, Card
  Fields for ACDC). v5 is the previous generation; legacy Hosted Fields are
  superseded by Card Fields. `@paypal/react-paypal-js` wraps v6 for React.

Conclusion: there is no library that gives a Django project Orders v2 +
subscriptions + verified webhooks + models/signals out of the box. That is the gap.

## Decisions taken (with Elisa, 2026-07-28)

1. **HTTP layer — own thin client over `httpx`**, sync *and* async, not a wrapper
   around `paypal-server-sdk`. The official SDK is sync-only and covers neither
   webhook signature verification nor the plans/products catalog (both mandatory
   here), and its generated model layer would leak into our public API. Accepted
   cost: we own request/response mapping and must track PayPal API changes.
2. **v0.1 scope — one-off payments + webhooks.** Orders v2 (create → approve →
   capture), verified webhooks, models/signals, admin, refunds. Subscriptions
   (M5), Vault and ACDC/Card Fields (M6) come after the first release.
3. **Name — `dj-paypal-checkout`**, import package `paypal_checkout`, repo
   `otto-torino/dj-paypal-checkout`. Verified free on PyPI on 2026-07-28
   (`django-paypal` and `dj-paypal` are taken; `django-paypal2` also exists).
   Matches the `dj-editor-js` convention, and "checkout" signals REST/Orders v2
   rather than the IPN-era incumbent.
4. **Supported versions — Django 5.2 LTS + 6.0, Python 3.11+.** Django 4.2 LTS
   went EOL in April 2026; supporting it would pin us to older idioms. Decided by
   default, not discussed — revisit if a existing project needs 4.2.
5. **Concrete models + generic FK.** The dj-stripe approach: concrete
   `PayPalOrder` / `Capture` / `Refund` / `WebhookEvent` (and later
   `Subscription`) tables act as a local cache of PayPal state, each with a
   generic FK to the host project's own order object. This is what makes the
   admin, the audit trail and the re-sync command possible; abstract/swappable
   models were rejected as boilerplate for every consumer.

## Proposed architecture

```
paypal_checkout/
  config.py        # get_paypal_config(): settings.PAYPAL merged over defaults
  client.py        # PayPalClient / AsyncPayPalClient (httpx), token cache, retries
  auth.py          # OAuth2 client_credentials, token caching + refresh
  orders.py        # Orders v2:      create / show / capture / authorize / confirm
  payments.py      # Payments v2:    captures, refunds, authorizations
  subscriptions.py # Subscriptions v1 + plans/products catalog
  vault.py         # Vault v3 (setup tokens → payment tokens)
  webhooks/
    verify.py      # offline RSA-SHA256 verification (+ API fallback)
    views.py       # ProcessWebhookView (csrf_exempt, raw body, dedupe)
    handlers.py    # event → signal dispatch registry
  models.py        # Order, Capture, Refund, Subscription, WebhookEvent
  signals.py       # payment_captured, payment_denied, subscription_activated, …
  admin.py
  templatetags/    # JS SDK v6 script tag + button container helper
  static/, templates/
example/           # runnable demo project (sandbox creds via .env)
tests/             # runtests.py + test_settings.py, one module per unit
```

Mirrors the layout that works in `dj-editor-js`: a single `config.py` as the only
place that reads `settings`, everything else takes explicit arguments.

### Correctness/security points that must be in the design from day one

- **Webhook verification**: offline verification over
  `transmissionId|transmissionTime|webhookId|crc32(rawBody)` with the cert from
  `PAYPAL-CERT-URL` — *validating that the cert URL is a paypal.com host* before
  fetching — cert cached; `/v1/notifications/verify-webhook-signature` as
  fallback. Needs the **raw** request body, so no middleware may consume it.
- **Event dedupe**: PayPal retries. Persist `WebhookEvent.event_id` unique and
  make handlers idempotent; ack fast (2xx) and do work out of band.
- **Idempotency on writes**: send `PayPal-Request-Id` on order create/capture so
  a retried request cannot double-charge.
- **Money**: `Decimal` end to end, never float. PayPal wants string amounts with
  currency-specific scale — zero-decimal currencies (JPY et al.) must not be sent
  with `.00`. Check the current list against the docs when implementing.
- **Never trust the client**: the amount is computed server-side from our own
  order; the JS SDK only ever receives an order id.
- **Sandbox/live**: `api-m.sandbox.paypal.com` vs `api-m.paypal.com`, switched by
  config, never by a code path that can be wrong in production.
- **Token cache**: access tokens are long-lived (hours) — cache them
  (Django cache, per-client-id key) and refresh on 401, don't re-auth per request.

### Events to handle in v0.1

`CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.DENIED`,
`PAYMENT.CAPTURE.PENDING`, `PAYMENT.CAPTURE.REFUNDED`. Subscription events
(`BILLING.SUBSCRIPTION.ACTIVATED` / `.CANCELLED` / `.EXPIRED`,
`PAYMENT.SALE.COMPLETED`) with the subscriptions milestone.

## Roadmap

### M0 — repo skeleton ✅ done 2026-07-28
- [x] `git init` (branch `main`, no commit yet), MIT `LICENSE` (2026 Otto.srl),
      `pyproject.toml`, `.gitignore`, `MANIFEST.in`, `.coveragerc`,
      `.python-version`, `tasks.py`
- [x] `CLAUDE.md` + `README.md`, `.readthedocs.yaml`, `docs/` (Sphinx: index,
      installation, configuration, usage, api_reference)
- [x] `tests/runtests.py` + `tests/test_settings.py` (in-memory SQLite, dummy
      `PAYPAL` creds), `tests/urls.py`, `tests/test_app/` with `ShopOrder`
      (stand-in for a host project's order, used by the generic FK from M2)
- [x] `paypal_checkout/` package: `__init__.py` (`__version__`), `apps.py`
- [x] `tests/test_skeleton.py` — 3 smoke tests, incl. `__version__` ==
      `pyproject.toml` version (a mismatch would ship a mislabelled release)
- [x] `.github/workflows/ci.yml` (py3.11/3.12 × Django 5.2, py3.13/3.14 ×
      Django 6.0) + `publish.yml` (publish-on-version-bump, as `dj-editor-js`)
- [x] deleted the empty `doc.md`

**Verified locally**: `venv/` created, `pip install -e ".[crypto]"` resolves
(Django 6.0.7, httpx 0.28.1); `python tests/runtests.py` → 3 tests OK, coverage
100% of 7 statements; `python -m build` + `twine check` → both artifacts PASSED
(PEP 639 license metadata, LICENSE included); Sphinx builds with 0 warnings;
all three YAML files parse; the publish guard correctly resolves to SKIP at 0.0.0.

Note: `version = "0.0.0"` means "never released" and `publish.yml` explicitly
skips it, so the skeleton cannot publish an empty package to PyPI by accident.
The first release is the bump to `0.1.0` on `main`.

### M1 — client + auth ✅ done 2026-07-28
- [x] `config.py` — frozen `PayPalConfig` dataclass + `get_config(**overrides)`.
      Chose a validated dataclass over a raw dict: unknown keys are **rejected**
      (a typo can't silently leave you on a default), types are coerced, and
      `PayPalConfigurationError` is also an `ImproperlyConfigured`. Overrides
      make multi-account setups possible.
- [x] `auth.py` — OAuth2 client_credentials, token cached in the Django cache
      under `sha256(client_id:secret:environment)`: sandbox/live can't be mixed
      up, rotating the secret invalidates immediately, credentials never appear
      in a cache key, and a token with less life than `TOKEN_LEEWAY` isn't
      cached. Sync + async (`aget`/`aset`).
- [x] `exceptions.py` — `PayPalError` tree; `error_from_response` handles *both*
      PayPal error shapes (REST `name/message/debug_id/details` and OAuth
      `error/error_description`) and falls back to the `Paypal-Debug-Id` /
      `Correlation-Id` headers. `debug_id` is on the exception and in `str()`,
      since that's what PayPal support asks for.
- [x] `client.py` — `PayPalClient` + `AsyncPayPalClient`, identical surface,
      context managers, connection pooling, `PayPal-Request-Id` support,
      one-shot token refresh + replay on 401, exponential backoff with jitter
      honouring `Retry-After`.
- [x] **Retry safety** (the point of the module): `POST`/`PATCH` are retried
      *only* when the caller passed a `request_id`, because PayPal dedupes on
      that header — otherwise a retried capture could charge twice. Same rule
      applies to connection errors, since the request may have reached PayPal.
      A 401 replay is exempt: nothing happened server-side.
- [x] `STRICT_IDEMPOTENCY` config + `PayPalIdempotencyError` + the missing-key
      warning (added after review; see the M2 idempotency contract below).
- [x] Tests: 119 total, **100% coverage incl. branches**. All HTTP goes through
      an `httpx.MockTransport` (`tests/support.py::FakePayPal`) — no live or
      sandbox calls anywhere.
- [x] 401 contract pinned by explicit tests (added after review): the replay
      reuses the **same** `request_id` *and* body, the refresh happens exactly
      once, a replay that then fails with 5xx or a connection error is **not**
      retried further, and the replay does **not** consume the `max_retries`
      budget (1 attempt + 1 replay + 2 retries = 4 calls, verified).
- [x] Docs: `configuration.rst` and `usage.rst` now document the real API
      (settings reference, retry rules, error handling); `api_reference.rst`
      autodocs config/client/auth/exceptions.

**Verified locally**: 101 tests OK on **py3.14 + Django 6.0.7** *and*
**py3.12 + Django 5.2.16** (both ends of the CI matrix); coverage 100%;
Sphinx builds with 0 warnings.

Deliberately deferred to M2 (not oversights): amount/currency formatting
(`money.py`, incl. zero-decimal currencies), and any Orders-specific wrapper —
M1 stops at the transport layer.

### M2 — one-off payments (Orders v2)

**Idempotency contract (decided with Elisa, 2026-07-28).** The economic
invariant lives in the wrappers, not in the low-level client — which stays
deliberately *sharp*. Requirements:

- A `PayPal-Request-Id` must be **stable for the same operation** and
  **different for different operations**. Scheme:
  `order:<pk>:authorize`, `order:<pk>:capture:<capture-attempt>`,
  `order:<pk>:refund:<refund-pk>`.
- ⚠️ A fixed `capture-<order_pk>` is *wrong*: it would block a legitimate
  second attempt after a decline, because PayPal would replay the first
  response. Hence the attempt counter in the key.
- The id must be **persisted before the call** (a row/field written first), so
  it survives a crash and the retry reuses it instead of minting a new one.
- Two distinct layers, do not conflate:
  - *within one call* — retries of the same attempt must reuse one id
    (`client.py` already guarantees this; the 401 replay reuses it too, covered
    by tests);
  - *across crashes, restarts and re-run jobs* — needs the persistent,
    deterministic id from the application layer. A per-call UUID cannot help
    here, which is exactly why the wrappers own it.
- **Decided**: the low-level client never auto-generates a `request_id`. A write
  without one is simply not retried. Auto-generating would make
  `_is_safe_to_retry` always true for writes, silently removing the "the caller
  has thought about idempotency" signal — and it would not help across a crash
  anyway, since the re-run would mint a different UUID. Accepted cost: transient
  PayPal blips on a bare `client.post()` turn into manual reconciliation. The
  wrappers are what remove that cost, by always supplying a persisted id.
- **Decided** (Elisa's "modalità più rigorosa", refined): a mutating request with
  no `request_id` is *reported* — structured `logger.warning` on
  `paypal_checkout.client` — and `PAYPAL['STRICT_IDEMPOTENCY'] = True` promotes it
  to `PayPalIdempotencyError`, raised **before** anything is sent (not even the
  token request).
- **Target state: strict on in *every* environment, production included.** An
  earlier draft of the docs said "use it everywhere except production" — that was
  wrong, and Elisa caught it: CI would then validate a guarantee production does
  not have, and the divergence is itself the bug. The warning is a **migration
  phase**, not the production posture. Sequence: warning everywhere (now) →
  strict in tests/CI from day one → strict in production once every call path
  supplies a persisted id (M2 wrappers) → turning it off only as a temporary,
  *alerted* measure. `STRICT_IDEMPOTENCY` defaults to `False` only because those
  wrappers don't exist yet; **flip the default to `True` before 0.1.0** (free
  now, breaking later — nothing is released).
- **Observability, not prose.** A plain warning gets filtered and ignored, so the
  record carries `paypal_method`, `paypal_endpoint`, `paypal_issue` as
  `LogRecord` attributes, ready to drive a metric. `paypal_endpoint` is
  templated and id-free (`/v2/checkout/orders/{id}/capture` via
  `client.endpoint_label`), so it is low-cardinality and holds no
  per-transaction identifier. Body, credentials, headers and query string are
  never logged — pinned by a test that asserts a card number, the client secret,
  the client id and a query value are all absent from the record.

  Two deviations from the original proposal, both deliberate:
  - **Not coupled to `max_retries` as a validity condition.** Making a call
    *invalid* because of a transport tuning knob means the same code raises in
    one project's settings and works in another's; bumping `MAX_RETRIES` from 0
    to 2 would break working call sites. The knob does decide *relevance*
    though, so the diagnostic is silent at `max_retries == 0` — there is no
    retry for the missing key to endanger. Right instinct, different lever.
  - **Off by default, not fail-fast in production.** "Fail immediately rather
    than reconcile later" holds when the alternative is an *unsafe retry*; here
    the alternative is a *single* attempt, which is always safe. Refusing to
    attempt a capture at all converts ~99% success into 100% failure. So it is
    a dev/CI lint, not a runtime policy.
- **Per-operation policy replaces the HTTP heuristic (agreed direction).**
  "Mutating method ⇒ needs a key" is a heuristic, and
  `/v1/notifications/verify-webhook-signature` (a side-effect-free POST that M3
  calls) is the proof: strict mode would flag a false positive. The end state is
  an explicit policy attached to the *operation*, not inferred from the verb:

  ```python
  class Idempotency(Enum):
      REQUIRED        # capture, refund, order create — strict mode enforces a key
      OPTIONAL        # a key helps but its absence is not a defect
      NOT_APPLICABLE  # side-effect-free POST (verify-webhook-signature)
  ```

  Each wrapper function declares its own value; `client.request()` accepts it as
  a parameter, and the method heuristic stays only as the fallback for callers
  driving the raw client. Land it together with the wrappers, since declaring
  policies before there is anything to declare them on would be unused API.
  Note this is also what makes "strict on in production" safe: without it, a
  caller's harmless POST would be refused.

Work items:
- [x] **`money.py`** (2026-07-28) — `format_amount` / `parse_amount` /
      `amount_payload` / `parse_amount_payload`, `PayPalAmountError`. Rejects
      floats outright, refuses to *drop* precision (padding `10.1` → `"10.10"` is
      fine; `10.005` in EUR raises — rounding is the caller's decision, not a
      silent one). Zero-decimal set `{HUF, JPY, TWD}` **verified against PayPal's
      currency-codes reference on 2026-07-28**; no PayPal currency has 3
      decimals; unknown currencies default to 2 decimals rather than being
      rejected, so a currency PayPal adds later doesn't need a release here.
- [x] **`Idempotency` enum + `client.request(..., idempotency=...)`** (2026-07-28).
      `NOT_APPLICABLE` makes a write retryable with no key *and* exempt from the
      report — this is what makes "strict in production" viable instead of a
      false-positive factory. `OPTIONAL` silences the report but does **not**
      loosen retry safety (pinned by a test). `REQUIRED` is flagged even on a
      safe method, since the policy describes the operation, not the verb. The
      HTTP heuristic is now only the fallback when nothing is declared.
- [x] **`models.py` + `0001_initial` + `admin.py`** (2026-07-28). `PayPalOrder` and
      `Capture`, both with a local-only `INITIATED` status so a row can exist
      *before* PayPal is called — that is what makes an interrupted operation
      discoverable instead of lost. `PayPalOrder.objects.start()` and
      `order.start_capture()` write the row and its key in one transaction (the
      key derives from the pk, which only exists after the insert).
      `order.start_capture()` **reuses** an unconfirmed attempt rather than
      duplicating it (recovery reuses the key, because it may have reached
      PayPal) but gives a *new* attempt after a decline its own row and key.
      The key is **stored**, not recomputed, so a future change to the naming
      scheme cannot hand an in-flight recovery a different key. `live` is on the
      row: sandbox and live records must never be read as interchangeable.
      Generic FK (`object_id` is a Char, so UUID pks work) + `for_target()`.
      Admin is deliberately read-only — a window for support ("which key did we
      send?"), never an editor of payment state.
- [ ] create / show / capture / authorize, with the `request_id` scheme above
      generated and persisted by the wrapper, never by the caller
- [ ] signals (`payment_captured`, `payment_denied`, `payment_refunded`)
- [ ] flip `STRICT_IDEMPOTENCY` to default `True` (before 0.1.0; now gated only
      on the wrappers, since the enum has landed)
- [ ] template tag for the JS SDK v6 script + button container
- [ ] `example/` demo: cart → button → capture → order marked paid

### M3 — webhooks
- [ ] offline verification + API fallback, `WebhookEvent` model, dedupe
- [ ] `ProcessWebhookView` + handler registry → signals
- [ ] docs: how to register the webhook and get its id into settings
- [ ] tests with captured real payloads + tampered-signature cases

### M4 — refunds & reconciliation
- [ ] refunds (full/partial), `Refund` model, `payment_refunded` signal
- [ ] management command to re-sync an order/capture from PayPal

**← v0.1 released here** (decision 2: M0–M4 is the first release)

### M5 — subscriptions
- [ ] products + plans catalog, subscription create/activate/cancel/suspend
- [ ] `Subscription` model, lifecycle signals, docs

### M6 — later / maybe
- [ ] Vault v3 (saved payment methods) — US-only, check we need it
- [ ] ACDC / Card Fields (needs merchant onboarding, more compliance surface)
- [ ] migration guide from `django-paypal` IPN → webhooks (run in parallel, then
      cut over)

## Open questions

Still open, but none of them block M0/M1:

- Is this library driven by a concrete project (which one, and which flows does it
  actually need)? Doesn't change the milestone order any more, but it decides how
  opinionated the `example/` demo and the docs should be.
- Currencies and locales in scope — EUR only, or multi-currency from the start?
  Affects how much of the amount-scale logic is worth generalising in M2.
- DRF integration (serializers/viewsets) or plain Django views only?
- Sandbox credentials for `example/` and for manual testing — who provisions them?

## Stato

- [x] Landscape and API surface verified against current PayPal docs/PyPI (2026-07-28).
- [x] Architecture and milestones drafted (this file).
- [x] Decisions 1–5 taken with Elisa (2026-07-28); PyPI name availability verified.
- [x] **M0 skeleton done** (2026-07-28), verified locally: install, tests, build,
      `twine check`, docs, YAML all green. Committed on `main` as
      `8dc2463 chore: project skeleton (M0)` (32 files). `CLAUDE.md` and
      `.claude/` are gitignored.
- [x] **M1 client + auth done** (2026-07-28), 100% covered, verified on both
      ends of the CI matrix.
- [ ] Create the GitHub repo `otto-torino/dj-paypal-checkout`, set up PyPI
      Trusted Publishing and the `CODECOV_TOKEN` secret (CI uploads coverage
      with `fail_ci_if_error: true`, so it fails until the token exists).
- [ ] Consider renaming the working directory to `dj-paypal-checkout/`
      (currently `django-paypal/`, which no longer matches the package).
- [ ] M2 — one-off payments (Orders v2). Not started. First steps: `money.py`
      (Decimal ↔ PayPal amount strings), `orders.py`, then the models.

*Reminder: semantic commits, subject only, no body, no Co-Authored-By trailer.*
