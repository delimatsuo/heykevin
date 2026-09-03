# Runbook: replay missed App Store Server Notifications

## When to use this

Every Apple App Store Server Notification was answered HTTP 400 for 30+
days, until PR #215 (deployed 2026-09-02) fixed the webhook's JWS signature
verification. Apple only retries a failed delivery for 72 hours, so any
renewal, expiration, cancellation, or refund that fired before the fix
never reached Firestore — affected contractors can carry a stale `active`
expiry, or a purchase that was never bound to their account.

Apple's **Get Notification History** endpoint keeps 180 days of history
regardless of delivery outcome. `scripts/replay_appstore_notifications.py`
fetches that history, verifies each `signedPayload` with the exact same
verifier the production webhook uses (`app.webhooks.appstore._decode_notification_payload`),
prints a summary, and — only with `--apply` — feeds each verified payload
to the same handler the webhook calls
(`app.services.subscription.handle_appstore_notification`).

Use it once, shortly after a similar outage, to backfill the gap. It is
owner-run: nothing about it is automated or wired into the deploy pipeline.

## The two commands

Always dry-run first and read the totals before applying.

```bash
# 1. Dry run: fetch, verify, print a summary line per notification, apply nothing.
.venv/bin/python scripts/replay_appstore_notifications.py \
    --environment production --days 180

# 2. Apply once the dry-run totals look right. Needs Firestore access (ADC),
#    because the handler writes contractor and apple_transactions records.
.venv/bin/python scripts/replay_appstore_notifications.py \
    --environment production --days 180 --apply
```

To source App Store credentials from a live Cloud Run service instead of
setting them locally, add `--from-cloud-run kevin-api` (or
`kevin-api-staging`). It copies `APPSTORE_KEY_ID`, `APPSTORE_ISSUER_ID`,
`APPSTORE_PRIVATE_KEY`, `APPSTORE_BUNDLE_ID`, `APPSTORE_ENVIRONMENT`, and
`FIRESTORE_PROJECT_ID` into the process environment (only variables not
already set) and never prints a value.

By default the script asks Apple for `onlyFailures=true` — the
notifications Apple itself knows never reached the server successfully.
Pass `--all` to fetch everything in the window instead (useful when
double-checking a period rather than assuming Apple's own bookkeeping is
complete).

## The 180-day limit

Apple's Get Notification History only covers the past 180 days. `--days`
defaults to 180 and refuses anything larger (exit code 2). Use `--start`
/ `--end` (`YYYY-MM-DD`, UTC) instead of `--days` to target a narrower
window inside that range — useful for isolating one incident without
re-verifying everything.

## What "rejected" means

Every fetched item is verified with the production webhook's own JWS
verifier before anything else happens to it — same certificate-chain
pinning, same signature check, same bundle ID check. An item that fails
verification is counted as `rejected` and is **never** applied, regardless
of `--apply`. A non-zero `rejected` count on a legitimate Apple history
fetch is unexpected and worth investigating before applying the rest of
the batch — it should not normally happen for payloads that came straight
from Apple's own API.

## Idempotency

Applying is safe to re-run. `handle_appstore_notification` dedupes through
the `apple_transactions` collection (`claim_transaction`) before writing
any subscription state, so replaying a notification that was already
applied — by this script or by the live webhook — is a no-op for that
transaction, not a double-write. This is what makes it safe to run the
apply pass more than once, or to run it after some notifications in the
window already made it through the (now-fixed) live webhook.

## Credentials

- App Store Server API: `APPSTORE_KEY_ID`, `APPSTORE_ISSUER_ID`,
  `APPSTORE_PRIVATE_KEY`, `APPSTORE_BUNDLE_ID` — the same ones the
  production service uses to sign requests to Apple. Source them locally
  or via `--from-cloud-run <service>`.
- `--environment {production,sandbox}` selects Apple's base URL; defaults
  to `$APPSTORE_ENVIRONMENT`.
- `--apply` additionally needs Firestore access via Application Default
  Credentials, because the handler writes contractor and
  `apple_transactions` records. Dry runs need no Firestore access at all.

No secrets are ever printed. Per-notification output is limited to
notification type/subtype, the signed date, environment, a 6-character
suffix of the original transaction id, attempt count, and the last
delivery result — never the full transaction id, `appAccountToken`, or any
email/phone.
