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
#    Still needs Application Default Credentials for read-only Firestore
#    access -- the stale-deactivation guard looks up the contractor for
#    every EXPIRED/DID_FAIL_TO_RENEW/GRACE_PERIOD_EXPIRED/REFUND/REVOKE
#    notification, even in a dry run. Without ADC, every one of those
#    lookups fails and the guard treats each one as "not stale" (the
#    ambiguous-case default), so a dry run without credentials silently
#    reports every deactivating item as would-apply -- it will not show you
#    which ones the guard would actually have skipped.
.venv/bin/python scripts/replay_appstore_notifications.py \
    --environment production --days 180

# 2. Apply once the dry-run totals look right. Additionally needs *write*
#    Firestore access (ADC), because the handler writes contractor and
#    apple_transactions records.
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
`--all` fetches everything in the window instead, including notifications
Apple already delivered successfully the first time. **Treat `--all
--apply` as a higher-risk combination, not a routine one** — see
Idempotency below for why re-applying an already-delivered notification is
not a no-op. Use `--all` only to double-check a period's completeness, and
always dry-run it first.

## The 180-day limit

Apple's Get Notification History only covers the past 180 days. `--days`
defaults to 180 and refuses anything larger (exit code 2). Use `--start`
/ `--end` (`YYYY-MM-DD`, UTC) instead of `--days` to target a narrower
window inside that range — useful for isolating one incident without
re-verifying everything. `--start`/`--end` are both **inclusive** (the
`--end` day is fully covered), and `--days` cannot be combined with
`--start`/`--end` (exit code 2 if both are given).

## What "rejected" means

Every fetched item is verified with the production webhook's own JWS
verifier before anything else happens to it — same certificate-chain
pinning, same signature check, same bundle ID check. Certificate validity
is checked at each notification's own signing time (its payload's
`signedDate`), not wall-clock now, so a signing leaf that has since
expired or rotated does not cause historical notifications to be
rejected. An item that fails verification is counted as `rejected` and is
**never** applied, regardless of `--apply`. A non-zero `rejected` count on
a legitimate Apple history fetch is unexpected and worth investigating
before applying the rest of the batch — it should not normally happen for
payloads that came straight from Apple's own API.

## Idempotency — read this before a second `--apply` run

**Applying is not a general notification dedupe. Run `--apply` once per
incident, read the totals, and stop.**

`claim_transaction` (inside the handler) is an ownership *binding*, not a
replay guard. Once a contractor already owns a given
`original_transaction_id`, a repeat notification for it is a same-contractor
no-op for that binding — but the handler still runs its full state
transition every time it is invoked. For `EXPIRED`, `DID_FAIL_TO_RENEW`,
`REFUND`, and `REVOKE`, that means an **unconditional** write of
`subscription_status = "expired"` and a re-sent "your subscription has
ended" push, every single time, regardless of what has happened to the
account since. Replaying an old `EXPIRED` for a customer who has since
renewed would otherwise flip them straight back to a non-dismissible
paywall with AI screening off.

This script includes one targeted guard against exactly that regression: a
deactivating notification (`EXPIRED`, `DID_FAIL_TO_RENEW`,
`GRACE_PERIOD_EXPIRED`, `REFUND`, `REVOKE`) whose implicated subscription
term has already been superseded by a newer paid term on file for that
contractor is skipped — printed as `STALE (account term ends later) —
skipped`, counted in the `stale_skipped` total (broken down by type in
`stale_by_type`), and never handed to the handler. This check runs in
**both** dry-run and `--apply`, so a dry run's `dry_run` count already
excludes what would have been skipped as stale.

That guard covers one specific, worst-case regression. It is **not**
general idempotency: every non-stale notification in the window is still
fully re-applied — including re-sent pushes — on every `--apply` run. Do
not run `--apply` more than once on the same window as a matter of
routine; if you need to re-check something, dry-run it and read the
`stale_skipped`/`stale_by_type`/`by_type` totals together instead of
applying again — `by_type` only counts non-stale notifications (it
reconciles exactly with `dry_run + applied + handler_false + handler_error`),
so a type that's fully accounted for in `stale_by_type` and absent from
`by_type` was entirely skipped as stale.

## Credentials

- App Store Server API: `APPSTORE_KEY_ID`, `APPSTORE_ISSUER_ID`,
  `APPSTORE_PRIVATE_KEY`, `APPSTORE_BUNDLE_ID` — the same ones the
  production service uses to sign requests to Apple. Source them locally
  or via `--from-cloud-run <service>`.
- `--environment {production,sandbox}` selects Apple's base URL; defaults
  to `$APPSTORE_ENVIRONMENT`.
- Both dry runs and `--apply` need **read-only** Firestore access via
  Application Default Credentials — the stale-deactivation guard above
  looks up the contractor for every deactivating notification, even in a
  dry run, so it can report `stale_skipped` accurately.
- `--apply` additionally needs **write** access, because the handler
  writes contractor and `apple_transactions` records.

No secrets are ever printed. Per-notification output is limited to
notification type/subtype, the signed date, environment, a 6-character
suffix of the original transaction id, attempt count, and the last
delivery result — never the full transaction id, `appAccountToken`, or any
email/phone.
