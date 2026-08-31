# Releasing

How a release happens, and what still stands between this package and a 1.0.

## How a release happens

Pushing to `main` triggers `.github/workflows/publish.yml`. It reads `version`
from `pyproject.toml` and, if no `v<version>` tag exists, builds, publishes to
PyPI via Trusted Publishing (OIDC) and creates the tag. **Bumping the version on
`main` is therefore the release.** Two brakes:

- the version `0.0.0` is treated as "never released" and skipped;
- the job runs in the `pypi` GitHub environment, whose required reviewers make
  it wait for a human approval before publishing.

A version bump touches exactly four files, and `scripts/check_release_docs.py`
(run in both CI and publish) fails the release if they disagree:

| File | What must match |
|---|---|
| `pyproject.toml` | `version = "X.Y.Z"` |
| `paypal_checkout/__init__.py` | `__version__ = "X.Y.Z"` |
| `README.md` | the literal marker `**Status: X.Y.Z.` |
| `docs/source/index.rst` | the literal marker `**Version X.Y.Z.**` |

Then: `python tests/runtests.py` (coverage gate is `fail_under = 100`),
`python scripts/check_release_docs.py`, `sphinx-build -W`.

After publishing, the PyPI badge in the README can lag by up to three hours:
shields.io sends `cache-control: s-maxage=10800` and GitHub serves the image
through its Camo proxy, which honours it. The badge is orange because shields
colours any `0.x` or prerelease version orange; it turns blue by itself at 1.0.

## Maturity ladder

Two independent axes that are easy to conflate: the **classifier** describes how
finished the code is, the **version** describes how binding the API is.

### Next release — `3 - Alpha` → `4 - Beta`

By the classifier's usual meaning 0.4.0 already qualifies: complete within its
declared scope (no docs section describes an unimplemented part any more), 579
tests at 100% coverage including branches, refund concurrency verified against a
real Postgres in CI. Beta does not claim production use.

- [ ] `pyproject.toml`: `Development Status :: 3 - Alpha` → `4 - Beta`
- [ ] keep both blurbs honest: the "not been run against live PayPal traffic"
      caveat and the "API may still change on minor versions" clause stay

### Before 1.0 — chores

- [ ] **CHANGELOG.** There is none. Reconstruct the entries for `v0.1.0`–`v0.4.0`
      from the tags, then keep it per release.
- [ ] **Deprecation policy and support window**, written down: which Django and
      Python versions are supported (currently Django 5.2/6.0, Python
      3.11–3.14), and how something gets removed — warn for one minor, drop on
      the next major. At 1.0 the "may change on minor versions" escape hatch
      disappears, so this has to exist *before*, not after.
- [ ] **Decide the fate of legacy `sent_body=None` refund rows.** `retry_refund`
      refuses them and `merge_refund_attempt` exists only to rescue them; they
      can only exist in installs that ran 0.3.0 or earlier. Either keep the
      escape hatch permanently, or document a data migration and remove it.
- [ ] **Consider squashing migrations** `0001`–`0007`.
- [ ] **Run the whole suite on Postgres**, not only `tests.test_refund_concurrency`
      (`PAYPAL_TEST_POSTGRES=1`).
- [ ] Decide whether async wrappers over `orders`/`payments`/`subscriptions`/
      `vault` are ever wanted. Not a blocker: they are additive and can land in a
      1.x. The client already has the full async surface.

### Before 1.0 — what only live traffic can prove

This is the one gate that cannot be closed by writing code. Fixtures cannot
exercise PayPal's real certificates, real delivery timing or real retry
behaviour. Run a real project on a live merchant account with small amounts for
a few weeks, and confirm each of these:

**Verification and webhooks**
- [ ] Offline RSA-SHA256 verification succeeds against the certificates PayPal
      actually serves — the whole `verify.py` path, on real bytes.
- [ ] The real `PAYPAL-CERT-URL` values satisfy `validate_cert_url()` (https,
      `*.paypal.com`, port 443, no redirects, under the 64 KiB cap). A false
      *rejection* here is as bad as a false acceptance.
- [ ] The API verification mode works too, with the same events.
- [ ] A handler that raises produces a `500` and PayPal really redelivers; the
      stored-but-unprocessed event re-runs on the retry and is not mistaken for
      a duplicate.
- [ ] The "webhook overtook our capture response" race
      (`PayPalWebhookNotReady`) resolves on redelivery. Hard to force
      deliberately — watch for it rather than engineer it.
- [ ] No middleware in the host project consumes the request body.

**Money paths**
- [ ] Capture, partial refund, full refund, void of an authorization.
- [ ] Retrying a capture with the *same* `PayPal-Request-Id` makes PayPal replay
      its first response instead of charging twice.
- [ ] An interrupted refund for real (kill the process or block egress
      mid-call), then `retry_refund`: PayPal replays the same refund id and the
      rows merge. This is the 0.4.0 headline feature and it has never met a real
      timeout.
- [ ] `paypal_sync --unconfirmed` against real data, including its non-zero exit
      while a refund is unresolved.
- [ ] A zero-decimal currency (JPY) end to end, if the account supports it — the
      scale rules are only ever exercised locally.

**Subscriptions and Vault**
- [ ] Product → plan → activate → subscription → the first recurring payment
      lands as a `SubscriptionPayment` via `PAYMENT.SALE.COMPLETED`; then
      suspend, resume, cancel.
- [ ] Setup token → payment token → list → delete.

**Operational**
- [ ] Token caching behaves under a real cache backend (Redis, shared between
      processes), not `locmem`.
- [ ] Real logs carry the structured attributes and leak nothing: no body, no
      credentials, no headers, no query string, no PAN.

### At 1.0

- [ ] `Development Status :: 4 - Beta` → `5 - Production/Stable`
- [ ] drop "the API may still change on minor versions before 1.0" from
      `README.md` and `docs/source/index.rst`
- [ ] drop the "has not been run against live PayPal traffic" caveat — only once
      that is actually false
- [ ] the shields badge turns blue on its own; nothing to change
