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

### M1 — client + auth
- [ ] `config.py`, `auth.py` (token cache), `client.py` (sync + async, retries,
      typed errors carrying PayPal `debug_id`)
- [ ] tests against recorded fixtures — no live sandbox calls in CI

### M2 — one-off payments (Orders v2)
- [ ] create / show / capture / authorize, `PayPal-Request-Id`
- [ ] `PayPalOrder`, `Capture` models (+ generic FK to the host order) + signals; admin
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
      `twine check`, docs, YAML all green. Nothing committed yet — the repo is
      initialised on `main` with everything still unstaged.
- [ ] Create the GitHub repo `otto-torino/dj-paypal-checkout`, set up PyPI
      Trusted Publishing and the `CODECOV_TOKEN` secret (CI uploads coverage).
- [ ] Consider renaming the working directory to `dj-paypal-checkout/`
      (currently `django-paypal/`, which no longer matches the package).
- [ ] M1 — client + auth. Not started.

*Reminder: semantic commits, subject only, no body, no Co-Authored-By trailer.*
