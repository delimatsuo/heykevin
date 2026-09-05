# Hey Kevin — Canonical Source Roadmap & Reconciliation

**Reconciled source baseline:** `main` (`c093ed6ef1c505e8d68f01d6f6f365bf2553ac55`)
**Project:** Kevin (`delimatsuo/heykevin`)

---

## 1. Evidence Ceiling & Verification Scope

This document is the canonical source roadmap for the Hey Kevin repository. The
Google Calendar reschedule-fencing section describes the merged source slice; PR
#209 records the reviewed history and merge commit. Evidence is limited to source,
automated tests, and Git history.

> [!IMPORTANT]
> **Evidence Ceiling**: Source implementation and automated unit tests prove repository code correctness and simulated contracts only. They do **not** prove:
> - Active Cloud Run revisions, service configurations, or runtime environment variables in staging or production.
> - Secret Manager secret provisioning or active encryption keys.
> - Live provider configuration or account standing (Twilio, Deepgram, ElevenLabs, Gemini Live, Google Workspace, Jobber, Apple APNs/StoreKit).
> - Live Firestore or RTDB account documents, active flags, or customer data.
> - Physical iOS device execution, carrier GSM forwarding behavior, or cellular network audio quality.
> - App Store review status, TestFlight builds, or live telephonic traffic.
>
> Non-source observations (such as GitHub issue queries) are explicitly labeled as dated external snapshots.

---

## 2. Recently Completed Source-Only Slices

The following discrete backend hardening slices have been merged to `main` and verified through unit tests:

1. **Tenant-Explicit Contact Isolation (PR #204 / merge `d5cb3dc`)**
   - **Core paths:** [`app/services/adaptive_trust.py`](../app/services/adaptive_trust.py), [`app/services/lookup.py`](../app/services/lookup.py), [`tests/unit/test_call_history_tenant_isolation.py`](../tests/unit/test_call_history_tenant_isolation.py), [`tests/unit/test_tenant_explicit_contact_helpers.py`](../tests/unit/test_tenant_explicit_contact_helpers.py).
   - **Outcome:** Enforces tenant-explicit contact lookup and eliminates global un-scoped fallback in contact lookup and adaptive trust resolution, ensuring contact evaluation is strictly isolated to the contractor tenant.

2. **Default-Off Integration-Token Envelope Hardening (PR #205 / merge `7676458`)**
   - **Core paths:** [`app/services/integration_tokens.py`](../app/services/integration_tokens.py), [`app/services/integration_token_mutations.py`](../app/services/integration_token_mutations.py), [`app/db/contractors.py`](../app/db/contractors.py), [`app/api/integrations.py`](../app/api/integrations.py), [`docs/runbooks/integration-token-envelope.md`](runbooks/integration-token-envelope.md), [`tests/unit/test_integration_token_envelope.py`](../tests/unit/test_integration_token_envelope.py).
   - **Outcome:** Implemented context-bound AES-256-GCM encryption envelopes for Jobber and Google Calendar credentials with atomic CAS transitions, monotonic envelope floors (`jobber_token_envelope_required`, `google_calendar_token_envelope_required`), global write activation toggle (`INTEGRATION_TOKEN_ENCRYPTED_WRITES_ENABLED`), pair-valid boundary validation, and fail-closed downgrade protection.

3. **Trusted Returning-Caller Intake Identity Hydration (PR #206 / merge `235cb8b`)**
   - **Core paths:** [`app/services/gemini_pipeline.py`](../app/services/gemini_pipeline.py), [`app/services/live_intake_controller.py`](../app/services/live_intake_controller.py), [`tests/unit/test_live_intake_controller.py`](../tests/unit/test_live_intake_controller.py), [`tests/unit/test_receptionist_intelligence.py`](../tests/unit/test_receptionist_intelligence.py).
   - **Outcome:** Hydrates caller identity and first-name greetings for recognized returning callers during conversational intake in Gemini live intake flows.

4. **International Owner-Phone Canonicalization (PR #207 / merge `4a77364`)**
   - **Core paths:** [`app/api/contractors.py`](../app/api/contractors.py), [`app/db/contractors.py`](../app/db/contractors.py), [`tests/test_apple_auth.py`](../tests/test_apple_auth.py), [`tests/unit/test_account_dedupe.py`](../tests/unit/test_account_dedupe.py), [`tests/unit/test_audit_pr_a.py`](../tests/unit/test_audit_pr_a.py).
   - **Outcome:** Implemented for account creation and deduplication boundaries: canonicalizes international owner phone numbers and persists `owner_phone_e164` to prevent multi-region format aliasing and duplicate tenant creation. `owner_phone` and `owner_phone_e164` remain strictly protected from generic `PATCH /api/contractors/{contractor_id}` under F-04; any future owner phone mutation requires a separately designed possession-verified rebind flow with collision checks and atomic pair update.

5. **Provider-Safe Google Calendar Reschedule Fencing (PR #209 / merge `10af26bb065e904799257d2f47b9b5c45431c997`)**
   - **Core paths:** [`app/db/contractors.py`](../app/db/contractors.py), [`app/db/service_requests.py`](../app/db/service_requests.py), [`app/services/calendar.py`](../app/services/calendar.py), [`app/services/google_calendar_request_provider.py`](../app/services/google_calendar_request_provider.py), [`app/services/integration_tokens.py`](../app/services/integration_tokens.py), [`app/services/integration_token_mutations.py`](../app/services/integration_token_mutations.py), [`app/services/service_request_recovery.py`](../app/services/service_request_recovery.py), [`app/services/service_request_repository.py`](../app/services/service_request_repository.py), [`tests/unit/test_calendar_appointment_mutations.py`](../tests/unit/test_calendar_appointment_mutations.py), [`tests/unit/test_contractor_protected_fields.py`](../tests/unit/test_contractor_protected_fields.py), [`tests/unit/test_google_calendar_request_provider.py`](../tests/unit/test_google_calendar_request_provider.py), [`tests/unit/test_integration_token_envelope.py`](../tests/unit/test_integration_token_envelope.py), [`tests/unit/test_service_request_repository.py`](../tests/unit/test_service_request_repository.py), [`tests/unit/test_service_request_recovery.py`](../tests/unit/test_service_request_recovery.py), [`tests/unit/test_service_request_firestore.py`](../tests/unit/test_service_request_firestore.py).
   - **Outcome:** Delivered provider-safe Google Calendar reschedule fencing in backend services with fresh ETag invariant, UTC whole-second comparison, atomic recovery finalization, and fail-closed uncertainty classification.

### 2026-09-03 Follow-Up Wave (PRs #223–#233)

Eleven pull requests merged to `main` in one session, source-only. Nothing in
this wave has been deployed — production and staging both still serve the
pre-wave code as of this writing. Each closed one or more entries reconciled
in §6.A below.

1. **PR #223 (merge `9202885`):** Pinned twilio-python's request logging to
   `WARNING`; caller phone numbers were previously logged at `INFO`.
2. **PR #224 (merge `7b662bd`):** Corrected the deploy docs: staging dispatch
   requires `candidate_sha`; production dispatch must not pass it; only one
   `workflow_dispatch` run from `main` proceeds at a time.
3. **PR #225 (merge `b06aab4`):** The lapsed-number release's Apple ownership
   check now walks both App Store environments; the deleted-app release
   guard now honours `last_inbound_call_at`.
4. **PR #226 (merge `f381851`):** Twilio provisioning — a missing regulation
   now maps to "not yet supported" instead of "No phone numbers available in
   your area"; the regulatory address guard now runs before the Twilio
   client is constructed.
5. **PR #227 (merge `9908814`):** App Store webhook signature verification
   hardened — full 3-certificate chain validation, Apple's marker OIDs
   checked, `bundleId` mismatch fails closed across the payload locations
   Apple may populate.
6. **PR #228 (merge `ddf666d`):** Added an owner-run CLI to replay App Store
   notifications Apple failed to deliver, with a guard that skips replaying
   a stale deactivation against a contractor who has since renewed.
7. **PR #229 (merge `5242134`):** Wave-1 cleanup — fixed the stale
   deploy-workflow comment in `CLAUDE.md`/`AGENTS.md`, removed the dead
   `_get_appstore_url` resolver, extracted a shared inbound-call-stamp
   helper used by both release guards, named the environment in Apple
   non-200 webhook errors, and made a Firestore-reaching provisioning test
   run offline.
8. **PR #230 (merge `ef9813c`):** The App Store webhook verifier now checks
   certificate validity at the notification payload's `signedDate` rather
   than at `now()`, matching Apple's own offline verifier semantics.
9. **PR #231 (merge `5ae6845`):** Added an `isfinite` guard on `signedDate`;
   a non-dict JWS header now returns HTTP 400 instead of 500; the audit and
   subscription-security test suites run offline.
10. **PR #232 (merge `81dcabd`):** Added
    [`docs/specs/phone-rebind-possession-verified.md`](specs/phone-rebind-possession-verified.md),
    the design spec for the possession-verified owner-phone rebind flow.
11. **PR #233 (merge `387c847`):** iOS business address and city capture
    shipped for the six regulatory countries (`DE`, `FR`, `IT`, `ES`, `PT`,
    `BR`).

### 2026-09-04 Wave & Build 37 Release (PRs #234–#239)

Six pull requests merged to `main`, completing client and backend screening ergonomics, stability, and build 37 release to TestFlight:

1. **PR #234 (merge `6583a0c`):** Wave final sweep — reconciled the roadmap
   after the 2026-09-03 follow-up wave and addressed review cleanups.
2. **PR #235 (merge `8067f30`):** App Store replay notification fetch usability
   — typed exception handling, honest `--days` current-instant bound labeling,
   and explicit stale notification skip diagnostics.
3. **PR #236 (merge `9752e06`):** Forwarding carrier documentation reconciliation
   — aligned US/CA carrier notes with the client's tested Verizon/GSM split.
4. **PR #237 (merge `e3addc6`):** iOS 1.2.11 candidate preparation (build 36).
5. **PR #238 (merge `407bf0b`):** Personal mode screening — preserved the 30-second
   silence hold threshold and suppressed business intake tool declarations
   in `app/services/relay_pipeline.py`.
6. **PR #239 (merge `c093ed6`):** Screening summary push notifications and in-app
   feedback/review system:
   - **In-Place Screening Summary Notification:** Emits an in-place notification
     update (`apns-collapse-id: call_<call_sid>`) when Kevin places the caller
     on hold, showing caller name and reason directly on the lock screen.
     Registers `SCREENING_CALL` category with 1-tap `PICK_UP_ACTION` quick pickup.
   - **Feedback & Review System:** `StoreReviewManager.swift` handles StoreKit
     in-app review requests at milestones (>= 3 calls or confirmed appointment,
     guarded against active calls, 60-day rate-limited, version-gated) and
     provides in-app mailto support with diagnostic metadata in Settings.
   - **Release Candidate:** Bumped `CURRENT_PROJECT_VERSION` to 37, regenerated
     Xcode project, archived, exported, and uploaded to TestFlight.
   - **TestFlight Intake:** Validated (`BUILD-STATUS: VALID`), internal testing
     activated (`IN_BETA_TESTING`), What's New notes published via ASC API.
   - **Staging Deployment:** Deployed revision `kevin-api-staging-00165-kif`
     (`c093ed6`), verified via release smoke test.
   - **Production Deployment:** Workflow run `33938295394` dispatched from `main`
     at `c093ed6`, passed all CI test suites, and parked at the owner approval gate.

---

## 3. Internationalization Status (Tasks 1–8)

The internationalization plan ([`docs/superpowers/plans/2026-04-07-internationalization.md`](superpowers/plans/2026-04-07-internationalization.md)) defined eight tasks to support 9 countries (`US`, `CA`, `BR`, `GB`, `DE`, `FR`, `IT`, `ES`, `PT`). The table below reflects current source status versus owner/live gates:

| Task | Title | Source Status | Source Implementation Details | Owner / Live Gate Status |
|---|---|---|---|---|
| **Task 1** | Country Model & Detection | **Implemented** | Supported country constants, `country_code` field, and `detect_country_from_phone()` helper implemented in [`app/db/contractors.py`](../app/db/contractors.py) and [`app/api/contractors.py`](../app/api/contractors.py). | Verified in unit tests. Cloud Run deployment is an owner gate. |
| **Task 2** | International Twilio Provisioning | **Partial** | Asynchronous Twilio number search and `_create_regulatory_bundle()` helper implemented in [`app/db/contractors.py`](../app/db/contractors.py). Missing regulatory-country provider unit tests and iOS business address/city input. | Twilio regulatory compliance bundle submission/approval, international number purchases, and live qualification are owner-gated. |
| **Task 3** | Regional Dial-In Routing | **Partial** | `get_dial_in_number(country_code)` helper implemented in [`app/config.py`](../app/config.py) and consumed in [`app/services/warm_transfer.py`](../app/services/warm_transfer.py). | `DIAL_IN_NUMBERS` environment variable provisioning, regional number inventory, and live conference testing are owner-gated. |
| **Task 4** | Language Voice Mapping | **Implemented** | `GEMINI_VOICES` mapping per ISO language code implemented in [`app/services/gemini_pipeline.py`](../app/services/gemini_pipeline.py). | Live audible voice quality and latency qualification over Twilio Media Streams are owner-gated. |
| **Task 5** | Forwarding Instructions API | **Partial** | `GET /api/forwarding-instructions` serves per-country GSM codes ([`app/api/forwarding.py`](../app/api/forwarding.py)); the generic `disable` now cancels the recommended no-reply mode (`##61#`, SC 61) and GSM rows carry `disable_all` / `disable_unanswered` / `disable_everything`. iOS ([`OnboardingView.swift`](../ios/Kevin/Views/OnboardingView.swift), [`SettingsView.swift`](../ios/Kevin/Views/SettingsView.swift)) resolves the account country (Task 7; device region as fallback) and dials the server's codes for every non-NANP country, falling back to the built-in codes offline or against a backend without the new shape ([`ForwardingInstructions.swift`](../ios/Kevin/Services/ForwardingInstructions.swift)). Production has served the fixed table since 2026-09-02 (revision `kevin-api-00264-stg`); the server-driven client path ships in the first iOS build after 1.2.10 (35). **Source Gap**: US/CA keep the existing client Verizon/GSM behaviour — the in-repo US code sources disagree (`CLAUDE.md` vs the table's `notes`, which overclaim for T-Mobile/AT&T) and the server deliberately asserts no granular NANP cancel codes until that is decided. | Carrier-network validation across international operators is owner-gated. |
| **Task 6** | International Phone Normalization | **Partial** | Implemented for account creation and deduplication: E.164 normalization logic in [`app/utils/phone.py`](../app/utils/phone.py); contractor creation canonicalization and account deduplication verified by PR #207 tests ([`tests/unit/test_account_dedupe.py`](../tests/unit/test_account_dedupe.py)). `owner_phone` and `owner_phone_e164` remain in `PROTECTED_FIELDS` under F-04 to prevent account hijacking via generic PATCH. Any future phone update requires a possession-verified rebind flow. | Verified in creation unit tests. |
| **Task 7** | Settings Country Code Update | **Implemented (source)** | `GET /api/settings` returns root-authoritative canonical `country_code` with fallback to `US`; `PUT /api/settings` validates and updates root contractor `country_code` with normalization and direct unit test coverage in [`tests/unit/test_settings_country.py`](../tests/unit/test_settings_country.py). iOS: a Country picker in [`SettingsView.swift`](../ios/Kevin/Views/SettingsView.swift) (Account & Plan) writes through `PUT /api/settings` and adopts only a server-confirmed value ([`SettingsCountry.swift`](../ios/Kevin/Services/SettingsCountry.swift)); the account country is mirrored in `AppState.countryCode` and takes precedence over the device region for forwarding codes; onboarding and Settings adopt it wherever a profile or provisioning response is in hand — every path that reaches the forwarding step, with provisioning now returning the resolved `country_code` — so first-run forwarding keys on it too. | Backend unit tests; iOS XCTests cover the parser, country precedence and picker decisions (not the network write path); live carrier qualification remains owner-gated (Task 8). |
| **Task 8** | Staging / Production Qualification | **Owner-Gated** | Multi-country test matrix defined in plan. | Requires staging deployment, active credentials, real phone numbers, and physical device testing. |

---

## 4. Phase 0 Status & Historical Artifact Clarification

1. **Source Status:**
   - Phase 0 backend safety gates, CAS invariants, fail-closed integration boundaries, and subsequent architectural improvements have long since been merged into `main`.
2. **Historical Artifact Demarcation:**
   - [`docs/security/phase0-release-readiness.md`](security/phase0-release-readiness.md) represents immutable historical evidence as of 2026-07-01, not an open pre-merge checklist.
   - The July 2026 staging smoke test executed against staging revision `kevin-api-staging-00032-tel` verified only enumerated read paths and disabled-gate boundaries; it did **not** prove enabled side effects, live carrier delivery, or full end-to-end call paths.
   - The June 30, 2026 account audit reflects an immutable historical snapshot where 0 of 95 production contractor documents had `gated_actions` or compliance flags. Any future release decision requires a freshly executed aggregate-only audit.
3. **Appointment-Confirmed Caller SMS Policy:**
   - In [`app/services/gated_actions.py`](../app/services/gated_actions.py), `ActionKey.APPOINTMENT_CONFIRMED_CALLER_SMS` is configured with `requires_flag=False` and `requires_sms_compliance=False`.
   - The registry policy specifies `requires_owner_confirmation=True` and `requires_idempotency=True`; `check_gated_action` permits execution when `context.owner_confirmed` is true or `automation_approvals[action]` is present.
   - In the current appointment confirmation call path, `owner_confirmed=True` is explicitly passed.
   - All other caller-facing SMS actions remain gated by default.
4. **Rollback Authority:**
   - Authoritative rollback procedures are encoded in [`.github/workflows/rollback.yml`](../.github/workflows/rollback.yml), supporting revision traffic-splitting and tagged redeployments for staging and production.
   - Executing a `workflow_dispatch` rollback and placing an incoming live call require separate owner authorization; the owner performs the live call.

---

## 5. Completed Slices: Provider-Safe Google Calendar Reschedule Fencing (PR #209 / merge `10af26bb065e904799257d2f47b9b5c45431c997`)

**Objective:** Deliver provider-safe Google Calendar reschedule fencing in backend services, default-off with zero live activation. Merged to `main` via PR #209 (`10af26bb065e904799257d2f47b9b5c45431c997`).

### Reschedule Fencing Contract
1. **Durable Bases & Fresh ETag Invariant:**
   - Base state truth is durably stored in `PreparedProviderOperation.base_request` / canonical Firestore aggregate; desired state truth is the persisted reschedule arguments plus `result.request`.
   - No base ETag is durably stored. Each mutation attempt or recovery refetch retrieves a fresh remote ETag via `GET`.
2. **Schedule Validation & Instant Comparison:**
   - Validate both base and desired timezone-aware, ordered schedule intervals (`start < end`).
   - Compare schedules using normalized UTC whole-second instants (converting to UTC and truncating microseconds to zero) so equivalent offsets and sub-second precision differences match cleanly. Outgoing PATCH request bodies emit normalized desired UTC whole-second RFC3339 timestamps.
   - All-day, cancelled, malformed, or recurring-master-shaped event resources fail closed.
3. **Pre-Mutation Evaluation:**
   - `GET` the bound event through the token-refresh wrapper; require a 2xx status, the exact requested event ID, a valid ETag, and valid non-cancelled start/end times.
   - A bound PATCH claim must CAS-match the exact generation, lifecycle epoch, and raw credential pair that authorized the GET. A controlled 401 retry may advance credentials only within the same lifecycle and must bind to the exact retry snapshot.
   - `current == desired` => Idempotent GET-only success; no `PATCH` issued.
   - `current differs from both base and desired` => Remote conflict; abort with failure and issue no `PATCH`.
   - `current == base` => Permitted conditional update; issue a `PATCH` updating only `start` and `end` with `If-Match: "<ETag>"`.
4. **Response Handling & Post-Condition Confirmation:**
   - Accept success only if the 2xx response body identifies the exact requested event and explicitly confirms the desired `start` and `end` schedule.
   - HTTP 412 Precondition Failed, timeout, transport error, 5xx, malformed/mismatched response, or token failure => return false/uncertain with zero second `PATCH` in that attempt.
5. **Durable Recovery & Bounded Convergence:**
   - Later durable recovery first performs exact-claim, GET-only reconciliation.
   - Before a reconciliation GET and again transactionally before finalization, the canonical authorizer validates exact bound claim/operation and lifecycle/generation; exact active and provider-connected state; a usable paired access/refresh credential set; token-envelope floor compliance; valid encrypted credential envelopes whose provider/token-kind AAD binding authenticates and decrypts when encrypted; the canonical required Google Calendar scope; and token-expiry metadata, when present, having finite-positive numeric shape. The source snapshot must carry a valid authoritative Firestore server `read_time`. Any inactive, disconnected, missing or unusable credential pair, plaintext below an enforced envelope floor, tampered or wrong-AAD envelope, reduced/malformed scope, malformed/non-positive/non-finite expiry metadata, lifecycle/claim mismatch, or missing/invalid `read_time` fails closed with zero provider construction or zero finalization writes as applicable.
   - If a matching started/uncertain claim exists and the remote event is already at `desired`, one Firestore transaction must verify the recovery lease, canonical proposal, exact claim identity, lifecycle counters, and raw-credential fingerprint before it both finalizes the canonical request and clears only that exact claim. No additional `PATCH` is issued.
   - If a matching claim exists but the remote result is `base`, third-state, malformed, unavailable, or otherwise unconfirmed, issue zero `PATCH`; bounded recovery retains the fence and advances toward `needs_review`.
   - Only an explicit `verified_absent` result from the durable authorizer may allow the ordinary fenced attempt to run. Read failures, malformed state, decryption failures, a different claim, and every other indeterminate state remain blocked with zero provider execution.
   - Never describe an ambiguous transport error as safely overwritten or blindly retried.
6. **Diagnostic Safety & Privacy:**
   - Fixed payload-safe diagnostic codes only; logs strictly exclude event IDs, URLs, ETags, OAuth tokens, customer identifiers, request IDs, schedule strings, and raw provider payloads.
7. **Bound Uncertainty & Fenced Recovery:**
   - Ambiguous Google Calendar reschedule PATCHes retain a bound started/uncertain claim; expiry alone never clears or quarantines it.
   - Disconnect stays blocked until desired-state reconciliation finalizes first and an exact claim clear follows.

### Targeted Implementation Allowlist (Source & Tests Only)
- [`app/db/contractors.py`](../app/db/contractors.py)
- [`app/db/service_requests.py`](../app/db/service_requests.py)
- [`app/services/calendar.py`](../app/services/calendar.py)
- [`app/services/google_calendar_request_provider.py`](../app/services/google_calendar_request_provider.py)
- [`app/services/integration_tokens.py`](../app/services/integration_tokens.py)
- [`app/services/integration_token_mutations.py`](../app/services/integration_token_mutations.py)
- [`app/services/service_request_recovery.py`](../app/services/service_request_recovery.py)
- [`app/services/service_request_repository.py`](../app/services/service_request_repository.py)
- [`docs/current-roadmap.md`](current-roadmap.md)
- [`docs/customer-memory-rollout.md`](customer-memory-rollout.md)
- [`tests/unit/test_calendar_appointment_mutations.py`](../tests/unit/test_calendar_appointment_mutations.py)
- [`tests/unit/test_contractor_protected_fields.py`](../tests/unit/test_contractor_protected_fields.py)
- [`tests/unit/test_google_calendar_request_provider.py`](../tests/unit/test_google_calendar_request_provider.py)
- [`tests/unit/test_integration_token_envelope.py`](../tests/unit/test_integration_token_envelope.py)
- [`tests/unit/test_service_request_repository.py`](../tests/unit/test_service_request_repository.py)
- [`tests/unit/test_service_request_recovery.py`](../tests/unit/test_service_request_recovery.py)
- [`tests/unit/test_service_request_firestore.py`](../tests/unit/test_service_request_firestore.py)

*Boundary Note:* The recovery worker and Firestore repository changed only where causal tests required exact-claim reconciliation and atomic canonical-finalization-plus-claim-clear. The two new protected contractor fields prevent client PATCH writes to lifecycle state. Evidence is source/mock-only; no live Google Calendar, Firestore recovery, staging deploy, feature flag activation, or provider qualification.

Shared-helper exception: Google Calendar authorization and recovery reuse the
provider-agnostic durable operation-intent parser and CAS helpers. This slice may
update those shared helpers only to enforce exact lifecycle/generation/raw-credential
binding, persist and parse the Google-only bound operation ID, transition ambiguous
Google PATCH outcomes to uncertainty, classify reconciliation authorization, and
atomically finalize the canonical request while clearing an exact reconciled claim.
Jobber regression
coverage must remain green; this does not authorize new Jobber behavior, provider
activation, or any live-provider change.

### Mutation-Effective Proof Targets

- Desired-current state yields GET-only success without issuing `PATCH`.
- Third-schedule event yields GET-only conflict failure without issuing `PATCH`.
- Missing base-equality guard causes tests to fail.
- Missing or malformed ETag prevents `PATCH` invocation.
- `PATCH` payload contains exact `If-Match` header and schedule-only body.
- A 2xx `PATCH` whose response body has a missing/wrong event ID or missing, malformed, or non-desired start/end returns false, retains canonical base/pending recovery, and never finalizes.
- Exact Firestore round trip: canonical aggregate remains base; pending arguments/result remain desired; recovery receives both exact schedules; no ETag field exists anywhere in the durable document.
- Exact 401 auth failure and retry sequences:
  1. `GET` 401 -> token refresh -> retried `GET` supplies the ETag used by `PATCH`.
  2. `PATCH` 401 -> token refresh -> retry uses the identical `If-Match` value and identical schedule-only body.
  3. HTTP 412 from that retry returns false with no further `PATCH`.
- Disconnect/reconnect, lifecycle changes, or raw-credential swaps between GET and bound claim acquisition cause zero PATCH; the same changes before a 401 retry cause zero retry PATCH.
- HTTP 412 response executes zero second `PATCH`.
- Timeout-after-PATCH followed by recovery `GET` at desired schedule finalizes exactly once with zero second `PATCH`.
- Cancellation or failure at the atomic recovery-finalization boundary leaves both the canonical proposal and exact provider fence retryable; a later GET-only reconciliation finalizes and clears both without a provider replay.
- Third-schedule divergence never `PATCH`es and reaches `needs_review` while retaining canonical base.
- Sensitive sentinels are completely absent from diagnostic logs.

Historical pre-repair evidence (2026-08-26): 496 focused tests and 3,765 total
repository suite tests previously passed in pre-repair evaluation; those prior
counts are historical pre-repair records and do not qualify the current tree.
Current exact-head verification belongs in the pull request and required hosted
CI. This is not live Google Calendar, Firestore, staging, production, or
customer-data qualification.

---

## 6. Categorization: Remaining Source Gaps vs. Owner / Live Gates

### A. Remaining Source Gaps (Repository Code & Tests)
- **Possession-Verified Phone Rebind Flow:** The design spec now exists —
  [`docs/specs/phone-rebind-possession-verified.md`](specs/phone-rebind-possession-verified.md)
  (PR #232, merge `81dcabd`) — covering collision checks and an atomic
  `owner_phone`/`owner_phone_e164` update. Implementation has not started; it
  waits on the owner decisions enumerated in the spec's §9. Generic PATCH
  must keep both fields protected under F-04 until then.
- **US/CA Forwarding Codes Decision — resolved (keep client, fix notes):**
  Per owner decision, the client keeps its tested Verizon/GSM split
  (`ForwardingCountry.nanp` in `ForwardingInstructions.swift`), where Verizon
  dials `*71` / `*73` and GSM carriers dial standard GSM MMI `*61*...#` /
  `##61#`. The server table's notes in `app/api/forwarding.py` have been
  corrected to state that `*71` / `*73` represents CDMA/Verizon forwarding rather
  than asserting it universally for all carriers, reconciling the server table with
  `CLAUDE.md` and the iOS client.
- **International forward target format:** the client strips `+` and dials the Kevin number as bare digits (`**61*15551234567#`). From a non-NANP SIM, a target in another country most likely needs the `+` international form in the MMI string; today's behaviour predates server-driven codes and has never been verified on a real carrier. Confirm on a live network (Task 8) before treating non-NANP forwarding as working end-to-end.
- **iOS International Address Capture — shipped (source):** Business street
  address and city capture for accounts in the six regulatory countries
  (`DE`, `FR`, `IT`, `ES`, `PT`, `BR`) merged in PR #233 (`387c847`).
  End-to-end provisioning in those countries remains gated on Twilio
  international number inventory and a live regulatory bundle round-trip
  (Task 8, owner-gated).
- **EU/BR number provisioning — `email` fix landed (source):** twilio 9.x `BundleList.create` requires `email`; `_create_regulatory_bundle` now passes `settings.twilio_regulatory_contact_email` and refuses clearly ("Regulatory contact email not configured") before any Twilio call when it is unset. `TWILIO_REGULATORY_CONTACT_EMAIL` is set on production (`kevin-api-00265-jr8`, 2026-09-03) but **not on staging** — after the next staging deploy, staging's EU/BR provisioning refuses on config until it is set there — and the code is inert until the next backend deploy (owner-gated). Live regulatory provisioning still needs Twilio international inventory and a real bundle round-trip (Task 8).
- **Regulatory provisioning follow-ups:** Twilio regulatory bundle addresses
  are still created with empty `region`/`postal_code`, which several
  regulatory countries require — only a live provisioning check (Task 8,
  owner-gated) can confirm whether that blocks bundle approval. (The
  "not yet supported" mapping and the Twilio-client-before-guard ordering
  closed in PR #226, merge `f381851`.)
- **App Store replay CLI usability — resolved (source):** PR #235 (merge `8067f30`)
  hardened `scripts/replay_appstore_notifications.py` and `app/services/appstore_replay.py`
  with typed exception handling naming failure targets, honest current-instant bound
  labeling for `--days`, and explicit logging when notifications are skipped against
  stored expiry timestamps.

### B. Owner / Live Gates (Explicitly Out of Bounds for Agent Tasks)
- Cloud Run staging and production deployments (`gcloud run deploy` or GitHub Actions deploy workflows).
- Feature flag enablement, backfills, or CAS runtime switches (`gated_actions`, `sms_compliance_status`, `INTEGRATION_TOKEN_ENCRYPTED_WRITES_ENABLED`, `jobber_token_envelope_required`, `google_calendar_token_envelope_required`).
- Secret Manager provisioning, key rotations, or environment variable updates (`INTEGRATION_TOKEN_ENCRYPTION_KEYS`, `DIAL_IN_NUMBERS`, `APNS_*`).
- Twilio A2P 10DLC registration, campaign submissions, and brand approvals.
- Twilio international phone number inventory procurement, regulatory bundle document submissions, and address verifications.
- Live Firestore / RTDB account audits or mutations.
- Live telephony calls, carrier diversion tests, or acoustic voice qualification.
- Physical iOS device testing, TestFlight uploads, or App Store submissions.
- Legacy Apple ID lookup removal: requires completion of the monitored client adoption window and separate explicit owner authorization.

---

## 7. Dated External Observations: GitHub Issues

- **Dated Observation (2026-08-25):** GitHub issue inventory observed on 2026-08-25 showed open Issue #33: *"Remove legacy Apple lookup GET fallback after build 24 adoption"*, tracking the eventual retirement of `GET /api/contractors/lookup-by-apple-id`.
- **Authorization & Route Preservation:** Legacy endpoint removal is **explicitly not authorized** in this task and must remain intact until the monitored client adoption window concludes and a separate owner authorization is issued.
