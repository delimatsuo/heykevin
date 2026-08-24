# Phase 0 Release Readiness Checklist

This checklist must be completed before merging the Phase 0 safety PR into a
branch that can deploy to production.

The code direction is intentionally fail-closed for caller-facing and
integration-write side effects. That is safer for v2, but it can change
existing production behavior unless account state and release timing are
reviewed first.

## Release Decision

Do not merge or deploy until all rows below have an owner and an outcome.

| Gate | Required outcome | Status |
|---|---|---|
| Deployment source of truth | Confirm whether production deploys only by manual `workflow_dispatch` from `main`, as `.github/workflows/deploy.yml` currently states. If any other path auto-deploys `main`, block merge until a release window and rollback path are set. | Pending |
| Production account audit | Count contractors that would be affected by default-off gates. Do not print customer names, phone numbers, transcripts, tokens, or message bodies. | Complete: see `docs/security/phase0-account-audit-2026-06-30.md`. |
| Staging account audit | Confirm a test contractor can exercise disabled and enabled gate paths without touching production Firestore, RTDB, APNs, or Twilio data. | Complete: `scripts/phase0_staging_smoke.py` seeded `codex_phase0_smoke` in staging Firestore and mutable gate smoke passed against staging revision `kevin-api-staging-00032-tel`. |
| Backfill decision | Decide which existing side effects stay disabled and which accounts, if any, receive explicit flags before release. | Complete: do not backfill caller-facing SMS, estimate, Jobber, or Google Calendar write gates before A2P/SMS compliance, owner approval, and integration-write safety are explicitly approved. |
| Staging smoke | Run the smoke matrix below against staging after deployment. | Complete: staging deploy and mutable safety smoke passed on 2026-07-01. |
| Production release | Only after the above pass, mark the PR ready for review and merge in an approved window. | Pending |

## Current Audit Status

Last updated: 2026-07-01.

- ADC reauthentication was completed for `delimatsuo@gmail.com`.
- The account can read Cloud Run service metadata for production and staging.
- Production and staging Firestore audits completed with
  `deli@ellaexecutivesearch.com`.
- Staging Firestore project was confirmed from Cloud Run metadata as
  `kevin-staging-491315`.
- Staging initially had no contractor or estimate records. A disposable
  `codex_phase0_smoke` contractor has now been seeded in staging by
  `scripts/phase0_staging_smoke.py`; the script rotates a scoped staging API
  token on each run and does not print it.
- A missing staging Firestore composite index for
  `jobs(contractor_id ASC, created_at DESC)` caused `/api/jobs` to return HTTP
  500 during smoke. The staging index was created programmatically and reached
  `READY`; `app/db/jobs.py` also now has a missing-index fallback so this
  endpoint fails soft if another environment lacks the index.
- Staging PR #37 was merged into `staging` to deploy this Phase 0 backend
  candidate through the protected staging path. GitHub rebase-merged the same
  patch series as staging commit `3cde31abdd524f4d503de5640cbef942ef7d0d15`.
  The staging workflow passed tests, deployed `kevin-api-staging-00032-tel`,
  and production deploy was skipped.
- Programmatic mutable staging smoke passed with no skips:

  ```bash
  .venv/bin/python scripts/phase0_staging_smoke.py \
    --expected-sha "$(git rev-parse origin/staging)" \
    --require-expected-sha \
    --mutable-checks \
    --database-url "$(gh variable get FIREBASE_DATABASE_URL --repo delimatsuo/heykevin --env staging)"
  ```

  Verified outcomes: scoped contractor profile allowed; cross-contractor
  profile denied; active-call, calls, jobs, settings, Jobber status, and Google
  Calendar status read paths passed; estimate token creation was blocked by the
  default-off gate; cross-tenant call action was denied; text reply was blocked
  by the default-off gate.
- Production has no `gated_actions`, `sms_compliance_status`,
  `integration_write_status`, or `automation_approvals` configured on any
  contractor document. Default-off gates will block caller-facing SMS/MMS,
  estimate token/result sends, and integration writes after release unless
  explicit account fields are backfilled.
- The checked-in GitHub workflow deploys staging on `staging` push and
  production only by manual `workflow_dispatch` from `main`; no merge should
  proceed until the owner confirms there is no additional auto-deploy path.
- Cloud Run service metadata currently exposes plaintext runtime secrets to
  accounts with service-describe access. Do not paste service environment dumps
  into tickets, PR comments, or chat. Secret Manager migration and rotation are
  outside this PR but remain production hardening work.

Backfill decision:

- Do not backfill caller-facing SMS, estimate, Jobber, or Google Calendar write
  gates before A2P/SMS compliance, owner approval, and integration-write safety
  are explicitly approved.

## Read-Only Account Audit

Audit production and staging contractor documents by counts only. The audit
should answer these questions without exposing PII:

Use the redacted audit helper:

```bash
# Production Firestore
.venv/bin/python scripts/phase0_account_audit.py \
  --project kevin-491315 \
  --environment production

# Staging Firestore. FIRESTORE_PROJECT_ID is a GitHub repository variable,
# not a secret; do not use the production project for this command.
STAGING_FIRESTORE_PROJECT_ID="$(gh variable get FIRESTORE_PROJECT_ID --repo delimatsuo/heykevin)"
.venv/bin/python scripts/phase0_account_audit.py \
  --project "$STAGING_FIRESTORE_PROJECT_ID" \
  --environment staging
```

The script prints aggregate JSON only. Review the output before storing it and
discard it if any unexpected raw value appears.

If the command reports that ADC must be reauthenticated, run:

```bash
gcloud auth application-default login
```

with an account that has read access to the target Firestore project, then
rerun the audit command. Do not use a service account key unless it is already
approved for local incident/release work.

| Field or behavior | Question |
|---|---|
| `gated_actions` | How many contractors already have action flags? Which action keys are present? |
| `sms_compliance_status` | How many contractors are `approved`, `pending`, missing, or another value? |
| `integration_write_status` | How many contractors are `approved`, `pending`, missing, or another value? |
| `automation_approvals` | How many contractors have explicit automation approvals, grouped by action key? |
| `auto_reply_sms` | How many contractors currently opt into auto-reply behavior? |
| `jobber_access_token` | How many contractors have Jobber connected? Count only presence, never token values. |
| `google_calendar_access_token` | How many contractors have Google Calendar connected? Count only presence, never token values. |
| `twilio_number` | How many contractors have assigned Kevin numbers? Count only presence. |
| `subscription_status` / `subscription_tier` | How many active/trial business accounts might be affected? |
| `estimates` collection | How many recent estimate docs exist by status and age bucket? Do not print token hashes, caller phones, media, or results. |

## Backfill Decision Matrix

Use this table to decide account state before release.

| Action | Default after merge | Backfill only if... |
|---|---|---|
| `caller_text_reply` | Disabled | A2P/10DLC is approved, UI/delivery error handling is production-ready, and owner confirmation remains explicit. |
| `caller_auto_reply` | Disabled | A2P/10DLC is approved and the owner has opted into auto-reply with clear copy. |
| `caller_confirmation_sms` / `caller_confirmation_mms` | Disabled | A2P/10DLC is approved and confirmation copy has product approval. |
| `caller_vcard_mms` | Disabled | A2P/10DLC is approved and public vCard fields have been reviewed. |
| `estimate_token_create` | Disabled | Estimate links are approved for production and owner/automation approval is recorded. |
| `estimate_result_sms` | Disabled | Estimate SMS is approved for production and A2P/10DLC is approved. |
| `jobber_lead_capture_enabled` | Disabled | The integration is connected, server-protected flag is enabled only after duplicate/retry/operator acceptance. Note: legacy `jobber_create_job` and `jobber_create_quote` action keys are retired/tombstoned and not writable by `set_gated_action.py`. |
| `google_create_event` | Disabled | Calendar write approval is recorded and booking conflict/retry behavior is accepted. |

Owner-facing SMS notifications remain outside these caller-facing gates by
design. They still require privacy-safe copy and ordinary delivery monitoring.

## Staging Smoke Matrix

Run these after a staging deploy and before production release:

| Flow | Expected result |
|---|---|
| Incoming unknown caller | Kevin answers, live call appears, transcript updates, no caller-facing SMS is sent while gates are disabled. |
| Pick up | Owner can pick up from iOS; this must not require `gated_actions` flags. |
| Let Kevin take message / voicemail | Owner can send the caller to message capture; this must not require `gated_actions` flags. |
| Text reply with gate disabled | Request is blocked with a clear backend response and no SMS send. |
| Text reply with gate enabled and SMS compliance approved | SMS send path is attempted and gate audit logs contain only safe metadata. |
| Post-call service request with gates disabled | Applicable caller-facing `gated_actions` flags are disabled and `jobber_lead_capture_enabled` is false. Job card and owner notification still work; caller confirmation SMS/MMS, vCard MMS, estimate link, and Jobber client/Request writes are skipped. |
| Post-call service request with selected gates enabled | Only the explicitly enabled side effects run. |
| Urgent call push | Visible push copy is generic and lock-screen safe. |
| Known contact route | Existing trusted/known caller routing still works. |
| Cross-tenant call action | A contractor cannot mutate another contractor's call. |

## Rollback

If production release causes unexpected behavior:

1. Stop any manual production deploy in progress.
2. Revert the merge commit or redeploy the previous known-good Cloud Run
   revision.
3. Confirm `/health` returns the expected previous `DEPLOY_SHA`.
4. Re-run one incoming-call smoke test.
5. Leave caller-facing gates disabled until the incident is understood.
