# Phase 0 Account Audit Results

Date: 2026-06-30

This report contains aggregate counts only. It does not include contractor IDs,
document IDs, names, phone numbers, tokens, transcripts, message bodies, token
hashes, media, or estimate results.

## Production

Project: `kevin-491315`

```json
{
  "contractors": {
    "active_or_trial_business_accounts": 1,
    "auto_reply_sms": {
      "missing": 95
    },
    "automation_approval_keys": {},
    "gated_action_keys": {},
    "google_calendar_connected": {
      "false": 94,
      "true": 1
    },
    "integration_write_status": {
      "missing": 95
    },
    "jobber_connected": {
      "false": 94,
      "true": 1
    },
    "sms_compliance_status": {
      "missing": 95
    },
    "subscription_status": {
      "active": 3,
      "missing": 10,
      "trial": 82
    },
    "subscription_tier": {
      "businessPro": 1,
      "missing": 11,
      "none": 81,
      "personal": 2
    },
    "total_contractors": 95,
    "twilio_number_assigned": {
      "false": 78,
      "true": 17
    }
  },
  "estimates": {
    "age_buckets": {
      "31_90_days": 9
    },
    "status": {
      "pending": 9
    },
    "total_estimates": 9
  }
}
```

## Staging

Project: `kevin-staging-491315`

```json
{
  "contractors": {
    "active_or_trial_business_accounts": 0,
    "auto_reply_sms": {},
    "automation_approval_keys": {},
    "gated_action_keys": {},
    "google_calendar_connected": {},
    "integration_write_status": {},
    "jobber_connected": {},
    "sms_compliance_status": {},
    "subscription_status": {},
    "subscription_tier": {},
    "total_contractors": 0,
    "twilio_number_assigned": {}
  },
  "estimates": {
    "age_buckets": {},
    "status": {},
    "total_estimates": 0
  }
}
```

## Release Implication

- No production contractor has `gated_actions` configured.
- No production contractor has `sms_compliance_status` configured.
- No production contractor has `integration_write_status` configured.
- No production contractor has `automation_approvals` configured.
- One production contractor has connected Jobber.
- One production contractor has connected Google Calendar.
- One production contractor is active or trialing on a business tier.
- Nine production estimate records exist, all pending and 31-90 days old.
- At audit time, staging had no contractor or estimate records. A staging smoke
  contractor was created after this audit; see the post-audit note below.

Default-off gates will therefore block all caller-facing SMS/MMS, estimate
token/result sends, and integration write actions after release unless explicit
account flags and approval fields are backfilled first.

Recommended release decision: do not backfill any caller-facing SMS, estimate,
Jobber, or Google Calendar write gates until A2P/SMS compliance, owner approval,
and integration-write safety are explicitly approved.

## Post-Audit Staging Seed

After this aggregate audit, `scripts/phase0_staging_smoke.py` seeded one
disposable contractor in staging Firestore: `codex_phase0_smoke`. This is a
Codex-managed test record for programmatic staging smoke only. It rotates a
scoped staging API token on each smoke run and keeps all Phase 0 action gates in
the default-off posture.

On 2026-07-01, staging deploy `kevin-api-staging-00032-tel` passed the
programmatic mutable smoke. The smoke confirmed default-off estimate token and
text reply gates, cross-tenant call-action denial, and read-only account/work
queue surfaces against staging Firestore and RTDB.
