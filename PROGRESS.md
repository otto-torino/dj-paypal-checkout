# Progress — modern PayPal integration library for Django

Status: **0.1.0 released** — M0–M4 done and on PyPI. Started and brought here on
2026-07-28.

- Repo: https://github.com/otto-torino/dj-paypal-checkout (public)
- Docs: https://dj-paypal-checkout.readthedocs.io (live, badge *passing*)
- PyPI: https://pypi.org/project/dj-paypal-checkout/ — 0.1.0, wheel + sdist,
  tag `v0.1.0` pushed. The name is now ours.
- 418 tests, **100% coverage incl. branches** (enforced by `fail_under = 100`),
  green on py3.11–3.14 across Django 5.2 and 6.0
- Working directory is still `django-paypal/`, which no longer matches the
  package name. Cosmetic only.

Shipped: config, OAuth2 auth with token caching, sync/async clients, amounts,
models with persisted idempotency keys, Orders v2 create/authorize/capture,
refunds and voids, verified webhooks with an atomic claim, a reconciliation
command, signals, read-only admin, JS SDK v6 tags, runnable demo.
Not shipped: subscriptions (M5), Vault and Card Fields (M6).

Goal: a maintained, REST-first PayPal library for Django — Orders v2 checkout,
subscriptions, refunds and **webhooks** — replacing the IPN/PDT-era tooling.
Packaged and released like the other Otto libraries (`dj-editor-js`,
`django-baton`).

*This file is a running log: the sections below are in milestone order and keep
the reasoning behind each decision, including the ones that turned out wrong.*

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
- [x] **`orders.py` + `signals.py`** (2026-07-28) — `create_order`, `refresh_order`,
      `fetch_order`, `capture_order` (full and partial). Each writes its row
      first, passes that row's persisted key and declares
      `Idempotency.REQUIRED`, so strict mode is satisfied *by construction* — a
      test asserts create+capture run clean under `STRICT_IDEMPOTENCY=True`.
      Amounts are always built here from the caller's figures, never read back
      from the browser. Signals `payment_captured` / `payment_denied` fire from
      the capture outcome (`PENDING` sends nothing — not an outcome yet).
      Wrappers are **sync only**: the ORM helpers run in a transaction and async
      versions need more than adding `await`. The async *client* is unaffected.
      - Guard worth noting: when the caller supplies `purchase_units`, their
        total must match the recorded `amount` or the call is refused *before*
        any HTTP request and before the row is written — otherwise the row could
        say EUR 10 while the buyer is charged EUR 100. Skipped when the units
        can't be read (other currency, missing amount), so a partial check never
        becomes a false rejection.
      - When a capture response contains no capture object, the attempt is left
        `INITIATED` and a structured warning is logged: money may have moved, and
        recording a guess would be worse than recording "unknown".
- [x] **`authorize` + `Authorization` model** (2026-07-28, migration `0002`).
      `authorize_order` (hold) and `capture_authorization` (take), same
      persisted-key and recovery rules as captures; key scheme
      `order:<pk>:authorize:<auth_pk>` — **per attempt**, deviating from the
      literal `order:<pk>:authorize` for the reason Elisa gave about captures: an
      authorization can be denied, and a fixed key would make PayPal replay that
      denial for ever. `Capture.authorization` distinguishes the two capture
      paths, and the two pending pools are kept separate so a recovery can never
      mistake a direct order capture for an authorization capture. Note the
      asymmetry in PayPal's API, handled here: capturing an *order* answers with
      the order (capture nested), capturing an *authorization* answers with the
      capture itself. `expires_at` holds PayPal's hold expiry, parsed leniently
      (an unparseable date is ignored, never fatal).
- [ ] `void_authorization` — not done; releasing a hold belongs with the refund
      work in M4.
- [ ] flip `STRICT_IDEMPOTENCY` to default `True` (before 0.1.0; now gated only
      on the wrappers, since the enum has landed)
- [x] **Template tags for the JS SDK v6** (2026-07-28) — `{% paypal_sdk %}`
      (script + config), `{% paypal_sdk_url %}`, `{% paypal_client_id %}`.
      Verified against PayPal's v6 docs: the SDK is loaded from
      **`/web-sdk/v6/core`** (v5's `/sdk/js` is a different generation) on
      different hosts for sandbox and live, and initialised with
      `createInstance({clientId, components})` — a plain client id is enough for
      basic checkout; a server-issued client token is only needed for
      Fastlane/vaulting (M6). Config travels through `|json_script`, so a value
      containing `</script>` cannot break out — pinned by a test. Only public
      values are emitted; a test asserts the secret never appears.
      - Dropped a `paypal_sdk_config_json` tag I had written: a simple_tag
        returning a JSON *string* is HTML-escaped by autoescaping, and marking it
        safe inside `<script>` is exactly the hole `json_script` closes. Replaced
        by an importable `sdk_config()` helper for views.
      - The tags deliberately stop at loading the SDK. Wiring buttons to
        create/capture endpoints is application code (URLs, CSRF, error
        handling), and belongs in the demo rather than in a half-right tag.
- [x] **`example/` demo + `run_demo.sh`** (2026-07-28) — cart → JS SDK v6 button →
      create → approve → capture → order marked paid by a signal receiver. Runs
      with `STRICT_IDEMPOTENCY: True` to prove the helpers satisfy the target
      posture. The checkout page lists the local `PayPalOrder` rows with their
      keys, so you can watch a row appear *before* PayPal is called.
      Smoke-verified: page renders 200, loads `/web-sdk/v6/core`, exposes the
      client id, renders the server-side amount, and **does not leak the secret**.
      Sandbox credentials come from the environment; `run_demo.sh` refuses to
      start without them.
      - No `run_demo.bat` (the sibling repos have one): I can't test a Windows
        script here, and a broken one is worse than none. Follow-up if wanted.

### M3 — webhooks ✅ done 2026-07-28
- [x] **`webhooks/verify.py`** — offline RSA-SHA256 over
      `transmission_id|transmission_time|webhook_id|crc32(raw_body)` (algorithm
      re-checked against PayPal's docs on 2026-07-28), plus the
      `/v1/notifications/verify-webhook-signature` path. `PAYPAL-CERT-URL` is
      validated as an HTTPS paypal.com host **before** anything is fetched —
      without that a forged header points the verifier at the attacker's
      certificate and every forgery passes; certificate cached for a day.
      `signed_message()` refuses a non-bytes body, so the classic
      re-serialisation bug cannot be written by accident.
- [x] **No "offline then API" fallback**, deliberately: a signature that fails
      must be rejected, never re-checked by a method that might say yes. The
      fallback that *does* exist is only for "verification could not be
      attempted" (no `cryptography`, cert unreachable) → refuse. Mode is
      `PAYPAL['WEBHOOK_VERIFY_MODE']`, and `"auto"` is rejected by config
      validation so nobody can ask for the unsafe thing.
- [x] **`WebhookEvent` + dedupe** (migration `0003`). Unique `event_id` stops
      double-processing; `processed_at` is the other half — a stored but
      unprocessed row is *unfinished work*, not a duplicate to skip, so a retry
      re-runs it. Same shape as the unconfirmed-capture rule.
- [x] **`ProcessWebhookView`** with deliberate status codes: `400` untrustworthy
      (nothing stored), `200` stored *and* finished (also for a true duplicate),
      `500` stored but unfinished → PayPal retries. Answering `200` on a handler
      failure would drop a payment confirmation for good.
      - The race is now safe: if a webhook overtakes our own capture response the
        handler raises `PayPalWebhookNotReady` → `500` → PayPal's retry finds the
        row and succeeds. Free reconciliation. A capture we don't know *and*
        whose order we don't know is someone else's integration → ignored.
- [x] **Handler registry** → the existing signals, for capture
      completed/denied/pending/refunded, order approved/completed and
      authorization created/voided. Unhandled types are stored and acked, not
      errors. `register_handler` is public for project-specific events.
- [x] Read-only `WebhookEventAdmin` (processed flag, `last_error`, transmission id).
- [x] Tests: **337 total, 100% coverage incl. branches.** Verification is tested
      with a **real RSA key and certificate** generated in-process, not a stubbed
      "signature ok": genuine signature passes; tampered body, tampered
      signature, non-base64 signature, another webhook id and a replayed
      transmission id all fail; `paypal.com.evil.example` is refused.
- [x] Docs: new `webhooks.rst` (setup, the two modes, the status-code contract,
      custom handlers, reconciling), plus the endpoint wired into the demo.

#### M3 review follow-ups (Elisa, 2026-07-28) — all three applied

- [x] **`400` was documented wrong, and it was my error.** PayPal retries *any*
      non-2xx (~25 attempts over 3 days), so "nothing is retried" was false.
      Re-worded everywhere (`views.py`, `webhooks.rst`, `CLAUDE.md`): the status
      code does not control retrying, it records what we did — `400` is "not
      trustworthy, **not persisted**", and PayPal re-asking only gets the same
      answer, which is the desired outcome for a forgery.
- [x] **Cert URL hardening**, beyond the anti-`paypal.com.evil.example` check:
      credentials in the URL refused, port must be 443 (a malformed port is a
      clean error, not a crash), **redirects not followed** (anything but `200`
      refused — one hop off a validated host would defeat the whole check),
      body capped at 64 KiB (the URL is attacker-supplied, so an unbounded read
      is a memory DoS), empty body refused, and the cache key **hashed** so a
      hostile URL cannot become a backend-hostile key verbatim.
- [x] **Atomic claim instead of a read.** Ownership is now taken with
      `UPDATE ... WHERE processed_at IS NULL` inside the handlers' transaction:
      the write lock serialises rivals (the loser matches 0 rows → `duplicate`),
      `processed_at` commits *together with* the handler effects, and a failure
      rolls the claim back while `last_error` is written outside the transaction
      and survives. Also handles losing the `get_or_create` race (`IntegrityError`
      → re-fetch), and an identical redelivery no longer costs a pointless write.
- [x] **Real concurrency tests.** Two threads with a barrier delivering the same
      event: the handler body runs **exactly once** and exactly one delivery is
      told `processed`. Plus deterministic tests for the claim being taken
      *before* dispatch, the claim surviving as rollback-able on failure, and
      `processed_at` being visible inside the handler's transaction.
      - This forced a real change to the harness: `tests/test_settings.py` now
        uses a **file** test DB with `timeout: 20`, because SQLite's shared-cache
        `:memory:` mode returns "table is locked" immediately instead of
        honouring the busy timeout — the first version of the threaded test
        "passed" with *both* threads erroring before reaching the handler, which
        is exactly the false confidence Elisa warned about. Suite cost: ~0.5s → ~1s.
- [x] Documented the shared-account rule explicitly (`webhooks.rst`): unknown
      capture/order/authorization → ignored and acked; unknown capture whose
      `related_ids.order_id` *is* ours → retry, not a foreign payment.
- [x] `unregister_handlers()` added (a project replacing a built-in handler needs
      it; the earlier test cleanup was a silent no-op because `get_handlers()`
      returns a copy).

### M4 — refunds & reconciliation ✅ done 2026-07-28
- [x] **`Refund` model + `payments.py`** (migration `0004`) — `refund_capture`
      (full and partial) and `void_authorization`. Key scheme
      `order:<pk>:refund:<refund_pk>`, per refund, so two partial refunds are two
      operations rather than one replayed twice.
- [x] **A refund past the captured amount is refused locally**, before the row is
      written, counting refunds whose outcome we do not know
      (`reserved_refund_amount` includes `INITIATED` — it may have reached
      PayPal). PayPal would refuse it too, but only afterwards, by which time the
      local row would claim money was returned that never was. `refunded_amount`
      (completed only) and `refundable_amount` are exposed for callers.
      A completed refund syncs the capture to `REFUNDED`/`PARTIALLY_REFUNDED`.
- [x] **`void_authorization`** — the one key that is *not* per attempt and *not*
      stored (`void_request_id`), documented as a deliberate exception: voiding
      is single-shot, so there is no attempt dimension, and PayPal refuses a
      second void rather than repeating it, so key drift cannot move money.
      **Worth a second opinion**, since it breaks the "store the key" rule.
- [x] **Fixed a real bug found while writing this**: the M3 handler for
      `PAYMENT.CAPTURE.REFUNDED` treated `resource.id` as a *capture* id, but for
      that event the resource is the **refund**. It now resolves the capture via
      `related_ids.capture_id` (falling back to the `up` link), and **adopts** a
      refund it has never seen — which is how a refund issued straight from the
      PayPal dashboard now lands in the local records.
- [x] **`paypal_sync` management command + `reconcile_order`** — the way out of
      "unconfirmed for ever". An interrupted attempt has no PayPal id, so it
      cannot be fetched directly; re-reading the *order* reveals whether the
      capture happened, and the local row is settled (signals included).
      Adoption is conservative on purpose: only one unconfirmed attempt against
      one unmatched PayPal capture. Anything more ambiguous is reported for a
      person — guessing which attempt became which capture is not something to do
      silently with money. Flags: `--order`, `--unconfirmed`, `--since`,
      `--limit`, `--dry-run`.
- [x] **`STRICT_IDEMPOTENCY` now defaults to `True`** — the gate (all money-moving
      wrappers supplying persisted keys) is met. `tests/support.make_config()`
      forces it off explicitly, since most tests exercise the raw client's lenient
      path; separate tests assert the shipped default.
- [x] Read-only `RefundAdmin` + inline on captures; `refunded_amount` in the
      capture list.
- [x] 418 tests, **100% coverage incl. branches**.

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
- [x] **M2 essentially done** (2026-07-28): money, idempotency policy, models,
      orders (create/show/capture), authorize + authorization capture, signals,
      admin, JS SDK v6 tags, runnable demo. **262 tests, 100% coverage incl.
      branches**, green on py3.14/Django 6.0 *and* py3.12/Django 5.2, docs at 0
      warnings, wheel verified (templates/templatetags/migrations packaged,
      `example/` excluded). Left in M2: `void_authorization` (moved to M4 with
      the refund work) and the `STRICT_IDEMPOTENCY` default flip (after M4).
- [x] GitHub repo, CI and publishing set up — see *Release prerequisites* below.
      (The `CODECOV_TOKEN` this line used to demand turned out to be unnecessary.)
- [ ] Consider renaming the working directory to `dj-paypal-checkout/`
      (currently `django-paypal/`, which no longer matches the package). Purely
      cosmetic: git and the remote do not care.
- [x] **M3 webhooks done** (2026-07-28). 337 tests, 100% coverage, verification
      tested against real RSA signatures.
- [x] **M4 done** (2026-07-28): refunds, voids, reconciliation command, strict
      default flipped. 418 tests, 100% coverage.
- [x] **0.1.0 cut** (2026-07-28), after the RTD import went green — Elisa's
      sequencing, so the PyPI page is born linking to documentation that works.
      `version` and `__version__` both `0.1.0` (the test that pins them to each
      other passes), `Development Status` moved from Pre-Alpha to Alpha, README
      status rewritten — including the honest caveat that it has not yet run
      against live PayPal traffic. Local build + `twine check` PASSED on both
      artifacts.
- [x] **Published** (2026-07-28). Elisa approved the `pypi` environment, the run
      succeeded, the pending publisher converted into a normal one and the project
      was created. Verified from the PyPI JSON API: version `0.1.0`,
      `requires_python >=3.11`, license MIT, `Development Status :: 3 - Alpha`,
      Django 5.2/6.0 classifiers, both artifacts present (wheel 51.9 kB, sdist
      74.0 kB), dependencies `Django>=5.2`, `httpx>=0.27` and
      `cryptography>=42.0; extra == "crypto"`, and the Documentation URL pointing
      at the live RTD site. Tag `v0.1.0` on the remote.
- [x] **Environment-gate side effect fixed** (2026-07-28). The gate applied to the
      *whole job*, which sat in front of the version check, so **every** push to
      `main` queued an approval request — even ones that would immediately skip.
      Four stale runs piled up and were cancelled by hand; the danger is not the
      noise but that five identical pending approvals invite clicking the wrong
      one. `publish.yml` is now two jobs: `check` (no environment) reads the
      version and outputs whether this push is a release; `publish`
      (`needs: check`, `if:`, `environment: pypi`) does the work. An ordinary push
      now finishes clean and only a real release waits for a human.
      The refactor was held back until after the release on purpose, so there was
      exactly one pending approval to act on rather than two runs for one version.

#### Release prerequisites (2026-07-28)

- [x] **Repo created and pushed**: https://github.com/otto-torino/dj-paypal-checkout
      — **public**, like the sibling libraries (MIT, destined for PyPI, and the
      README badges point at public URLs). Topics: django, paypal, payments,
      django-app. `main` pushed, 16 commits.
- [x] **First CI run green**: all four matrix jobs plus `docs`. Two things proved
      themselves in production rather than only locally:
      - `publish.yml` ran and **skipped** — "Version is the 0.0.0 placeholder —
        nothing released yet, skipping publish." The guard I added instead of
        copying `dj-editor-js` verbatim is what stopped an empty package from
        going to PyPI on the very first push.
      - the **Codecov upload succeeded with no token**, confirming `use_oidc`.
- [x] **PyPI pending publisher created by Elisa** (2026-07-28). A pending publisher
      is the answer to "how do I enable trusted publishing before the project
      exists": it lives in account settings
      (https://pypi.org/manage/account/publishing/), *not* on a project page, and
      on the first successful publish it converts into a normal publisher and
      **creates** the project. The name is not reserved until then.
      The claim our workflow sends, for reference if it ever needs re-checking:
      repository `otto-torino/dj-paypal-checkout`, workflow `publish.yml`,
      environment `pypi`.
- [x] **`pypi` environment protected** (2026-07-28). It already existed — GitHub
      auto-creates an environment a job references — but with
      `protection_rules: []`, i.e. no gate at all: the OIDC claim would have
      matched and a version bump would have published immediately. Now it
      requires a review from `elisarubin`, so a release waits for a human click
      even if a bump reaches `main` by accident. Notes: `can_admins_bypass` is
      left `true` (avoids a lockout, and admins bypassing is a deliberate act),
      no branch policy is set, and protection rules are free here only because the
      repo is public. Add a team as reviewer if one person is too narrow.
      ⚠️ The environment name must match on both sides: change one, change both.
- [x] **No `CODECOV_TOKEN` at all.** I had copied `token: ${{ secrets.CODECOV_TOKEN }}`
      from `dj-editor-js` (codecov-action v4). Elisa pointed at
      `www/django-copier`, which uploads with **`use_oidc: true`** on
      codecov-action **v7** — GitHub's OIDC identity authenticates the upload, so
      there is no secret to store or rotate. Adopted, together with that repo's
      SHA-pinning of actions. The job declares `id-token: write`.
- [x] **The real coverage gate is `fail_under = 100` in `.coveragerc`**, enforced
      by a `coverage report` step rather than by Codecov: stronger than a
      dashboard and independent of any external service. Verified that it bites —
      a partial run exits 2. Codecov is now purely the badge.
- [x] Actions pinned by SHA in both workflows (`checkout` v5, `setup-python` v6,
      `codecov-action` v7), matching `django-copier`. Exception:
      `pypa/gh-action-pypi-publish@release/v1` stays on its release branch, which
      is how PyPA documents it — I did not want to invent a SHA I could not verify.
- [x] README badges in the house style (CI, codecov, Read the Docs, Django,
      Python, PayPal), matching `django-copier`/`django-cookiecutter`. They stay
      grey until the repo, Codecov project and RTD import exist.
- [ ] **Read the Docs import — the last prerequisite, and it is manual.**
      Decision (Elisa, 2026-07-28): **import RTD first, release after**, so the
      PyPI page is born with a working documentation link and the README badge is
      valid from the start.

      On readthedocs.org: *Import a Project* → `otto-torino/dj-paypal-checkout`.
      The slug **must** be `dj-paypal-checkout`, because
      `https://dj-paypal-checkout.readthedocs.io` is already declared in
      `pyproject.toml` and in the README badge. `.readthedocs.yaml` is
      auto-detected; nothing else to configure.

      ⚠️ **The repo will not appear in the list until the Read the Docs GitHub App
      is installed on the `otto-torino` org** — RTD only lists what its App can
      see. Verified 2026-07-28: the org has `slack`, `travis-ci`, `stale`,
      `codecov`, `deploy-bot-otto` installed and **no** Read the Docs app.
      The App is **https://github.com/apps/read-the-docs-community** (for
      readthedocs.org). Note two traps: `github.com/apps/readthedocs` is an
      unrelated *private* App owned by someone else — I sent Elisa there first and
      it was a dead end — and `read-the-docs-business` is the paid platform, which
      serves docs from `.readthedocs-hosted.com` and would not match the URL we
      have already published. Install on the **org**, not a personal account, then
      re-sync the repository list on RTD.
      Elisa is an org `admin`, so no owner approval is needed; the org's
      third-party *application* policy governs OAuth apps, not GitHub Apps.

      ✅ Installed 2026-07-28: `read-the-docs-community`,
      `repository_selection=selected`.

      Do **not** expect a per-repository webhook, and do not go looking for one: a
      GitHub App receives events on its own single endpoint for every repo in the
      installation, which is part of why RTD moved off the OAuth app. `hooks` on
      the repo staying empty is the correct state. (I predicted a webhook here
      first, reasoning from the old OAuth integration — wrong.)

      Our side is done and enforced: `.readthedocs.yaml` sets
      `sphinx.fail_on_warning: true`, and CI has a separate `docs` job running
      `sphinx-build -W --keep-going`, so a broken build fails the pull request
      instead of silently degrading on RTD.

      ✅ **Imported and green, 2026-07-28.** Verified from outside:
      `https://dj-paypal-checkout.readthedocs.io/` returns 200 and redirects to
      `/en/latest/`, all six pages are served, and the badge reads *passing*. The
      slug came out as `dj-paypal-checkout`, so the URL in `pyproject.toml` and the
      README badge are both valid.
      One trap in the wizard: it offers an example `.readthedocs.yaml` to copy.
      Do **not** — the example points `sphinx.configuration` at `docs/conf.py`
      (ours is `docs/source/conf.py`), leaves the requirements commented out and
      omits `method: pip, path: .`, without which autodoc cannot import
      `paypal_checkout` and the build fails. The right answer to that step is
      "this file exists".

      **The first RTD build was simulated faithfully before handing it over** —
      fresh Python 3.12 venv, `pip install docs/requirements.txt`, **non-editable**
      `pip install .` (as RTD does, so a docs build that only works from the
      source tree would have shown up), Sphinx **9.1** (newer than the local venv),
      `-W --keep-going`. Result: build succeeded, all 10 pages rendered, autodoc
      resolved (`PayPalOrder` and `refund_capture` present in
      `api_reference.html`), which means the Django setup in `docs/source/conf.py`
      works in a clean environment.
- [ ] M5 — subscriptions (products/plans catalog, subscription lifecycle).

*Reminder: semantic commits, subject only, no body, no Co-Authored-By trailer.*
