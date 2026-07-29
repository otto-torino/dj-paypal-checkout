# dj-paypal-checkout

[![CI](https://github.com/otto-torino/dj-paypal-checkout/actions/workflows/ci.yml/badge.svg)](https://github.com/otto-torino/dj-paypal-checkout/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/otto-torino/dj-paypal-checkout/branch/main/graph/badge.svg)](https://codecov.io/gh/otto-torino/dj-paypal-checkout)
[![Documentation](https://readthedocs.org/projects/dj-paypal-checkout/badge/?version=latest)](https://dj-paypal-checkout.readthedocs.io/)
[![PyPI](https://img.shields.io/pypi/v/dj-paypal-checkout?logo=pypi&logoColor=white)](https://pypi.org/project/dj-paypal-checkout/)
[![Django 5.2 | 6.0](https://img.shields.io/badge/Django-5.2%20%7C%206.0-092E20?logo=django&logoColor=white)](https://docs.djangoproject.com/en/stable/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://docs.python.org/3/)
[![PayPal REST](https://img.shields.io/badge/PayPal-Orders%20v2-003087?logo=paypal&logoColor=white)](https://developer.paypal.com/docs/api/orders/v2/)

A modern, REST-first PayPal integration for Django: **Orders v2** checkout,
refunds and **verified webhooks**, with models, signals and admin.

> **Status: 0.3.0.** One-off payments are covered end to end:
> configuration, OAuth2 auth with token caching, sync/async HTTP clients, amount
> handling, models with persisted idempotency keys, the Orders v2
> create/authorize/capture flows, refunds and voids, verified webhooks, a
> reconciliation command, signals, a read-only admin and a runnable demo.
> Subscriptions add the products/plans catalog, create/revise and lifecycle
> operations, verified lifecycle/payment webhooks and recurring-payment records.
> Payment Method Tokens v3 adds setup/payment tokens, verified Vault webhooks
> and local audit records.
> Browser-side Card Fields still requires merchant enablement and application UI.
>
> It has not been run against live PayPal traffic yet, and the API may still
> change on minor versions before 1.0. See [PROGRESS.md](PROGRESS.md).

## Why another PayPal library?

The established `django-paypal` package is built on **Payments Standard with
IPN/PDT**, i.e. PayPal's Classic stack. PayPal now recommends webhooks for all
new integrations and IPN is not fired by newer payment products. Meanwhile
PayPal's own `paypal-server-sdk` is sync-only and ships neither webhook
signature verification nor the subscription plans/products catalog.

This library targets the current REST APIs and fills those gaps:

| Area | API / implementation |
|---|---|
| Checkout | Orders v2 (create → approve → capture) |
| Captures/refunds | Payments v2, with a local guard against over-refunding |
| Notifications | Webhooks with RSA-SHA256 signature verification — no IPN |
| Client side | JS SDK **v6** loader; checkout UI remains application policy |
| Subscriptions | Subscriptions v1 + plans/products catalog and lifecycle webhooks |
| Saved methods | Payment Method Tokens v3 (setup tokens → permanent vault tokens) |
| Async | sync **and** async client, same surface |

## Design principles

- **The server owns the amount.** It is computed from your own order; the
  browser only ever receives a PayPal order id.
- **Webhooks are the source of truth** for money having moved, and handlers
  are idempotent — PayPal retries, and events can arrive more than once.
- **Writes are idempotent**, via `PayPal-Request-Id`, so a retry cannot
  double-charge.
- **`Decimal` end to end**, with currency-correct scale (never float).
- **One config entry point**: a single `PAYPAL` settings dict, read only by
  `paypal_checkout.config`.
- **The DB is a local cache of PayPal state** — concrete models plus a generic
  FK to your own order object, so admin, audit and re-sync work out of the box.

## Requirements

- Python 3.11+
- Django 5.2 LTS or 6.0

## Try it

`example/` is a runnable sandbox checkout — two endpoints, signals, and a
read-only admin:

```bash
cp example/.env.example example/.env
# Edit example/.env with the client id and secret of a PayPal sandbox REST app.
./run_demo.sh                    # http://127.0.0.1:8000/
```

`run_demo.sh` loads `example/.env` automatically. The file is ignored by Git
and must never contain live credentials. `PAYPAL_WEBHOOK_ID` is optional for
the synchronous checkout and is only needed to test verified webhook delivery.
The demo also sets Django's `SECURE_CROSS_ORIGIN_OPENER_POLICY` to
`"same-origin-allow-popups"`, as required for the cross-origin PayPal popup to
communicate with its opener.

## Development

```bash
# Run the test suite (custom runner, uses tests/test_settings.py)
python tests/runtests.py

# Coverage (what CI runs; fails below 100% via .coveragerc)
coverage run tests/runtests.py && coverage report -m

# Docs the way CI and Read the Docs build them (warnings are errors)
sphinx-build -W --keep-going -b html docs/source docs/build/html

```

Invoke tasks are available too: `invoke test`, `invoke coverage`,
`invoke docs`, `invoke clean`.

## License

MIT — see [LICENSE](LICENSE).
