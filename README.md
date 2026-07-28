# dj-paypal-checkout

A modern, REST-first PayPal integration for Django: **Orders v2** checkout,
refunds and **verified webhooks**, with models, signals and admin.

> **Status: in development — nothing released yet.**
> Feature-complete for a 0.1.0: configuration, OAuth2 auth with token caching,
> sync/async HTTP clients, amount handling, models with persisted idempotency
> keys, the Orders v2 create/authorize/capture flows, refunds and voids, verified
> webhooks, a reconciliation command, signals, a read-only admin and a runnable
> demo. Not yet: subscriptions, Vault, Card Fields.
> See [PROGRESS.md](PROGRESS.md). Do not use it in production; the API will
> change until 0.1.0.

## Why another PayPal library?

The established `django-paypal` package is built on **Payments Standard with
IPN/PDT**, i.e. PayPal's Classic stack. PayPal now recommends webhooks for all
new integrations and IPN is not fired by newer payment products. Meanwhile
PayPal's own `paypal-server-sdk` is sync-only and ships neither webhook
signature verification nor the subscription plans/products catalog.

This library targets the current REST APIs and fills those gaps:

| | |
|---|---|
| Checkout | Orders v2 (create → approve → capture) |
| Captures/refunds | Payments v2, with a local guard against over-refunding |
| Notifications | Webhooks with RSA-SHA256 signature verification — no IPN |
| Client side | JS SDK **v6** (standalone buttons, Card Fields) |
| Subscriptions | Subscriptions v1 + plans/products catalog *(after 0.1.0)* |
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
export PAYPAL_CLIENT_ID=...      # sandbox REST app credentials
export PAYPAL_CLIENT_SECRET=...
./run_demo.sh                    # http://127.0.0.1:8000/
```

## Development

```bash
# Run the test suite (custom runner, uses tests/test_settings.py)
python tests/runtests.py

# Coverage (what CI runs)
coverage run tests/runtests.py && coverage report -m

# Build the docs
sphinx-build -E -b html docs/source docs/build/html
```

Invoke tasks are available too: `invoke test`, `invoke coverage`,
`invoke docs`, `invoke clean`.

## License

MIT — see [LICENSE](LICENSE).
