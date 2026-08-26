# Phase 0 Historical Release Evidence (as of 2026-07-01)

> [!NOTE]
> **Historical Artifact**: This document records historical qualification and verification evidence captured on 2026-07-01 for the Phase 0 backend safety release.
>
> - **Source Status:** Phase 0 backend safety gates, CAS invariants, fail-closed boundaries, and subsequent architectural improvements have long since merged to `main`. Historical notes below referring to "before merging" or "Production release: Pending" document the release evaluation as of July 1, 2026.
> - **Canonical Roadmap:** For current repository roadmap, source implementation status, remaining source gaps, and owner/live gates, see [`docs/current-roadmap.md`](../current-roadmap.md).
> - **Operational Authorities:** Operational envelope runbooks and automated rollback procedures are maintained in [`docs/runbooks/integration-token-envelope.md`](../runbooks/integration-token-envelope.md) and [`.github/workflows/rollback.yml`](../../.github/workflows/rollback.yml).

The code direction is intentionally fail-closed for caller-facing and integration-write side effects. That is safer for v2, but it can change existing production behavior unless account state and release timing are reviewed first.

## Historical Release Decision (2026-07-01 Evaluation)

The following evaluations were recorded during the initial Phase 0 release review:

| Gate | Required outcome | Status (2026-07-01) | Current source interpretation / required revalidation |
|---|---|---|---|
| Deployment source of truth | Confirm whether production deploys only by manual `workflow_dispatch` from `main`, as `.github/workflows/deploy.yml` currently states. If any other path auto-deploys `main`, block merge until a release window and rollback path are set. | Pending | Checked-in workflow verified manual-only in source (`.github/workflows/deploy.yml`); external-path confirmation remains an owner gate |
| Production account audit | Count contractors that would be affected by default-off gates. Do not print customer names, phone numbers, transcripts, tokens, or message bodies. | Complete: see [`docs/security/phase0-account-audit-2026-06-30.md`](phase0-account-audit-2026-06-30.md). | Historical snapshot of 95 contractors (fresh aggregate audit required before future release/backfill decisions) |
| Staging account audit | Confirm a test contractor can exercise disabled and enabled gate paths without touching production Firestore, RTDB, APNs, or Twilio data. | Partial: enumerated read/disabled paths passed; enabled paths not demonstrated | Historical staging evidence (partial: read/disabled subset); staging revalidation required before new releases |
| Backfill decision | Decide which existing side effects stay disabled and which accounts, if any, receive explicit flags before release. | Complete: do not backfill caller-facing SMS, estimate, Jobber, or Google Calendar write gates before A2P/SMS compliance, owner approval, and integration-write safety are explicitly approved. | Default-off baseline maintained in source; flag backfills require explicit owner authorization |
| Staging smoke | Run the smoke matrix below against staging after deployment. | Partial: enumerated read/disabled paths passed; enabled effects and full matrix not demonstrated | Historical staging evidence (read paths and disabled gates only; enabled effects and full matrix unverified) |
| Production release | Only after the above pass, mark the PR ready for review and merge in an approved window. | Pending (historical marker as of 2026-07-01) | Historical marker; source merged to `main`, but production deployment remains owner-gated |

## Historical Audit Status (Captured 2026-07-01)

Last updated: 2026-07-01.

- ADC reauthentication was completed for `delimatsuo@gmail.com`.
- The account could read Cloud Run service metadata for production and staging.
- Production and staging Firestore audits completed with `deli@ellaexecutivesearch.com`.
- Staging Firestore project was confirmed from Cloud Run metadata as `kevin-staging-491315`.
- Staging initially had no contractor or estimate records. A disposable `codex_phase0_smoke` contractor was seeded in staging by `scripts/phase0_staging_smoke.py`; the script rotated a scoped staging API token on each run and did not print it.
- A missing staging Firestore composite index for `jobs(contractor_id ASC, created_at DESC)` caused `/api/jobs` to return HTTP 500 during smoke. The staging index was created programmatically and reached `READY`; `app/db/jobs.py` also now has a missing-index fallback so this endpoint fails soft if another environment lacks the index.
- Staging PR #37 was merged into `staging` to deploy this Phase 0 backend candidate through the protected staging path. GitHub rebase-merged the patch series as staging commit `3cde31abdd524f4d503de5640cbef942ef7d0d15`. The staging workflow passed tests, deployed `kevin-api-staging-00032-tel`, and production deploy was skipped.
- Programmatic mutable staging smoke passed for read paths and disabled-gate boundaries.

> [!CAUTION]
> **Historical Smoke Command Note**: The command snippet below reflects the historical invocation executed on 2026-07-01 and is **not directly runnable** as a modern smoke command. The modern [`scripts/phase0_staging_smoke.py`](../../scripts/phase0_staging_smoke.py) uses randomized contractor nonces (`codex_phase0_smoke_<hex32>`), cleanup is opt-in via `--cleanup`, and staging RTDB smoke remains blocked until an authoritative staging database allowlist is owner-verified and committed to `ALLOWED_STAGING_DATABASE_URLS`.

```bash
# HISTORICAL ONLY (Non-runnable as-is against current HEAD):
.venv/bin/python scripts/phase0_staging_smoke.py \
  --expected-sha "$(git rev-parse origin/staging)" \
  --require-expected-sha \
  --mutable-checks \
  --database-url "$(gh variable get FIREBASE_DATABASE_URL --repo delimatsuo/heykevin --env staging)"
```

  Verified outcomes from July 1, 2026 smoke: scoped contractor profile allowed; cross-contractor profile denied; active-call, calls, jobs, settings, Jobber status, and Google Calendar status read paths passed; estimate token creation was blocked by the default-off gate; cross-tenant call action was denied; text reply was blocked by the default-off gate.
  *(Note: Historical staging smoke verified enumerated read paths and disabled gates only; enabled side-effects, live telephony audio, and carrier SMS delivery were not exercised).*

- The June 30, 2026 audit recorded that 0 of 95 production contractor documents had `gated_actions`, `sms_compliance_status`, `integration_write_status`, or `automation_approvals` configured. Default-off gates block caller-facing SMS/MMS, estimate token/result sends, and integration writes unless explicit account fields are backfilled.
- The checked-in GitHub workflow deploys staging on `staging` push and production only by manual `workflow_dispatch` from `main`.
- Cloud Run service metadata (as observed on July 1, 2026) exposed plaintext runtime secrets to accounts with service-describe access. Do not paste service environment dumps into tickets, PR comments, or chat.

Backfill decision:
- Do not backfill caller-facing SMS, estimate, Jobber, or Google Calendar write gates before A2P/SMS compliance, owner approval, and integration-write safety are explicitly approved.

## Read-Only Account Audit (Historical Snapshot)

Audit production and staging contractor documents by counts only. The audit answers questions without exposing PII.

> [!NOTE]
> The audit output in [`docs/security/phase0-account-audit-2026-06-30.md`](phase0-account-audit-2026-06-30.md) is an immutable historical record from June 30, 2026 (recording 95 total production contractors, 0 with compliance/gated-action flags). Any future release or feature-enablement decision requires running a fresh aggregate audit.

To run a new redacted audit helper when authorized:

```bash
# Production Firestore
.venv/bin/python scripts/phase0_account_audit.py \
  --project kevin-491315 \
  --environment production

# Staging Firestore (FIRESTORE_PROJECT_ID is a GitHub repository variable)
STAGING_FIRESTORE_PROJECT_ID="$(gh variable get FIRESTORE_PROJECT_ID --repo delimatsuo/heykevin)"
.venv/bin/python scripts/phase0_account_audit.py \
  --project "$STAGING_FIRESTORE_PROJECT_ID" \
  --environment staging
```

The script prints aggregate JSON only. Review the output before storing it and discard it if any unexpected raw value appears.

If the command reports that ADC must be reauthenticated:
```bash
gcloud auth application-default login
```

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

## Backfill Decision Matrix & Gated Action Policies

| Action | Default | Gating & Policy Rules |
|---|---|---|
| `caller_text_reply` | Disabled | Requires `sms_compliance_status == "approved"`, `requires_owner_confirmation=True`, and `requires_idempotency=True`. |
| `caller_auto_reply` | Disabled | Requires `sms_compliance_status == "approved"`, explicit owner opt-in, and `requires_idempotency=True`. |
| `caller_confirmation_sms` / `caller_confirmation_mms` | Disabled | Requires `sms_compliance_status == "approved"` and product approval. |
| `appointment_confirmed_caller_sms` | No per-tenant flag; current call path owner-confirmed | In [`app/services/gated_actions.py`](../../app/services/gated_actions.py), this action has `requires_flag=False` and `requires_sms_compliance=False`; policy specifies `requires_owner_confirmation=True` and `requires_idempotency=True`. `check_gated_action` permits execution when `context.owner_confirmed` is true or `automation_approvals[action]` is present. The current appointment confirmation call path sets `owner_confirmed=True`. All other caller-facing SMS remain gated by default. |
| `caller_vcard_mms` | Disabled | Requires `sms_compliance_status == "approved"`, reviewed public vCard fields, and `requires_idempotency=True`. |
| `estimate_token_create` | Disabled | Requires `requires_owner_confirmation=True` and `requires_idempotency=True`. |
| `estimate_result_sms` | Disabled | Requires `sms_compliance_status == "approved"`, `requires_idempotency=True`, and estimate SMS approval. |
| `jobber_lead_capture_enabled` | Disabled | Server-protected flag enabled only after integration connection and duplicate/retry operator acceptance. Legacy `jobber_create_job` and `jobber_create_quote` action keys are retired/tombstoned. |
| `google_create_event` | Disabled | Requires `requires_integration_approval=True`, `requires_owner_confirmation=True`, and `requires_idempotency=True`. |

Owner-facing SMS notifications remain outside these caller-facing gates by design. They still require privacy-safe copy and ordinary delivery monitoring.

## Planned Staging Smoke Matrix

Defined for staging evaluation (July 1, 2026 smoke verified the enumerated subset of read paths and disabled gates only; enabled effects and full live call matrix remain unverified and owner-gated):

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

## Rollback Procedures

Authoritative automated rollback workflows are configured in [`.github/workflows/rollback.yml`](../../.github/workflows/rollback.yml).

If an unexpected behavior occurs after a deployment:
1. Stop any manual production deploy in progress.
2. Trigger the Rollback workflow (`.github/workflows/rollback.yml`) via `workflow_dispatch`, choosing either `traffic-split` (immediate traffic shift to previous Cloud Run revision) or `redeploy-tag` (rebuild/redeploy from a verified release tag). Note: Executing a `workflow_dispatch` rollback and placing an incoming live call require separate owner authorization; the owner performs the live call.
3. Confirm `/health` returns the expected previous `DEPLOY_SHA`.
4. Re-run incoming-call smoke verification.
5. Keep caller-facing and integration-write gates disabled until the incident is fully understood.
