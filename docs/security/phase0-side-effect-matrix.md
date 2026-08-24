# Phase 0 Side-Effect Matrix

This matrix is the human-readable companion to `app/services/side_effect_inventory.py`.

The canonical inventory lives in code so tests can enforce coverage. During Phase 0, each row must be verified against current code, assigned a backend gate where needed, and covered by a disabled-by-default test before v2 UI or copy can rely on it.

| Path | Risk | Current behavior | Required gate | Required evidence |
|---|---|---|---|---|
| `app/services/post_call.py` | `user_contact` | Extracts job cards, saves jobs, sends contractor SMS, sends caller SMS/MMS, sends vCard MMS, and can send auto-reply SMS. Behind jobber_lead_capture_enabled and duplicate prevention, lead capture may create a Jobber client when lookup misses, then creates a Request and attempts to add a note; it never calls create_job or create_quote. | Caller-facing SMS/MMS require backend gated actions that default off. Jobber lead capture requires jobber_lead_capture_enabled and claim idempotency. | Disabled-gate tests for caller SMS/MMS, vCard MMS, estimate links, auto-replies, and Jobber disabled-flag / duplicate-prevention claim tests. |
| `app/services/voice_pipeline.py` | `external_write` | Voice tools can check Jobber but expose no Jobber write tool; Google event creation remains separately gated. | Write tools require backend gates, idempotency, and owner confirmation or explicit automation approval. Google Calendar event creation remains separately gated; Jobber exposes no write tool. | Tests proving model tool calls cannot write integrations while gates are disabled, and Jobber write tool attempts are rejected as unknown tools. |
| `app/services/gemini_pipeline.py` | `external_write` | Gemini Live shares prompt, callback, transcript, command, urgency, and tool surfaces with the legacy voice pipeline. | Gemini must obey the same gate registry and sensitive-data rules as the legacy pipeline. | Parity tests or contract tests for tools, transcripts, urgency pushes, and completion callbacks. |
| `app/services/sms.py` | `user_contact` | Sends SMS/MMS through Twilio without delivery-state UI, A2P proof, or opt-out enforcement in the helper. | Caller-facing SMS/MMS requires A2P, delivery tracking, opt-out handling, send limits, and failure UI. | A2P proof, webhook tests, opt-out tests, failed-delivery UI test, and log audit. |
| `app/api/calls.py` | `sensitive_read` | mark-read can write submitted call SIDs. | All call mutations must verify every call SID belongs to the authenticated contractor. | Cross-tenant negative tests for list, detail, mark-read, status update, export, and delete. |
| `app/api/voip.py` | `twilio_mutation` | Call actions can redirect Twilio calls, queue take-message commands, route to voicemail, and send text replies. | Core live-call controls accept, decline, and voicemail remain ownership-only with CallSession/idempotency protections; text_reply requires the caller-text backend gate. | Cross-tenant call-action tests for accept, decline, voicemail, and text_reply; disabled-gate tests for text_reply. |
| `app/webhooks/telegram_callback.py` | `user_contact` | Telegram buttons can pick up, text reply, send voicemail, ignore, call back, and send follow-up texts. | Legacy/admin alternate control paths must use the same backend gated-action registry as iOS. | Tests proving Telegram text/callback/follow-up paths fail closed when gates are disabled. |
| `app/webhooks/twilio_incoming.py` | `twilio_mutation` | Routes incoming calls, computes trust, redirects calls, creates conferences, and can send owner SMS for deleted-app voicemail. | Routing and history must be tenant-scoped; verification calls must bypass normal screening; Twilio mutations must be CallSession-bound. | Tenant route tests, verification-call spoof tests, and CallSession idempotency tests. |
| `app/webhooks/media_stream.py` | `sensitive_read` | Can store full transcript in RTDB, reject streams based on RTDB race, redirect calls, and send urgent pushes with caller speech snippets. | Live state is sensitive; push payloads must be lock-screen safe; media auth must be race-free. | Race test, live-state retention test, and urgent push payload snapshot test. |
| `app/db/jobs.py` | `sensitive_read` | Stores job cards and can list/update jobs. | Job records are call-derived sensitive data with contractor ownership, retention, deletion, export, and encryption rules. | Job ownership, deletion/export, and encryption tests. |
| `app/services/job_card.py` | `sensitive_read` | Sends transcripts and business context to LLM extraction and logs extracted summaries. | LLM prompts and extracted fields are sensitive and must be redacted in logs and covered by deletion/export policy. | Prompt/log redaction tests and extraction payload classification test. |
| `app/services/calendar.py` | `external_write` | Refreshes Google tokens, reads free/busy data, and creates Google Calendar events. | Event creation is a gated write action; token refresh and calendar data are sensitive integration operations. | Disabled calendar write tests, token handling tests, and log redaction tests. |
| `app/services/jobber.py` | `external_write` | Refreshes and persists Jobber tokens, reads customer/calendar data, and contains the active Request adapter. Dormant create_job/create_quote helpers are not runtime-exposed and are out of scope for deletion here. | Jobber Request lead capture requires jobber_lead_capture_enabled; token persistence must use encrypted storage and payload-safe audit logs. | Disabled Jobber lead capture flag tests, duplicate-prevention claim tests, token refresh tests, and log redaction tests. |
| `app/api/integrations.py` | `external_write` | OAuth connect/callback/disconnect writes and deletes Jobber and Google Calendar tokens on contractor documents. | Integration tokens require state binding, contractor ownership, encrypted storage, revocation, deletion, and audit. | OAuth state replay tests, cross-tenant state tests, token encryption tests, and disconnect deletion tests. |
| `app/api/estimates.py` | `user_contact` | Creates public estimate tokens, accepts uploads, analyzes media, stores results, and sends caller/contractor SMS. | Estimate links, uploads, analysis, and result SMS are gated side effects with expiry, upload caps, and deletion/export policy. | Disabled-gate tests, upload abuse tests, token expiry tests, SMS disabled tests, and deletion/export tests. |
| `app/api/contractors.py` | `irreversible` | Provisions Twilio numbers, patches config, deactivates accounts, and releases phone numbers. | Provisioning, deletion, and number release require confirmation, idempotency, partial-failure handling, and audit. | Duplicate provisioning, protected-field, deletion completeness, and number-release partial failure tests. |
| `app/services/conference.py` | `twilio_mutation` | Adds/removes participants and ends Twilio conferences. | Conference actions must be CallSession-owned and idempotent. | Conference ownership and duplicate-action tests. |
| `app/services/warm_transfer.py` | `twilio_mutation` | Redirects callers into conferences and sends dial-in details via Telegram. | Pickup must be CallSession-owned, idempotent, and payload-safe. | Pickup ownership, duplicate pickup, rollback-to-screening, and redacted logging tests. |
| `app/services/vcard.py` | `sensitive_read` | Generates signed public vCard URLs. | vCard links require dedicated HMAC secret, expiry, contractor ownership at creation, and approved public fields. | HMAC secret, expiry, forged signature, and public-data review tests. |
| `app/api/vcard.py` | `sensitive_read` | Serves signed public contractor vCards. | Public vCard downloads must be limited to approved contact data and valid signatures. | Signature rejection, expiry, and response-field tests. |
| `app/services/push_notification.py` | `user_contact` | Sends VoIP, urgent, regular, and summary pushes; can delete expired device tokens. | Payloads must be lock-screen-safe; token deletion must be contractor-scoped or otherwise safe. | Push payload snapshot tests and expired-token deletion ownership tests. |

Run:

```bash
pytest tests/unit/test_phase0_side_effect_inventory.py -q
```

Expected: inventory completeness tests pass.

## Phase 0 Verification Commands

Run the focused Phase 0 suite:

```bash
pytest \
  tests/unit/test_phase0_side_effect_inventory.py \
  tests/unit/test_gated_actions.py \
  tests/unit/test_phase0_call_ownership.py \
  tests/unit/test_phase0_sms_gates.py \
  tests/unit/test_phase0_post_call_gates.py \
  tests/unit/test_phase0_action_gates.py \
  tests/unit/test_phase0_voice_tool_gates.py \
  tests/unit/test_phase0_estimate_gates.py \
  tests/unit/test_phase0_push_payloads.py \
  -q
```

Run the adjacent security regression suite:

```bash
pytest \
  tests/unit/test_conference_security.py \
  tests/unit/test_security_audit_medium.py \
  tests/unit/test_security_audit_f9_f10_f11.py \
  tests/unit/test_jobber.py \
  tests/unit/test_twilio_provisioning.py \
  tests/unit/test_voip_token.py \
  -q
```

Run full backend tests before PR:

```bash
pytest --tb=short -q
```
