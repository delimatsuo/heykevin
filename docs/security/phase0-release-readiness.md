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
| Production account audit | Count contractors that would be affected by default-off gates. Do not print customer names, phone numbers, transcripts, tokens, or message bodies. | Pending |
| Staging account audit | Confirm a test contractor can exercise disabled and enabled gate paths without touching production Firestore, RTDB, APNs, or Twilio data. | Pending |
| Backfill decision | Decide which existing side effects stay disabled and which accounts, if any, receive explicit flags before release. | Pending |
| Staging smoke | Run the smoke matrix below against staging after deployment. | Pending |
| Production release | Only after the above pass, mark the PR ready for review and merge in an approved window. | Pending |

## Read-Only Account Audit

Audit production and staging contractor documents by counts only. The audit
should answer these questions without exposing PII:

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
| `jobber_create_job` / `jobber_create_quote` | Disabled | The integration is connected, write approval is recorded, and duplicate-prevention behavior is accepted. |
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
| Post-call service request with gates disabled | Job card and owner notification still work; caller confirmation SMS/MMS, vCard MMS, estimate link, and integration writes are skipped. |
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

