# Hey Kevin v2 Business Dispatch Product Spec

**Date:** 2026-06-30
**Status:** Draft for user review
**Decision source:** Product, engineering, UX, security, and growth panel handoff from 2026-06-30
**Implementation status:** No v2 implementation should start until this spec is reviewed and approved.

## 1. Product Decision

Hey Kevin v2 should be built around a business-first wedge:

> Kevin is the iPhone-native AI receptionist for solo contractors and small trades businesses. It answers when they cannot, qualifies real jobs, alerts urgent calls, follows up only through production-ready channels, and lets the owner take over live.

The product should not lead with generic consumer spam blocking. Personal mode can remain as a secondary use case, but the growth engine is missed-job recovery for owners who use an iPhone as their business phone and do not want to migrate to a full phone system.

## 2. Target Customer

Primary customer:

- Solo contractors and owner-operated trades businesses.
- Businesses where the owner is often driving, on a job, with a customer, or after hours.
- Users who already receive business calls on a personal iPhone or a simple mobile business line.
- Users who need missed calls turned into qualified job opportunities, not just voicemail transcripts.

Initial trades:

- Plumbing.
- HVAC.
- Electrical.
- Roofing.
- Handyman.
- Landscaping.

Secondary customer:

- Personal call-screening users who value summaries and live takeover.
- This segment should not drive v2 prioritization because free/native call screening is becoming stronger.

## 3. Product Promise

Kevin v2 should make one promise:

> If a real customer calls while the owner cannot answer, Kevin captures the job details, identifies urgency, gives the owner a fast decision brief, and leaves a clean next step.

The promise is not "Kevin blocks spam." Spam handling remains useful, but business value comes from job qualification and follow-up.

## 4. Product Principles

1. **Business readiness beats passive setup.** Kevin must not claim it is ready until call forwarding is verified.
2. **The live moment is a dispatch decision.** The owner needs a fast brief and clear actions before reading a transcript.
3. **Every call should become work or noise.** Calls should be triaged into action states, not listed as a chronological archive only.
4. **Job cards are the differentiator.** A transcript is evidence. A structured job card is the product.
5. **Owner control comes first.** Kevin can draft follow-ups, booking actions, and job records, but production integrations require confirmation and safety gates.
6. **Trust gates block launch.** Privacy, deletion, tenant isolation, transcript encryption, disclosure, and call reliability are preconditions for broader launch.

## 5. Information Architecture

The app should move from `Live / Recents / Settings` to:

1. **Dispatch**
   - Home and live-call surface.
   - Shows readiness when idle.
   - Shows an active decision brief above the live transcript during calls.

2. **Calls**
   - Work queue for screened calls.
   - Shows calls by action state, urgency, lead quality, and outcome.
   - Opens into call details and structured job cards.

3. **Kevin**
   - Control center.
   - Owns setup, business profile, intake configuration, privacy, plan, and integrations.

This structure should make business value visible without burying it in one long settings screen.

## 6. Dispatch

### 6.1 Idle Dispatch

When there is no active call, Dispatch should show readiness, not an empty "No Active Call" state.

Required readiness states:

- **Ready:** Kevin number assigned, forwarding verified, push enabled, subscription active or trialing, and backend readiness confirmed by a screening-capable health state.
- **Setup needed:** Missing Kevin number, missing verified forwarding, push disabled, or incomplete business basics.
- **Verification pending:** User has started a forwarding verification test that has not completed.
- **Not screening:** User skipped forwarding setup or verification failed.
- **Expired:** Subscription expired; AI screening is unavailable.
- **Offline or degraded:** App cannot confirm current backend state.

Idle Dispatch should answer the owner's practical question: "Will Kevin answer my next missed business call?"

### 6.2 Active Dispatch

During a live call, Dispatch should be a decision surface first and a transcript second.

First viewport requirements:

- Caller identity or formatted phone number.
- Current call state.
- Decision brief above the transcript.
- Primary action buttons visible without scrolling on iPhone SE through Pro Max.
- Transcript below the decision brief.

Decision brief fields:

- **Caller:** name when available, phone always.
- **Reason:** one-sentence reason for calling.
- **Service category:** trade-specific category when confidence is sufficient.
- **Urgency:** emergency, today, scheduled, sales/noise, or unknown.
- **Address/location:** captured address or "not captured yet."
- **Confidence:** high, medium, or low, based on extraction completeness and ambiguity.
- **Route reason:** why Kevin screened, rang through, escalated, or blocked.
- **Recommended owner action:** pick up now, let Kevin take a message, ask for address, call back, or ignore/block.

Actions:

- **Pick up:** starts warm transfer without dropping the caller.
- **Let Kevin take message:** replaces "Ignore" and keeps caller expectations clear.
- **Ask for missing detail:** Kevin can be prompted to ask for address, availability, issue type, or photos when the voice pipeline supports safe mid-call instructions.
- **Save contact:** available when caller identity is useful.
- **Block or mark spam:** available only after enough evidence or owner confirmation.

Gated actions:

- **Text reply** must remain hidden unless A2P/10DLC approval, backend send path, UI state handling, and delivery/error reporting are production-ready.
- **Book job** must remain disabled unless the booking safety gates in this spec are met.
- **Send booking link** must remain disabled unless SMS compliance and link-tracking privacy are ready.

### 6.3 Active Dispatch States

Dispatch must handle:

- Active screening.
- Kevin taking a message.
- Owner pickup in progress.
- Owner in call.
- Caller hung up.
- Kevin failed to answer.
- Push disabled while call is active.
- Network degraded while call is active.
- Subscription expired.

Each state needs visible owner guidance and a safe action. No state should leave the owner guessing whether the caller is still connected.

## 7. Verified Forwarding Setup

### 7.1 Requirement

Kevin must not claim readiness based only on a local toggle or a tapped carrier code. Verified forwarding is mandatory before the app can say "Kevin is ready."

Current behavior allows onboarding to complete through "I'm All Set" or "Skip for now" after launching dial codes. v2 must replace that with a server-backed readiness state.

### 7.2 Verification Model

Forwarding status should be stored server-side and mirrored locally:

- `not_started`
- `dial_code_launched`
- `verification_pending`
- `verified`
- `failed`
- `skipped`
- `needs_reverification`

`verified` means Kevin has received an expected forwarded test call or a manual support verification has been recorded server-side. Directly calling the Kevin number can verify that the assigned number works, but it does not prove the user's personal or business phone forwards missed calls to Kevin.

### 7.3 Test Flow

Recommended verification flow:

1. User chooses carrier type, with Verizon separated from default GSM-style codes.
2. App launches the correct activation code for the assigned Kevin number.
3. Backend creates a short-lived verification session for the contractor.
4. Kevin initiates or instructs a test call to the user's real phone number.
5. User lets the phone ring through or declines as instructed.
6. Incoming webhook marks forwarding verified only when the expected verification call reaches the contractor's Kevin number within the session window.
7. App moves from `verification_pending` to `verified` after reading server state.

Failure states must explain what happened:

- No forwarded call received.
- Test call reached voicemail instead of Kevin.
- Carrier code likely failed.
- Kevin number missing.
- Push or backend state unavailable.
- User skipped setup.

### 7.4 Reverification

Kevin should require reverification when:

- The user's personal phone changes.
- The assigned Kevin number changes.
- Carrier type changes.
- Forwarding is manually disabled.
- The backend detects calls have stopped after prior successful usage.
- The user clears all forwarding.

## 8. Structured Trade Intake

### 8.1 Goal

Kevin should turn calls into job cards that contractors can act on quickly. The job card is the main product differentiator against native call screening.

### 8.2 Intake Packs

The first v2 intake packs should be:

- Plumbing.
- HVAC.
- Electrical.
- Roofing.
- Handyman.
- Landscaping.

Each pack should define:

- Service categories.
- Required intake questions.
- Safety flags.
- Urgency rules.
- Address requirements.
- After-hours behavior.
- Follow-up and booking eligibility.

There should also be a general contractor fallback pack for businesses that do not match the first six verticals.

### 8.3 Job Card Schema

Every qualified business call should produce a structured job card:

- `call_sid`
- `contractor_id`
- `created_at`
- `caller_name`
- `caller_phone`
- `callback_number`
- `caller_language`
- `trade_pack`
- `service_category`
- `issue_summary`
- `urgency_level`
- `urgency_reason`
- `safety_flags`
- `address_raw`
- `address_confidence`
- `property_type`
- `preferred_time`
- `after_hours`
- `customer_status`
- `spam_or_sales_signal`
- `missing_fields`
- `recommended_next_action`
- `owner_action_status`
- `extraction_confidence`
- `transcript_available`

The job card should avoid pretending uncertain extraction is certain. Missing or low-confidence fields should be visible and actionable.

### 8.4 Backend Ownership

Urgency, action status, service category, and job-card fields should be generated and stored by the backend. iOS can display and cache them, but client-side transcript heuristics should not be the source of truth for business decisions.

## 9. Calls Work Queue

### 9.1 Goal

Calls should replace Recents as the work queue for missed opportunities and completed screening.

### 9.2 Filters

Required filters:

- Needs action.
- New leads.
- Urgent.
- Voicemail/message.
- Answered.
- Blocked/spam.
- All.

### 9.3 Row Content

Each row should show:

- Caller identity.
- Time.
- Job-card summary or call summary.
- Urgency badge from backend.
- Action status.
- Read/unread state.
- Whether the call has a complete job card.

### 9.4 Call Detail

Call detail should show:

- Job card first when available.
- Owner actions.
- Transcript as evidence below the job card.
- Outcome.
- Trust and route reason.
- Missing fields.
- Follow-up history.
- Privacy/retention controls when available.

### 9.5 Action Statuses

The queue should support:

- New.
- Needs callback.
- Waiting on customer.
- Booked externally.
- Dismissed.
- Spam/blocked.
- Archived.

These statuses should be server-backed so work state survives reinstall and multiple devices later.

## 10. Kevin Control Center

The Kevin tab should replace the overloaded settings model with an operational control center.

Sections:

- **Readiness:** number, forwarding verification, push, subscription, backend health.
- **Business:** business name, owner name, trade, service area, hours, after-hours behavior.
- **Intake:** selected trade pack, required questions, emergency rules, tone.
- **Knowledge:** FAQs, services, policies, pricing guidance, unavailable services.
- **Contacts and trust:** contact sync, VIP ring-through, block list, trust policy.
- **Follow-up:** SMS status, caller follow-up settings, owner alert channels.
- **Integrations:** Jobber, Google Calendar, Zapier/webhooks, and future FSM systems.
- **Privacy Center:** retention, transcript controls, contact sync status, export, delete call history, delete account.
- **Plan:** subscription status, tier, limits, renewal price, restore.

Kevin should be where the owner controls how the receptionist behaves. It should not feel like a generic preference list.

## 11. SMS, A2P, and Follow-Up Safety

Text reply and caller follow-up are high-value but must not be promised before compliance and reliability are complete.

Production-ready SMS requires:

- A2P/10DLC registration approved for the sending use case.
- Backend delivery handling for Twilio accepted, queued, sent, delivered, failed, and undelivered states.
- User-visible failure handling for important owner actions.
- Per-contractor sending identity and messaging limits.
- Opt-out handling.
- Audit logging that does not expose sensitive message content.
- Clear distinction between owner notifications and caller-facing SMS.

Until these are complete:

- The active-call text reply button remains hidden.
- Marketing copy should not promise automatic text replies.
- Job cards can include "follow-up recommended" without exposing a send action.

## 12. Booking and Integration Safety

Integrations should support owner-operated workflows without letting the AI create bad appointments or duplicate jobs.

Priority order:

1. Jobber.
2. Google Calendar.
3. Zapier or outbound webhooks.
4. Housecall Pro and ServiceTitan after demand is validated.

Booking and job-creation actions require:

- Owner confirmation for any created job, appointment, or outbound customer message.
- Idempotency keys per call and action.
- Duplicate detection.
- Conflict checks for calendars.
- Clear integration connection status.
- App-level encryption or KMS envelope encryption for integration tokens.
- Revocation and reconnect flows.
- Audit trail of which owner or automation created the record.

v2 may show integration setup and read-only status before write actions are enabled. It must not imply automatic booking is live until these gates pass.

## 13. Trust, Security, and Reliability Launch Blockers

Broader v2 launch is blocked until these conditions are fixed or explicitly descoped from launch:

### 13.1 Account Deletion

Deletion behavior must match product copy. If the app says deletion releases the Kevin number and deletes data, backend behavior must delete or irreversibly anonymize:

- Contractor profile.
- Contacts and caller contacts.
- Device tokens.
- Calls, transcripts, summaries, and job cards.
- Integration tokens.
- RTDB active-call state.
- Business knowledge.
- Transaction artifacts where legally allowed.

Deletion must have automated tests and an owner-visible completion state.

### 13.2 Tenant Isolation

Call history, trust scoring, contacts, job cards, and integration actions must be contractor-scoped. Legacy global lookups may remain only if they cannot leak another contractor's data and are covered by tests.

### 13.3 Transcript Encryption

Production must fail closed when transcript encryption is not configured. Plaintext fallback is acceptable only for local development or explicitly migrated legacy records. Production startup should reject missing or invalid encryption configuration.

### 13.4 Caller Disclosure

Kevin needs caller-facing disclosure by default. The default greeting should make it clear that Kevin is an AI assistant and that the call may be transcribed and summarized for the business owner. Jurisdiction-specific variants may replace the default only after legal review.

### 13.5 Privacy Disclosures

App privacy labels and `ios/Kevin/Resources/PrivacyInfo.xcprivacy` must match actual data handling for:

- Phone numbers.
- Names.
- Contacts.
- Call content.
- Transcripts.
- Business knowledge.
- Device tokens.
- Subscription identity.
- Integration data.
- Calendar or CRM data when enabled.

### 13.6 Privacy-Safe Logging

Logs must avoid raw caller speech, full phone numbers, full tokens, transcript content, and integration payloads. Production observability should use structured events with safe identifiers and redacted fields.

### 13.7 Call Session Reliability

The backend needs a canonical call-session lifecycle covering:

- Incoming webhook.
- Route decision.
- Active-call state creation.
- Media stream connection.
- First audible response.
- Transcript updates.
- Owner pickup.
- Owner decline.
- Caller hangup.
- Conference end.
- Summary and job-card creation.
- Cleanup.

The known active-call/media-stream race must be reproduced with a test and eliminated before launch.

## 14. Reliability Targets

Launch targets:

- Inbound webhook p95 under 1 second and p99 under 3 seconds.
- First audible Kevin greeting p95 under 2.5 seconds from Twilio media `start`.
- Live transcript update p95 under 1.5 seconds after final STT.
- Pickup success p99 above 99 percent.
- Caller is never dropped during stream-to-conference redirect.
- Per-call trace covers webhook, route, APNs, media connect, first audio, STT/LLM/TTS timings, app action, conference join, summary, and cleanup.

If these targets are not met, v2 can be used for internal testing or limited beta, but not broader launch.

## 15. Packaging and Pricing

### 15.1 Personal

Personal remains available but secondary:

- Recommended range: $6.99-$9.99/month or $49-$69/year.
- Product promise: smarter personal screening, summaries, live takeover.
- Not the primary growth story.

### 15.2 Business

Business is the default v2 tier:

- Current $49.99/month can remain as founding launch pricing.
- Includes business-first Dispatch, verified setup, trade intake, job cards, Calls queue, business hours, knowledge, and owner alerts.
- Usage limits should be explicit before broader launch to avoid heavy-user margin surprises.

### 15.3 Business Pro

Business Pro should move toward $99-$149/month when it includes:

- Higher call volume.
- Longer history.
- Advanced trade packs.
- Jobber or calendar integrations.
- Owner-confirmed booking or job creation.
- Automation and webhooks.
- More complete privacy and export controls.

If Business Pro remains at $79.99/month during launch, it should be framed as founding pricing.

## 16. Metrics

Activation metrics:

- Percent of new business users who reach verified forwarding.
- Median time from install to verified readiness.
- Percent of users stuck in each setup failure state.

Call value metrics:

- Calls screened per active business.
- Percent of screened calls that produce a job card.
- Percent of job cards with caller, issue, address, urgency, and next action.
- Percent of calls marked urgent by backend and owner-confirmed as urgent.
- Time from call start to owner decision.

Retention and monetization metrics:

- First-week qualified leads captured.
- Owner callback rate from Calls.
- Business trial to paid conversion.
- Cancellation reasons tied to setup failure, call quality, or missing integrations.

Trust metrics:

- Pickup failure rate.
- First greeting latency.
- Transcript delay.
- Forwarding verification failure rate.
- SMS delivery failure rate after SMS is enabled.
- Deletion completion and support intervention rate.

## 17. Accessibility and Localization

v2 must preserve iPhone-native accessibility quality:

- All call actions need VoiceOver labels that include consequences.
- Tap targets should be at least 44 points.
- Dynamic Type must work through accessibility sizes.
- Active-call actions must remain reachable one-handed.
- Dark mode, high contrast, and reduced motion must be reviewed.
- Long names, long business names, prices, dates, phone numbers, and non-English transcripts must not overflow.
- Pseudo-localization should pass before App Store release.

## 18. Panel-Required Gates Before Implementation Planning

The 2026-06-30 plan-review panel approved the product direction with conditions. These conditions are hard gates. Implementation planning must not start until this section is reviewed and accepted.

### 18.1 Phase 0 Safety and Existing Behavior Audit

Phase 0 must audit and gate existing outbound side effects before any v2 UI, copy, or workflow promises them.

| Existing path | Current behavior to audit | Gate before v2 can rely on it | Required evidence |
|---|---|---|---|
| `app/services/post_call.py` | Extracts job cards, saves jobs, can auto-create Jobber jobs, sends contractor SMS, sends caller SMS/MMS, sends vCard MMS, and can send auto-reply SMS. | All outbound caller actions and integration writes must be behind explicit per-contractor flags that default off. | Test proving disabled flags prevent caller SMS/MMS, Jobber writes, vCard MMS, estimate links, and auto-replies. |
| `app/services/voice_pipeline.py` | Voice tools can check Jobber customers, check availability, and create Jobber jobs during a call. | `book_appointment` and any write tool must require production-ready integration gates and owner confirmation. | Test proving the model cannot create jobs unless the contractor has the gate enabled and an idempotent owner-approved action exists. |
| `app/services/sms.py` | Sends SMS/MMS through Twilio without delivery-state UI, A2P proof, or opt-out enforcement in the helper. | Caller-facing SMS/MMS requires A2P/10DLC proof, delivery webhooks, opt-out handling, send limits, and visible failure states. | A2P proof, webhook tests, opt-out tests, failed-delivery UI test, and log audit. |
| `app/api/calls.py` | `mark-read` writes arbitrary submitted call SIDs without checking ownership per SID. | All call mutations must verify every `call_sid` belongs to the authenticated contractor before writing. | Cross-tenant negative test for list, detail, mark-read, status update, export, and delete. |
| `app/webhooks/twilio_incoming.py` | Trust history can be calculated from caller-phone history without contractor scoping. | Trust, routing, and history must be tenant-scoped. | Test proving one contractor's history cannot affect another contractor's route decision. |
| `app/webhooks/twilio_incoming.py` and `app/webhooks/media_stream.py` | Media stream can connect before RTDB active-call state exists. | CallSession creation and WebSocket authorization must be race-free before Dispatch UI depends on live state. | Failing-then-passing race test covering Twilio webhook to media-stream connection. |
| `app/webhooks/media_stream.py` | RTDB active-call state can contain the full live transcript. | RTDB must be treated as sensitive call-derived storage with retention, access, and encryption/redaction decisions documented. | Privacy review and test proving live state is retained only as intended and access is contractor-scoped. |
| `app/webhooks/media_stream.py` | Urgent push body can include caller speech snippets and full caller context. | Lock-screen push payloads must not include raw caller speech, full phone numbers, detailed issue text, tokens, or integration data. | Push-payload audit test with urgent and non-urgent calls. |
| `app/db/jobs.py` and `app/services/job_card.py` | Job records can store transcripts and extracted service details. | Job cards are sensitive call-derived records and must follow the same ownership, retention, deletion, export, and encryption rules as calls. | Seeded deletion/export/encryption tests across calls and jobs. |
| `app/api/voip.py` | Active-call polling exposes transcript buffers; call actions can redirect Twilio calls, queue take-message commands, route to voicemail, and send text replies. | Core live-call controls `accept`, `decline`, and `voicemail` remain ownership-only with CallSession/idempotency protections. `text_reply` must use the caller-text backend gate and remain disabled unless SMS gate is ready. | Cross-tenant call-action tests for `accept`, `decline`, `voicemail`, and `text_reply`, plus disabled-gate tests for `text_reply`. |
| `app/webhooks/telegram_callback.py` | Telegram buttons can pick up, text reply, send to voicemail, ignore, call back, and send follow-up text. | Legacy/admin alternate control paths must obey the same backend gated-action registry as iOS. | Tests proving Telegram text/callback/follow-up paths fail closed when gates are disabled or ownership cannot be proven. |
| `app/services/voice_pipeline.py` | Voice tools expose Jobber and Google Calendar availability checks and `book_appointment`; prompt copy can offer media follow-up links. | Tool execution must distinguish read-only tools from write tools. All write tools default off, require owner confirmation or explicit automation approval, and return typed disabled responses. | Tests proving model tool calls cannot create Jobber jobs or Calendar events without enabled backend gates and idempotency keys. |
| `app/services/gemini_pipeline.py` | Gemini Live can use the same prompt, callback, transcript, command, urgency, and tool surfaces as the voice pipeline. | Gemini must inherit the same gated-action registry, sensitive-data, disclosure, and CallSession rules as the legacy pipeline. | Parity tests or contract tests proving both engines obey identical gates for tools, transcripts, urgency pushes, and completion callbacks. |
| `app/services/calendar.py` | Can refresh Google tokens, read free/busy data, and create Google Calendar events. | Event creation is a gated write action; token refresh and calendar data are sensitive integration operations. | Tests for disabled calendar writes, token handling, log redaction, and owner confirmation before event creation. |
| `app/services/jobber.py` | Can refresh and persist Jobber tokens, read customer/calendar data, create jobs, and create quotes. | Jobber writes are gated actions; token refresh persistence must use encrypted storage and audit logs without token values. | Tests for disabled Jobber writes, token refresh persistence, duplicate prevention, and log redaction. |
| `app/api/integrations.py` | OAuth connect/callback/disconnect writes and deletes Jobber and Google Calendar tokens on contractor docs. | Integration connect/disconnect must enforce state binding, contractor ownership, encrypted token storage, and revocation/deletion audit. | State replay tests, cross-tenant state tests, token encryption tests, disconnect deletion tests. |
| `app/api/estimates.py` | Creates public estimate tokens, accepts media uploads, analyzes media, stores results, and sends caller/contractor SMS. | Estimate links, uploads, analysis, and result SMS are gated side effects. Public-token access must be scoped by unguessable token, expiry, upload limits, content limits, and deletion/export policy. | Disabled-gate tests, upload abuse tests, token expiry tests, SMS disabled tests, deletion/export tests for estimate records and media. |
| `app/api/contractors.py` | Provisions Twilio numbers, patches business/config data, deletes/deactivates accounts, and releases phone numbers. | Provisioning, deletion, and number release are irreversible or costly actions requiring explicit confirmation, idempotency, rollback decision, and audit. | Tests for duplicate provisioning, protected fields, deletion completeness, number-release partial failure, and no accidental release from stale clients. |
| `app/services/conference.py` and `app/services/warm_transfer.py` | Can add/remove conference participants, end conferences, redirect callers, and send dial-in details via Telegram. | Conference actions must be bound to CallSession ownership and idempotent state transitions; no raw PINs or caller details in unsafe logs/pushes. | Tests for conference ownership, duplicate pickup, rollback to screening on failure, and redacted logging. |
| `app/services/vcard.py` and `app/api/vcard.py` | Generates signed public vCard URLs and serves contractor contact cards. | vCard links must use a dedicated HMAC secret, expiry, contractor ownership at creation, and no sensitive fields beyond approved contact data. | HMAC-secret config test, expiry test, forged-signature test, and public data review. |
| Push notification services | VoIP, urgent, regular, and summary pushes can expose caller context on lock screen and can delete expired device tokens. | Payloads must be lock-screen-safe; token deletion must be contractor-scoped or otherwise safe. | Payload snapshot tests and expired-token deletion ownership tests. |

Phase 0 exit criteria:

- Every current outbound side-effect path is inventoried with trigger, production status, feature flag, owner confirmation status, and test coverage.
- All caller-facing SMS/MMS and integration write paths are default-off unless their gates are satisfied.
- No v2 UI or copy mentions text reply, automatic caller follow-up, booking, estimates, or integration writes unless the matching gate is production-ready.
- The implementation plan includes a rollback or disable switch for each side-effect path.
- The inventory must include every live path named in the table above plus any newly discovered route, webhook, scheduled task, or helper that can contact users, mutate Twilio, mutate integrations, create public links, release numbers, delete data, write sensitive records, or expose push/log payloads.

### 18.1.1 Backend Gated-Action Registry

Backend gates are canonical. iOS feature flags, hidden buttons, prompt instructions, and admin/Telegram UI choices are not sufficient.

The backend gated-action registry must define each action:

- action key.
- contractor ID.
- source path: iOS, Telegram, webhook, voice tool, post-call task, admin, scheduled job, or support.
- entitlement requirement.
- feature flag status.
- compliance gate status.
- environment allowance: local, staging, production.
- owner confirmation requirement.
- automation approval status.
- idempotency key requirement.
- rollback or disable switch.
- typed disabled response.
- audit event name.
- sensitive-data class touched.

Registry rules:

- Unknown actions fail closed.
- Missing contractor ownership fails closed.
- Missing compliance gate fails closed.
- Missing idempotency key for a side effect fails closed.
- Production defaults are off until explicitly enabled per contractor or per environment.
- Alternate control paths must call the same gate check as the primary iOS path.
- Read-only integration tools are separate from write tools.
- Disabled responses must be typed so UI can display "not enabled" rather than generic failure.
- Audit events must record action, actor, contractor, resource IDs, result, and timestamps without recording sensitive payload bodies.

### 18.1.2 Endpoint Ownership and Side-Effect Matrix

The implementation plan must include an endpoint-by-endpoint matrix before code work starts. Each row must list route/path, actor, owner source, sensitive data touched, mutation or external side effect, required gate, rollback/disable switch, and cross-tenant negative test.

Minimum required matrix rows:

| Route or path | Owner source | Mutation or side effect | Required gate/test |
|---|---|---|---|
| `GET /api/active-call` | authenticated contractor plus RTDB `contractor_id` | Reads active call and live transcript buffer. | Cross-tenant active-call read denial; sensitive live-state redaction decision. |
| `GET /api/transcript/{call_sid}` | authenticated contractor plus CallSession/call owner | Reads live transcript. | Cross-tenant transcript denial; retention/encryption expectation. |
| `POST /api/call-action` | authenticated contractor plus RTDB/Firestore call owner | Accept, decline, voicemail, text reply, Twilio redirect, RTDB command, SMS. | Per-action backend gate; duplicate action/idempotency; cross-tenant denial. |
| `GET /api/calls` and `GET /api/calls/{call_sid}` | authenticated contractor plus call owner | Reads call records and transcripts. | Cross-tenant denial; encrypted transcript handling. |
| `POST /api/calls/mark-read` | authenticated contractor plus every submitted call owner | Updates read state. | Reject mixed-owner SID list; partial update semantics defined. |
| `GET /api/jobs` and `GET /api/jobs/{job_id}` | authenticated contractor plus job owner | Reads job cards. | Cross-tenant denial; sensitive job-card redaction/export rules. |
| Job-card status/action endpoints planned for v2 | authenticated contractor plus job owner | Updates status, actions, archive, dismiss, spam/block. | Allowed transition test; actor audit; cross-tenant denial. |
| Forwarding verification endpoints planned for v2 | authenticated contractor plus verification session owner | Creates/verifies/retries verification sessions. | Session nonce, expiry, spoof rejection, direct-call rejection. |
| `POST /api/register-device` | authenticated contractor | Writes push/VoIP token and device metadata. | Contractor ownership; token redaction; expired-token deletion behavior. |
| `GET/POST /api/integrations/*` | OAuth state plus contractor binding | Stores, refreshes, revokes, or deletes integration tokens. | OAuth state replay/cross-tenant tests; encrypted token storage; disconnect audit. |
| Voice pipeline and Gemini pipeline tool execution | CallSession contractor plus gate registry | Reads integrations and may write Jobber/Calendar records. | Read/write split; disabled write tools; owner confirmation; idempotency. |
| Post-call processing | CallSession contractor plus gate registry | Saves jobs, sends SMS/MMS, creates estimate tokens, creates Jobber jobs. | Side-effect disabled tests; duplicate prevention; audit events. |
| Estimate endpoints | contractor on token creation; public token on upload/result | Creates public links, accepts uploads, stores analysis, sends SMS. | Token expiry, upload caps, disabled SMS gate, deletion/export coverage. |
| Telegram callback webhook | Telegram secret plus CallSession owner | Pickup, text reply, voicemail, ignore, callback, follow-up text. | Same gate registry as iOS; no global user fallback; cross-tenant/resource spoof test. |
| Twilio webhooks | Twilio signature plus phone/CallSession mapping | Routes calls, redirects calls, writes call state, sends voicemail SMS. | Contractor resolution, verification-call bypass, CallSession idempotency. |
| Contractor provisioning and deletion endpoints | authenticated contractor plus contractor owner | Provisions/releases Twilio numbers; deactivates/deletes account. | Confirmation, idempotency, partial-failure handling, deletion completeness. |
| vCard generation/download | contractor at generation plus HMAC token at download | Creates public contact-card URL and serves vCard. | HMAC secret, expiry, approved fields, forged token rejection. |
| Conference/warm-transfer helpers | CallSession owner | Redirects callers, creates conferences, adds/removes participants. | Conference ownership binding, duplicate pickup, rollback to screening on failure. |

### 18.2 Sequenced Rollout and Stop Gates

Implementation must be planned in this order:

1. **Phase 0: Safety audit and gate existing behavior.**
   - Stop gate: all outbound side-effect paths are inventoried and default-off where not production-ready.
2. **Phase 1: Trust, security, and call reliability.**
   - Stop gate: tenant isolation, deletion, encryption, push/log privacy, and CallSession race tests are defined before feature UI work.
3. **Phase 2: Verified forwarding setup.**
   - Stop gate: readiness cannot display "ready" until server-side verification succeeds.
4. **Phase 3: Backend contracts.**
   - Stop gate: readiness, verification sessions, CallSession, jobs/job cards, action statuses, and gated actions have API/schema contracts and tests.
5. **Phase 4: Dispatch, Calls, and Kevin UI.**
   - Stop gate: UI surfaces only states and actions the backend can prove.
6. **Phase 5: SMS/A2P.**
   - Stop gate: A2P proof, opt-out, delivery tracking, send limits, and failure UI are complete.
7. **Phase 6: Booking and integrations.**
   - Stop gate: owner confirmation, idempotency, duplicate detection, integration token protection, and rollback are complete.
8. **Phase 7: broader launch.**
   - Stop gate: reliability targets, privacy labels, deletion behavior, disclosure, and staging smoke tests pass.

If a later phase is incomplete, earlier phases must not expose copy, buttons, notifications, or onboarding claims that imply it is live.

### 18.3 Branch, Environment, and Release Safety

v2 implementation must not start from the current dirty, diverged local `main` worktree.

Required release-safety rules:

- Start implementation from a clean branch or clean worktree based on an agreed remote base.
- Use the `codex/` branch prefix unless the user asks otherwise.
- Do not push directly to `main` during v2 work.
- Do not depend on local `main` because it is currently ahead, behind, and dirty.
- All v2 feature flags default off in production.
- Staging validation must pass before any production path is touched.
- Any Firestore or RTDB migration must have backup, dry-run, rollback, and idempotency steps.
- Account deletion and Twilio number release require explicit confirmation, idempotency, and a recovery/rollback decision for partial failures.

### 18.4 Authorization and Tenant Isolation Contract

Contractor identity must come from authenticated server-side context, token claims, verified device/session state, or a server-side registry. Client-provided contractor IDs are filters, not authority.

Every endpoint and side-effect path must enforce ownership before read or write:

- Call list and call detail.
- Mark-read and action-status updates.
- Active-call state.
- Dispatch actions.
- Job card list, detail, create, update, archive, and delete.
- Verification sessions and readiness.
- Contacts and trust history.
- Export and deletion.
- SMS/MMS actions.
- Integration connect, disconnect, token refresh, booking, and job creation.

No endpoint may update arbitrary `call_sid`, `job_id`, `verification_session_id`, or integration resource IDs without checking contractor ownership. Cross-tenant negative tests are required for each write path.

### 18.5 Sensitive Data and Privacy Contract

Sensitive data includes more than transcripts. v2 must classify and protect:

- Caller phone numbers and names.
- Contacts and caller contacts.
- Full transcripts and live transcript buffers.
- Summaries, urgency labels, route reasons, and trust signals.
- Job cards, addresses, property type, issue descriptions, safety flags, and preferred times.
- Business knowledge and service-area data.
- RTDB active-call state.
- Push payloads and notification metadata.
- LLM prompts, tool inputs, tool outputs, and extraction payloads.
- SMS/MMS bodies, delivery status, opt-out state, and messaging logs.
- Integration tokens, calendar data, CRM/customer data, and booking payloads.
- Export bundles and deletion audit records.

Production requirements:

- Encryption must fail closed for sensitive call-derived records in production. Plaintext fallback is allowed only in local development or for explicitly migrated legacy records.
- Logs must not include raw caller speech, full phone numbers, full tokens, transcripts, detailed issue text, integration payloads, or tool arguments.
- Lock-screen push payloads must avoid raw speech, transcript snippets, full phone numbers, address details, and sensitive issue details.
- Exports must be contractor-scoped and logged without exposing exported content in logs.
- Deletion must remove or irreversibly anonymize every sensitive record where legally allowed, and it must report partial failures.

### 18.6 Readiness Contract

Readiness should be a server-calculated state exposed to iOS. iOS can cache it, but it cannot create readiness by itself.

Readiness inputs:

- Contractor active state.
- Subscription status and tier.
- Kevin number assignment.
- Forwarding verification status.
- Push and VoIP token freshness.
- Screening engine availability.
- Call-session subsystem health.
- A2P/SMS gate status.
- Integration gate status.
- Privacy/security config status.

Readiness statuses:

- `ready`
- `setup_needed`
- `verification_pending`
- `not_screening`
- `needs_reverification`
- `expired`
- `degraded`
- `offline_unknown`

The readiness response must include:

- status
- owner-facing reason
- blocking requirements
- safe primary action
- last verified timestamp
- next reverification reason when applicable
- gated feature statuses for SMS and booking

### 18.7 Forwarding Verification Contract

Forwarding verification requires a server-side verification session.

Verification session fields:

- `verification_session_id`
- `contractor_id`
- `user_phone`
- `kevin_number`
- `carrier_type`
- `status`
- `created_at`
- `expires_at`
- `expected_test_call_id` or equivalent nonce
- `attempt_count`
- `failure_reason`
- `verified_at`
- `verified_by` for manual support verification

Verification rules:

- A direct call to the Kevin number can prove the Kevin number works, but cannot prove missed-call forwarding is active.
- Verification calls must bypass normal AI screening and be identified as test calls.
- Incoming Twilio webhooks must match contractor, Kevin number, expected caller/test identity, and active session window before marking verification as successful.
- Expired or mismatched verification calls must not mark readiness as verified.
- Manual support verification must be server-side, auditable, and show source and timestamp.

### 18.8 CallSession Contract

CallSession is the canonical lifecycle for live and completed calls. Firestore should be the canonical durable source. RTDB can be a live mirror for low-latency iOS updates.

Required CallSession fields:

- `call_sid`
- `contractor_id`
- `caller_phone`
- `caller_name`
- `route`
- `route_reason`
- `state`
- `state_updated_at`
- `conference_name`
- `conference_sid`
- `ws_token_hash` or equivalent server-side authorization material
- `started_at`
- `media_connected_at`
- `first_audio_at`
- `owner_action`
- `ended_at`
- `failure_reason`
- `trace_id`

Required states:

- `pending`
- `routing`
- `screening_connecting`
- `screening`
- `taking_message`
- `pickup_requested`
- `pickup_connecting`
- `owner_in_call`
- `caller_hung_up`
- `completed`
- `failed`
- `cleanup_needed`

CallSession requirements:

- WebSocket authorization must not depend on a background task that can lose a race with Twilio media connection.
- Every transition must be idempotent.
- Twilio callbacks, app actions, and cleanup jobs must use the same state machine.
- RTDB live state must be contractor-scoped and must not contain more sensitive data than the privacy contract allows.
- A per-call trace must cover route, APNs, media connect, first audio, STT/LLM/TTS timing, app action, conference join, summary, job-card extraction, and cleanup.

### 18.9 Jobs, Job Cards, and Calls Queue Contract

v2 uses both calls and jobs:

- `calls` remains the canonical telephony record: call SID, route, outcome, transcript reference or encrypted transcript, timing, trust, and owner pickup details.
- `jobs` is the canonical business work item and job card for qualified business calls.
- A v2 job card has one primary `call_sid`; v2 should enforce idempotency so a call cannot create duplicate jobs.
- The Calls queue is returned by backend APIs that join call and job-card summary fields server-side. iOS should not query Firestore directly or infer urgency from transcript text.

Required job-card fields:

- all fields listed in Section 8.3
- `job_id`
- `contractor_id`
- `source_call_sid`
- `status_updated_at`
- `status_updated_by`
- `idempotency_key`
- `retention_policy`
- `encryption_version`

Required indexes:

- contractor plus created time.
- contractor plus owner action status plus created time.
- contractor plus urgency plus created time.
- contractor plus source call SID.

Required rules:

- Job cards are contractor-scoped.
- Job cards follow call-derived data encryption, logging, export, deletion, and retention rules.
- Jobs should not store raw transcript content unless encrypted and explicitly required.
- Existing `jobs` records created before v2 need a migration or compatibility plan before the Calls queue relies on them.

### 18.10 Action Status and Gated Action Contract

Action statuses:

- `new`
- `needs_callback`
- `waiting_on_customer`
- `booked_externally`
- `dismissed`
- `spam_blocked`
- `archived`

Allowed transitions:

- `new` to `needs_callback`, `waiting_on_customer`, `booked_externally`, `dismissed`, `spam_blocked`, or `archived`.
- `needs_callback` to `waiting_on_customer`, `booked_externally`, `dismissed`, or `archived`.
- `waiting_on_customer` to `needs_callback`, `booked_externally`, `dismissed`, or `archived`.
- `booked_externally`, `dismissed`, `spam_blocked`, and `archived` can reopen to `needs_callback` only through an owner action.

Every status update must store:

- actor type: owner, support, system, or automation.
- actor ID when available.
- previous status.
- new status.
- reason.
- timestamp.

Gated side-effect actions:

- text reply.
- automatic caller follow-up.
- send booking link.
- create Jobber job.
- create Google Calendar event.
- send estimate link.
- send vCard MMS.
- block caller.

Each gated action requires:

- contractor ownership check.
- feature flag and entitlement check.
- compliance gate check.
- owner confirmation unless explicitly approved as automation.
- idempotency key.
- delivery or integration result state.
- audit event without sensitive payload content.

### 18.11 Dispatch State and Action Table

| State | Owner-facing message | Source of truth | Primary action | Secondary action | Disabled or gated actions | Recovery and accessibility |
|---|---|---|---|---|---|---|
| Ready | "Kevin is screening missed calls." | Server readiness `ready`. | View Calls. | Recheck setup. | SMS/booking only if gates ready. | VoiceOver: "Kevin is ready and screening missed calls." |
| Setup needed | "Finish setup before Kevin can screen calls." | Server readiness blockers. | Continue setup. | Contact support. | Live screening claims. | Explain exact blocker and next step. |
| Verification pending | "Waiting for the forwarding test call." | Verification session. | Check status. | Retry test. | Mark ready. | Include timeout and instructions. |
| Not screening | "Kevin is not screening missed calls yet." | `skipped`, `failed`, or missing verification. | Verify forwarding. | Call Kevin number to test number only. | Ready claims. | Explain direct Kevin-number call is not forwarding proof. |
| Needs reverification | "Forwarding needs to be verified again." | Readiness reverification reason. | Reverify. | Review carrier settings. | Ready claims. | State why reverification is needed. |
| Expired | "AI screening is paused until the plan is active." | Subscription status. | View plans. | Restore purchase. | AI screening, SMS, booking. | Explain what stops on expiry. |
| Offline/degraded | "Kevin cannot confirm setup right now." | Backend or network health. | Retry. | View last known status. | New readiness claims. | Do not claim screening is active unless cached status is clearly labeled. |
| Active screening | "Kevin is talking with the caller." | CallSession `screening`. | Pick up. | Let Kevin take message. | Text/booking unless gates ready. | VoiceOver names consequence of each call action. |
| Taking message | "Kevin is taking a message." | CallSession `taking_message`. | Return to Dispatch. | View transcript. | Pick up if caller already gone. | Show whether caller is still connected. |
| Pickup requested | "Connecting you to the caller." | CallSession `pickup_requested` or `pickup_connecting`. | Wait. | Cancel if safe. | Duplicate pickup. | Show progress and failure fallback. |
| Owner in call | "You are connected." | CallSession `owner_in_call`. | End call in phone UI. | Return to app. | Let Kevin take message. | Do not hide active call state. |
| Caller hung up | "Caller hung up." | CallSession `caller_hung_up`. | View call details. | Mark handled. | Pick up. | Show whether a job card or transcript is available. |
| Kevin failed | "Kevin could not answer this call." | CallSession `failed`. | Call back. | Report issue. | Mark ready from this state. | Explain failure without blaming user. |
| Push disabled during call | "Pickup alerts may not work." | Push permission plus active CallSession. | Open Settings. | Continue watching. | Push-dependent actions. | Include VoiceOver label with consequence. |
| Network degraded during call | "Live updates may be delayed." | App network state plus CallSession. | Retry connection. | Call back if available. | Actions requiring fresh state. | Avoid destructive actions until state refreshes. |

### 18.12 Verified Setup UX Flow

Verified setup must cover:

- First-run setup.
- Skipped setup.
- Failed verification.
- Reverification.
- Carrier switch.
- Single-phone user confusion.
- Manual support verification.

Required UX flow:

1. Explain that the user's normal phone rings first and Kevin catches missed calls only after forwarding is active.
2. Let the user choose carrier type, with Verizon separated from default codes.
3. Launch the correct carrier code.
4. Start a server-side verification session and show a countdown.
5. Instruct the user what to expect during the test call.
6. Show `verification_pending` while the backend waits.
7. On success, show verified date and last test-call result.
8. On failure, show the specific failure reason and one repair action.
9. If skipped, keep a persistent "not screening" state in Dispatch and Kevin.
10. If manually verified by support, show verified status with support timestamp.

Failure copy must distinguish:

- "Your Kevin number works, but forwarding is not verified."
- "We did not receive the forwarding test call."
- "The carrier code may not have completed."
- "This looks like a direct call to Kevin, not a forwarded missed call."
- "Verification expired. Start a new test."

### 18.13 Calls and Job-Card Workflow

Default Calls view:

- Opens to `Needs action`.
- Sorts urgent calls first, then newest calls.
- Shows "All" as a secondary filter.
- Keeps read/unread separate from action status.

Row hierarchy:

1. Caller or formatted phone.
2. Urgency and action status.
3. One-line job summary or call summary.
4. Missing critical field indicator when address, callback number, or issue is missing.
5. Time and unread badge.

Job-card detail:

- Job card appears before transcript.
- Low-confidence fields display as "Needs review."
- Missing fields have actions such as call back, ask for address, or mark not needed.
- Transcript is evidence, not the primary workflow.
- Empty or unavailable transcript state explains whether recording/transcription failed or was intentionally unavailable.
- Empty or unavailable job-card state offers "create manually" and "mark not a lead."

Status changes:

- Archive, dismiss, spam/block, and booked actions require confirmation.
- Blocking a caller explains that future calls may be blocked or routed differently.
- Reopening an archived or dismissed item requires an owner action.
- Status updates must survive reinstall because the server is source of truth.

### 18.14 Copy Gating Checklist

No onboarding, paywall, Dispatch, Calls, Kevin, push notification, App Store, website, or support copy may promise these until their gates pass:

- Text reply.
- Automatic caller follow-up.
- Caller SMS confirmation.
- MMS/vCard follow-up.
- Booking links.
- Automatic Jobber job creation.
- Google Calendar event creation.
- Estimates or estimate links.
- Deletion of all data unless backend deletion matches the promise.
- "Kevin is ready" unless forwarding is server-verified.

Allowed copy before gates pass:

- "Follow-up recommended."
- "Booking setup available soon."
- "Texting is not enabled yet."
- "Kevin number works; forwarding still needs verification."

### 18.15 Additional Verification Required Before Implementation Planning

Before implementation planning begins, the amended spec must be re-reviewed for:

- Phase 0 audit completeness.
- Endpoint-level ownership and tenant isolation.
- Sensitive-data classification.
- Production safety gates.
- CallSession/readiness/verification/job-card/action-status contracts.
- Dispatch state/action table.
- Verified setup UX.
- Calls/job-card workflow.
- Copy gating.

The implementation plan must later require:

- Cross-tenant backend tests.
- Startup/config tests for production encryption.
- Push-payload and log privacy audits.
- Deletion/export integration tests.
- Failing-then-passing CallSession race test.
- iPhone SE and Pro Max visual QA.
- VoiceOver, Dynamic Type, high contrast, reduced motion, dark mode, pseudo-localization, and non-English transcript checks.

## 19. Acceptance Criteria

### 19.1 Product Acceptance

- The app presents `Dispatch`, `Calls`, and `Kevin` as the primary IA.
- Dispatch idle state clearly says whether Kevin is actually screening missed calls.
- Dispatch active state shows a decision brief above transcript.
- "Ignore" is replaced with "Let Kevin take message."
- Text reply and booking actions are hidden or disabled until their production gates are complete.
- Calls behaves like a work queue with filters and action statuses.
- Call detail prioritizes job card over transcript.
- Kevin tab exposes setup, business controls, intake, privacy, plan, and integrations.
- Phase 0 confirms existing backend side effects are gated before UI promises them.

### 19.2 Verified Setup Acceptance

- Tapping a forwarding code cannot mark Kevin as ready by itself.
- Skipping setup leaves a persistent "not screening" state.
- A successful verification test marks forwarding verified server-side.
- Changing phone number, Kevin number, or carrier type requires reverification.
- Direct calls to the Kevin number are not treated as proof of forwarding.
- Verification sessions are server-side, expiring, contractor-scoped, and auditable.

### 19.3 Intake and Job Card Acceptance

- The first six trade packs exist and can produce structured job cards.
- Every qualified business call has a backend-generated summary and extraction confidence.
- Urgency and service category come from backend state, not client-only heuristics.
- Missing fields are visible and actionable.
- Job cards are contractor-scoped.
- Calls and jobs have a defined data contract, idempotency model, retention model, and migration plan.

### 19.4 Trust and Reliability Acceptance

- Account deletion behavior matches app copy and is covered by automated tests.
- Transcript encryption fails closed in production.
- Caller disclosure is present in the greeting or legally validated configuration.
- Privacy labels and privacy manifest match real data handling.
- Production logs are privacy-safe.
- The media-stream active-call race is covered by a failing test first and then fixed.
- Per-call tracing exists for key lifecycle events.
- Push payloads and RTDB live state follow the sensitive-data contract.
- Every call, job-card, verification, active-call, export, delete, and integration endpoint enforces contractor ownership.

### 19.5 Launch Acceptance

- Internal test passes on iPhone SE-size and Pro Max-size viewports.
- Backend tests cover tenant scoping, deletion, transcript encryption config, verification state, call-session lifecycle, and job-card extraction.
- No public copy promises SMS follow-up, text reply, booking, or integrations before those tracks are production-ready.
- Broader launch waits until trust and reliability blockers are closed or explicitly removed from the launch scope.
- Implementation starts from a clean branch or worktree, not the current dirty/diverged local `main`.

## 20. Non-Goals for v2

- Building a full business phone system.
- Making Personal mode the main paid wedge.
- Shipping broad consumer spam-blocker positioning.
- Auto-booking jobs without owner confirmation.
- Promising caller text reply before A2P/10DLC and delivery handling are ready.
- Replacing the full app with the live dispatch screen.
- Broad launch before forwarding verification and trust blockers are fixed.

## 21. Spec Assumptions for Approval

This spec assumes:

- Business-first positioning is approved.
- The three-tab IA is approved.
- Verified forwarding is mandatory for "ready" status.
- Job cards are the central product differentiator.
- SMS and booking are priority tracks but remain gated.
- Business stays at $49.99/month as founding pricing unless the user chooses a pricing change.
- Business Pro can be repositioned toward $99-$149/month once integrations and automation justify it.
- Implementation planning should be split into trust/reliability, IA/UI, verified setup, job cards, SMS/A2P, and booking/integrations.
- The panel-required gates in Section 18 must be accepted before implementation planning.

## 22. Source Context

Local context used while writing this spec:

- `AGENTS.md`
- `HANDOFF-2026-06-30-PRODUCT-PANEL.md`
- `ios/Kevin/Views/ContentView.swift`
- `ios/Kevin/Views/OnboardingView.swift`
- `ios/Kevin/Views/CallHistoryView.swift`
- `ios/Kevin/Views/SettingsView.swift`
- `ios/Kevin/Models/Call.swift`
- `app/api/calls.py`
- `app/db/calls.py`
- `app/db/jobs.py`
- `app/services/job_card.py`
- `app/services/post_call.py`
- `app/services/sms.py`
- `app/services/state_machine.py`
- `app/webhooks/media_stream.py`
- `app/webhooks/twilio_incoming.py`
- `docs/pricing-competitive-strategy.md`
