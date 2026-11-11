# Backend Module Classification & Maintenance Contract

This document is the single authoritative manifest classifying every Python source module under `app/` (`app/**/*.py`) and defining the static import-boundary, dependency-uniqueness, runtime-target, and module-retirement contracts enforced by `scripts/run_local_checks.py` and `tests/unit/test_local_checks.py`.

## 1. Module Categories

Every regular `.py` file under `app/` (including package `__init__.py` markers; symlinks anywhere under `app/` are forbidden and fail closed) belongs to exactly one category:

- **`a` -- Live Production Runtime:** Core FastAPI ingress (`app/main.py`), private Cloud Run worker entrypoints (`app/worker_main.py`, `app/worker_runner.py`), active REST routers (`app/api/*`), active inbound webhooks (`app/webhooks/*`), Firestore/RTDB persistence adapters (`app/db/*`), authentication/signature middleware (`app/middleware/*`), shared runtime utilities (`app/utils/*`), and active domain services (`app/services/*`), including pure shared domain modules (`app/services/dialogue_planner.py`, `app/services/receptionist_state.py`) owned by live intake and consumed one-way by offline replay suites.
- **`b` -- Backward-Compatibility / Migration Shims:** Transitional compatibility shims still imported by live runtime (`app/services/legacy_call_commands.py` for non-acknowledging consumer delivery compatibility) and unmounted legacy V1 Telegram, Vapi, conference-bridge, transcription, and warm-transfer modules retained during staged migration audits.
- **`c` -- Isolated Offline Qualification / Voice Bakeoff Modules:** Offline qualification harnesses, synthetic replay engines, visual-diagnosis contract/state prototypes, candidate voice adapters (`app/services/voice_candidates/*`), isolated experiment boundaries (`app/experiments/*`), `app/services/voice_bakeoff_*.py` modules, and the 13 non-prefixed offline modules (`caller_observation_extractor.py`, `caller_turns.py`, `gemini_turn_events.py`, `receptionist_replay.py`, `urgency_replay.py`, `visual_diagnosis_contracts.py`, `visual_diagnosis_state.py`, `voice_call_lifecycle.py`, `voice_lifecycle.py`, `voice_session_auth.py`, `voice_speech_control.py`, `voice_telemetry.py`, `voice_turn_replay.py`). Category `c` modules are strictly unreachable from live production, worker, shim, and dormant runtime entrypoints.
- **`d` -- Dormant / Feature-Gated Integrations:** Operator-gated or default-off capabilities imported by live/worker entrypoints (`canada_sms_consent.py`, `canada_sms_enrollment.py`, `number_release.py`, `sms_sender_enrollment.py`), standalone isolated public-demo entrypoints and their dedicated modules (`public_demo_main.py`, `public_demo_breaker_main.py`, `app/services/public_demo*.py`, `app/webhooks/public_demo.py`), and unmounted operator/audit modules (`adaptive_trust.py`, `appstore_replay.py`, `side_effect_inventory.py`).

## 2. Reachability Tags & Two-Way AST Import Firewall

Each row pairs its category with an explicit static AST reachability classification computed from `app/` imports (resolving `import`, `from ... import ...`, relative imports, and ancestor package `__init__.py` chains):

- **`live_or_worker`:** Exactly the transitive `app.*` import closure of `("app/main.py", "app/worker_main.py", "app/worker_runner.py", "app/__init__.py")`.
  - Every category `a` module MUST appear in this closure (`reachability == "live_or_worker"`).
  - Every module in this closure MUST have `reachability == "live_or_worker"` and `category in {"a", "b", "d"}` (never `c`).
- **`isolated_demo_entrypoint`:** Exactly `closure(["app/public_demo_main.py", "app/public_demo_breaker_main.py"]) - live_closure`.
  - Every module in this demo-only closure MUST have `reachability == "isolated_demo_entrypoint"` and `category == "d"`.
- **`unmounted`:** Every `app/**/*.py` module outside both `live_closure` and `demo_only_closure`.
  - Permitted categories are `{"b", "c", "d"}` (never `a`). Every category `c` module MUST have `reachability == "unmounted"`.

### Static Import Firewall Invariants

1. **No dynamic imports in `app/`:** Calls to `__import__` or `import_module` inside `app/**/*.py` are forbidden and fail closed.
2. **No symlinks under `app/`:** File and directory symlinks anywhere below `app/` are rejected fail-closed before AST analysis.
3. **One-way boundary into `c`:** No module in `a`, `b`, or `d` may import any module in `c` (directly, relatively, or via package `__init__.py` chains). Category `c` modules may import pure shared contracts in `a` (such as `dialogue_planner.py` and `receptionist_state.py`), never the reverse.
4. **Exact 1:1 manifest bijection:** Every `.py` file on disk under `app/` must appear once in the classification table below, and every row in the table must exist on disk.

## 3. Python Target & Dependency Uniqueness Contract

- **Package metadata vs execution target:** `pyproject.toml` retains `requires-python = ">=3.11"` as package-level compatibility metadata alongside existing pinned dependency versions, while local verification (`scripts/run_local_checks.py`), GitHub Actions CI (`python-version: '3.12'`), Cloud Run container images, and Ruff (`target-version = "py312"`) standardize on **Python 3.12** as the required execution interpreter. `scripts/run_local_checks.py` resolves `<repo_root>/.venv/bin/python` when invoked under a non-3.12 interpreter and fails closed if pytest would execute under any version other than Python 3.12.
- **Dependency uniqueness (PEP 503 / PEP 508):** `pyproject.toml` is validated with stdlib `tomllib` and a strict stdlib PEP 508 parser using PEP 503 name canonicalization (`re.sub(r"[-_.]+", "-", name).lower()`). Malformed requirement specifiers, duplicate canonical package names within `[project.dependencies]`, duplicate canonical names within any `[project.optional-dependencies.<group>]`, and redundant or conflicting redeclarations across runtime and optional dependency groups fail closed (for example, `httpx[http2]==0.28.1` is retained as the single canonical `httpx` declaration without a duplicate bare `httpx==0.28.1` entry).

## 4. Module Retirement & Promotion Rules

Before deleting, moving, renaming, or changing the category/reachability of any `b`, `c`, or `d` module:

1. **Verify external and operational references:**
   - For **`b` (migration shims):** Confirm Cloud Run logs, RTDB consumers, webhook routes, and scheduled jobs show zero active callers across a full retention window before removing compatibility code.
   - For **`c` (offline qualification / bakeoff):** Confirm no offline CLI evaluator in `scripts/`, security packet under `docs/security/`, or qualification test under `tests/unit/` binds the module path or digest before archiving or deleting it; never wire a `c` module into `a`, `b`, or `d` without an approved architecture promotion and full production security review.
   - For **`d` (dormant / feature-gated):** Confirm Cloud Run service definitions, environment flags, and operator runbooks either decommission the entrypoint/flag cleanly (before deletion) or complete rollout readiness gates (before promotion to `a`).
2. **Update this manifest atomically:** Add, remove, or reclassify the corresponding table row in the same commit as the filesystem or import change so 1:1 bijection and closure invariants hold at every commit.
3. **Verify locally offline:** Run `python scripts/run_local_checks.py --verify-contracts-only` (and `--mode fast` on affected unit tests) before committing.

## 5. Exhaustive `app/` Module Manifest

| Module Path | Category | Reachability | Notes |
| --- | --- | --- | --- |
| app/__init__.py | a | live_or_worker | Top-level backend package marker imported by live and worker entrypoints. |
| app/api/__init__.py | a | live_or_worker | REST API router package marker. |
| app/api/acquisition.py | a | live_or_worker | Acquisition and activation measurement API router. |
| app/api/admin.py | a | live_or_worker | Admin monitoring API endpoints. |
| app/api/app_version.py | a | live_or_worker | App version-gate endpoint. |
| app/api/calls.py | a | live_or_worker | Call history API. |
| app/api/contacts.py | a | live_or_worker | Contact management API -- CRUD, whitelist, blacklist, bulk sync. |
| app/api/contractors.py | a | live_or_worker | Contractor management API. |
| app/api/estimates.py | a | live_or_worker | AI estimate endpoints -- token creation, upload, analysis, results. |
| app/api/forwarding.py | a | live_or_worker | Per-country call forwarding instructions served to the iOS app during onboarding. |
| app/api/integrations.py | a | live_or_worker | OAuth integration endpoints for Jobber and Google Calendar. |
| app/api/knowledge.py | a | live_or_worker | Knowledge base API -- CRUD for FAQ entries Kevin can answer. |
| app/api/markets.py | a | live_or_worker | Market availability discovery API. |
| app/api/settings.py | a | live_or_worker | User settings API -- per-contractor settings stored in Firestore. |
| app/api/subscription.py | a | live_or_worker | Subscription management API endpoints. |
| app/api/vcard.py | a | live_or_worker | Public vCard download endpoint with HMAC-signed URLs. |
| app/api/voip.py | a | live_or_worker | VoIP API -- device registration, Twilio access tokens, call actions. |
| app/config.py | a | live_or_worker | Application configuration. Loads from .env locally, Secret Manager in production. |
| app/db/__init__.py | a | live_or_worker | Firestore and persistence package marker. |
| app/db/admin_audit.py | a | live_or_worker | Durable admin audit events. |
| app/db/apple_transactions.py | a | live_or_worker | Global Apple App Store transaction registry. |
| app/db/appstore_unmatched.py | a | live_or_worker | App Store notifications that matched no Kevin account. |
| app/db/background_jobs.py | a | live_or_worker | Constant-cardinality private admission ledger. Uncertain work never replays. |
| app/db/cache.py | a | live_or_worker | Firebase RTDB wrapper for active call state. |
| app/db/calls.py | a | live_or_worker | Firestore operations for call records. |
| app/db/contacts.py | a | live_or_worker | Firestore operations for contacts. |
| app/db/contractors.py | a | live_or_worker | Contractor profile management in Firestore. |
| app/db/customer_memory.py | a | live_or_worker | Strictly tenant-scoped Firestore adapter for product customer memory. |
| app/db/firestore_client.py | a | live_or_worker | Firestore client singleton. |
| app/db/integration_lifecycle_audit.py | a | live_or_worker | Durable integration lifecycle audit events and revocation outbox. |
| app/db/jobber_disconnect_receipts.py | a | live_or_worker | Durable Firestore store for Jobber provider disconnect control, barriers, receipts, and work. |
| app/db/jobber_disconnect_transactions.py | a | live_or_worker | Bounded Firestore transactions for Jobber receipts and recovery. |
| app/db/jobber_local_removals.py | a | live_or_worker | Immutable audit for an explicit local Jobber credential removal. |
| app/db/jobs.py | a | live_or_worker | Job record management in Firestore. |
| app/db/knowledge.py | a | live_or_worker | Firestore operations for knowledge base. |
| app/db/owner_sms.py | a | live_or_worker | Firestore state for owner SMS preferences and consent tracking. |
| app/db/post_call_handoffs.py | a | live_or_worker | Firestore outbox state for replay-safe post-call processing. |
| app/db/purge.py | a | live_or_worker | Data purge for deleted accounts. |
| app/db/rate_limits.py | a | live_or_worker | Persistent, cross-instance rate limiting backed by Firestore. |
| app/db/service_requests.py | a | live_or_worker | Tenant-scoped Firestore repository for durable service-request aggregates. |
| app/experiments/__init__.py | c | unmounted | Isolated experiment entrypoints; production routing must not import this package. |
| app/experiments/voice_bakeoff_app.py | c | unmounted | Factory-built, offline-qualified application boundary for the voice bakeoff. |
| app/main.py | a | live_or_worker | Kevin - AI Call Screening Assistant. |
| app/middleware/__init__.py | a | live_or_worker | HTTP and webhook verification middleware package marker. |
| app/middleware/admin_google.py | a | live_or_worker | Google sign-in for the admin page. |
| app/middleware/auth.py | a | live_or_worker | Bearer token authentication for management API endpoints. |
| app/middleware/telegram_verify.py | b | unmounted | Telegram webhook secret token verification. |
| app/middleware/twilio_verify.py | a | live_or_worker | Twilio webhook signature validation middleware. |
| app/middleware/worker_auth.py | a | live_or_worker | Google-signed scheduler OIDC authentication, independent of contractor auth. |
| app/models/__init__.py | b | unmounted | Unmounted legacy V1 models package shim retained pending retirement. |
| app/public_demo_breaker_main.py | d | isolated_demo_entrypoint | IAM-private entry point holding the public demo breaker's parent authority. |
| app/public_demo_main.py | d | isolated_demo_entrypoint | Minimal, isolated FastAPI entry point for Kevin's public phone demo. |
| app/services/__init__.py | a | live_or_worker | Backend domain services package marker. |
| app/services/acquisition.py | a | live_or_worker | Acquisition and activation measurement service (default off). |
| app/services/adaptive_trust.py | d | unmounted | Adaptive trust -- adjust contact trust scores based on call outcomes. |
| app/services/address_validation.py | a | live_or_worker | Post-call address validation via the Google Geocoding API. |
| app/services/admin_diagnostics.py | a | live_or_worker | Admin support diagnostics helpers. |
| app/services/admin_numbers.py | a | live_or_worker | Admin Twilio number inventory helpers. |
| app/services/ai_estimate.py | a | live_or_worker | AI diagnosis and cost estimation via Gemini. |
| app/services/apple_auth.py | a | live_or_worker | Apple Sign-In identity-token verification. |
| app/services/appointment_confirm.py | a | live_or_worker | Owner-tapped appointment confirmation: write Google Calendar, then mark confirmed. |
| app/services/appointment_time.py | a | live_or_worker | Normalize appointment datetimes the voice model hands us. |
| app/services/appstore_replay.py | d | unmounted | Owner-run replay of missed App Store Server Notifications. |
| app/services/background_dispatch.py | a | live_or_worker | Fixed dispatch table and bounded, awaited child-process supervision. |
| app/services/background_maintenance.py | a | live_or_worker | Single-pass housekeeping, shared by legacy loops and the private worker. |
| app/services/calendar.py | a | live_or_worker | Google Calendar client for contractors without Jobber. |
| app/services/call_registration.py | a | live_or_worker | Bounded, create-once incoming screening authority. |
| app/services/call_tags.py | a | live_or_worker | Call tags, derived at read time from what call extraction already stored. |
| app/services/call_tasks.py | a | live_or_worker | Ownership and bounded shutdown of a voice pipeline's asyncio tasks. |
| app/services/caller_observation_extractor.py | c | unmounted | Provider-neutral, final-turn-only observation extraction for the bakeoff. |
| app/services/caller_turns.py | c | unmounted | Provider-neutral retrospective caller-turn assembly primitives. |
| app/services/canada_sms_consent.py | d | live_or_worker | Pure, fail-closed identity and express-consent policy for Canadian owner SMS. |
| app/services/canada_sms_enrollment.py | d | live_or_worker | Bounded Canada-only owner SMS enrollment. No purchases or routing mutations. |
| app/services/circuit_breaker.py | a | live_or_worker | Circuit breaker -- detects error spikes and switches to safe mode. |
| app/services/conference.py | b | unmounted | Twilio Conference Bridge management -- participant add/remove for warm transfer. |
| app/services/conference_registry.py | a | live_or_worker | Conference name registry. |
| app/services/country_policy.py | a | live_or_worker | Country policy, market availability, and service binding resolution. |
| app/services/customer_memory.py | a | live_or_worker | Product-owned, tenant-scoped memory for returning callers. |
| app/services/dialogue_planner.py | a | live_or_worker | Deterministic receptionist planner for next allowed action. |
| app/services/entitlements.py | a | live_or_worker | Subscription entitlement helpers. |
| app/services/estimate_media.py | a | live_or_worker | Estimate media persistence in GCS and HMAC-signed watch URLs. |
| app/services/estimate_notifications.py | a | live_or_worker | Estimate SMS notifications for callers and contractors. |
| app/services/estimate_worker.py | a | live_or_worker | Recovery worker for stranded estimate video analyses. |
| app/services/gated_actions.py | a | live_or_worker | Canonical backend gate registry for Phase 0 side effects. |
| app/services/gemini_pipeline.py | a | live_or_worker | Gemini Live API voice pipeline -- audio-native alternative to Deepgram+Claude+ElevenLabs. |
| app/services/gemini_turn_events.py | c | unmounted | Bounded, payload-safe Gemini Live caller-turn event decoding. |
| app/services/google_calendar_request_provider.py | a | live_or_worker | Google Calendar mutation adapter for product-owned service requests. |
| app/services/instruction_composer.py | a | live_or_worker | Compose compact per-turn model instructions from receptionist state. |
| app/services/integration_token_mutations.py | a | live_or_worker | Durable, transactional CAS mutations and multi-instance lease coordination for integration tokens. |
| app/services/integration_tokens.py | a | live_or_worker | Versioned, context-bound AES-256-GCM encryption envelope for integration tokens. |
| app/services/job_card.py | a | live_or_worker | Post-call job card extraction via Claude. |
| app/services/jobber.py | a | live_or_worker | Jobber GraphQL API client for FSM integration. |
| app/services/jobber_admissions.py | a | live_or_worker | Jobber-specific durable admission, lifecycle claims and authenticated recovery. |
| app/services/jobber_disconnect_barriers.py | a | live_or_worker | Strict sticky Jobber account-barrier admission; no provider calls. |
| app/services/jobber_disconnect_recovery.py | a | live_or_worker | Bounded durable Jobber disconnect fan-out; never calls the provider. |
| app/services/jobber_webhook_auth.py | a | live_or_worker | Jobber webhook signature verification primitive. |
| app/services/jobber_webhook_events.py | a | live_or_worker | Authenticated Jobber disconnect webhook event parser and envelope primitive. |
| app/services/knowledge_base.py | b | unmounted | Knowledge base service -- used by Vapi tool calls to answer FAQs. |
| app/services/legacy_call_commands.py | b | live_or_worker | Conservative delivery compatibility with legacy, non-acknowledging consumers. |
| app/services/live_intake_controller.py | a | live_or_worker | Call-scoped wrapper that turns receptionist policy into live instructions. |
| app/services/lookup.py | a | live_or_worker | Parallel number lookups with 3-second per-lookup timeout. |
| app/services/message_taking.py | a | live_or_worker | Owner Take-a-Message conversational transition and timing policy. |
| app/services/number_release.py | d | live_or_worker | Release sweep for Twilio numbers held by accounts that no longer need them. |
| app/services/oauth_pkce.py | a | live_or_worker | OAuth 2.0 PKCE (RFC 7636) helpers for Jobber integration. |
| app/services/owner_call_actions.py | a | live_or_worker | Durable, exact-call arbitration. RTDB callbacks are pure and never delete rejects. |
| app/services/owner_sms.py | a | live_or_worker | Owner SMS service: single boundary for sending SMS to account owners. |
| app/services/personal_language.py | a | live_or_worker | Language normalization and resolution helpers for personal and voice pipelines. |
| app/services/post_call.py | a | live_or_worker | Post-call processing: extract job card, save to Firestore, send SMS. |
| app/services/post_call_handoff.py | a | live_or_worker | Durable orchestration for at-most-once post-call side effects. |
| app/services/public_demo.py | d | isolated_demo_entrypoint | Fail-closed policy helpers for the public receptionist demo. |
| app/services/public_demo_breaker.py | d | isolated_demo_entrypoint | Parent-authority Twilio breaker used only by the private breaker runtime. |
| app/services/public_demo_breaker_client.py | d | isolated_demo_entrypoint | Narrow authenticated client for the IAM-private public-demo breaker. |
| app/services/public_demo_pipeline.py | d | isolated_demo_entrypoint | Dedicated Gemini Live adapter for the stateless public phone demo. |
| app/services/push_notification.py | a | live_or_worker | APNs VoIP push notification sender. |
| app/services/quiet_hours.py | a | live_or_worker | Quiet hours -- Kevin answers non-whitelisted callers during an account's window. |
| app/services/receptionist_context.py | a | live_or_worker | Shared, bounded caller context for every receptionist voice engine. |
| app/services/receptionist_replay.py | c | unmounted | Replay receptionist transcript fixtures through state, planner, and composer. |
| app/services/receptionist_state.py | a | live_or_worker | Call-scoped receptionist state for live and replayed intake. |
| app/services/receptionist_tools.py | a | live_or_worker | Model-neutral receptionist tools for changing existing service requests. |
| app/services/relay_pipeline.py | a | live_or_worker | ConversationRelay session engine -- the receptionist brain, text-in/text-out. |
| app/services/routing.py | a | live_or_worker | Call routing decisions based on trust score. |
| app/services/scoring.py | a | live_or_worker | Trust score engine with breakdown logging. |
| app/services/screening_summary.py | a | live_or_worker | Screening summary service for live call screening push notifications. |
| app/services/service_request.py | a | live_or_worker | Pure domain model for versioned service-request workflows. |
| app/services/service_request_recovery.py | a | live_or_worker | Bounded recovery worker for durable service-request provider proposals. |
| app/services/service_request_repository.py | a | live_or_worker | Repository contract and application service for service-request commands. |
| app/services/side_effect_audit.py | a | live_or_worker | Payload-safe audit events for gated side effects. |
| app/services/side_effect_inventory.py | d | unmounted | Phase 0 side-effect inventory for v2 safety gating. |
| app/services/sms.py | a | live_or_worker | SMS service -- Text Reply and Text Them flows. |
| app/services/sms_sender_enrollment.py | d | live_or_worker | Guarded SMS sender enrollment and recovery service. |
| app/services/state_machine.py | a | live_or_worker | Call lifecycle state machine with RTDB-backed atomic transitions. |
| app/services/subscription.py | a | live_or_worker | Apple App Store Server API integration. |
| app/services/subscription_recheck.py | a | live_or_worker | Re-check stale paid subscriptions against the App Store. |
| app/services/telegram_bot.py | b | unmounted | Telegram bot service -- notifications with inline action buttons and live transcript. |
| app/services/transcriber.py | b | unmounted | Real-time speech-to-text using Deepgram. |
| app/services/urgency.py | a | live_or_worker | Deterministic urgency classification shared by live voice pipelines. |
| app/services/urgency_replay.py | c | unmounted | Aggregate evaluation for redacted multilingual urgency fixtures. |
| app/services/urgent_handoff.py | a | live_or_worker | Once-only, bounded urgent escalation without allocating a conference. |
| app/services/vapi_agent.py | b | unmounted | Vapi AI agent service -- creates Kevin and dials him into conferences. |
| app/services/vcard.py | a | live_or_worker | vCard generation for contractor contact sharing. |
| app/services/visual_diagnosis_contracts.py | c | unmounted | Provider-neutral, synthetic-only contracts for visual diagnosis A0-A2. |
| app/services/visual_diagnosis_state.py | c | unmounted | Pure structural state machine for visual diagnosis A0-A2. |
| app/services/voice_bakeoff_closure.py | c | unmounted | Dedicated, offline-only authority for one-use local closure. |
| app/services/voice_bakeoff_control_admission_projection.py | c | unmounted | Non-authorizing metadata projection for the sealed control-store boundary. |
| app/services/voice_bakeoff_control_store_assembly.py | c | unmounted | Inert assembly seam for the isolated bakeoff execution-control store. |
| app/services/voice_bakeoff_coordinator.py | c | unmounted | Bakeoff-only coordinator joining shared speech and lifecycle contracts. |
| app/services/voice_bakeoff_credential_broker.py | c | unmounted | Nonproduction-only credential broker for the bakeoff runner. |
| app/services/voice_bakeoff_execution_firewall_contracts.py | c | unmounted | Offline contracts for the future bakeoff execution firewall. |
| app/services/voice_bakeoff_firestore_transaction_port.py | c | unmounted | Unwired Firestore-shaped transaction port for isolated bakeoff control data. |
| app/services/voice_bakeoff_gate_contracts.py | c | unmounted | Neutral, payload-safe Task 4.8 gate report value contracts. |
| app/services/voice_bakeoff_gate_report.py | c | unmounted | Read-only Task 4.8 gate status for the sole project owner. |
| app/services/voice_bakeoff_google_firestore_runner.py | c | unmounted | Unwired Google Firestore transaction runner for isolated bakeoff control data. |
| app/services/voice_bakeoff_language_choice.py | c | unmounted | Offline-only, one-shot unsupported-language recovery. |
| app/services/voice_bakeoff_materializer.py | c | unmounted | Closed, non-authorizing content proposals for the offline voice bakeoff. |
| app/services/voice_bakeoff_nonce_ledger.py | c | unmounted | Persisted, file-locked, one-use nonce/approval-id consumption ledger. |
| app/services/voice_bakeoff_preauth_reference.py | c | unmounted | Validate the non-authorizing Task 4.8 pre-auth control-store reference. |
| app/services/voice_bakeoff_residue_audit.py | c | unmounted | Residue audit: proves whether artifacts remain past their TTL. |
| app/services/voice_bakeoff_security_contracts.py | c | unmounted | Offline-only security contracts for a future voice bakeoff execution gate. |
| app/services/voice_bakeoff_session_driver.py | c | unmounted | Sealed, synchronous, synthetic-only driver for offline voice composition. |
| app/services/voice_bakeoff_silence.py | c | unmounted | Sealed offline materialization for inert call-lifecycle speech intents. |
| app/services/voice_bakeoff_transactional_control_seam.py | c | unmounted | Unwired transaction contract for future isolated bakeoff control storage. |
| app/services/voice_bakeoff_turn_composition.py | c | unmounted | Transactional, offline-only composition for candidate-final caller turns. |
| app/services/voice_call_lifecycle.py | c | unmounted | Pure, revision-bound call-lifecycle reducer for the offline voice bakeoff. |
| app/services/voice_candidates/__init__.py | c | unmounted | Provider-SDK-free contracts for offline voice candidate adapters. |
| app/services/voice_candidates/chained_streaming.py | c | unmounted | Offline Arm B1 streamed-chain adapter contract. |
| app/services/voice_candidates/conversation_relay.py | c | unmounted | Offline Arm B2 ConversationRelay adapter contract. |
| app/services/voice_candidates/manual_native.py | c | unmounted | Offline Arm C manual-turn native feasibility contract. |
| app/services/voice_candidates/native_gemini.py | c | unmounted | Offline Arm A interface stub for native Gemini lifecycle mapping. |
| app/services/voice_events.py | a | live_or_worker | Privacy-preserving event formatting shared by voice engines and tools. |
| app/services/voice_lifecycle.py | c | unmounted | Closed, offline-only lifecycle primitives for the voice bakeoff. |
| app/services/voice_pipeline.py | a | live_or_worker | Voice pipeline: Deepgram STT -> Claude -> ElevenLabs TTS. |
| app/services/voice_session_auth.py | c | unmounted | Offline-only authentication contract for future isolated voice bakeoff ingress. |
| app/services/voice_speech_control.py | c | unmounted | Offline, provider-neutral speech authorization for the voice bakeoff. |
| app/services/voice_telemetry.py | c | unmounted | Immutable allowlist projection for offline voice bakeoff telemetry. |
| app/services/voice_tool_declarations.py | a | live_or_worker | Provider-specific tool declarations used during tenant voice calls. |
| app/services/voice_tool_dispatch.py | a | live_or_worker | Shared execution of tenant-bound voice tools, independent of model transport. |
| app/services/voice_turn_replay.py | c | unmounted | Offline, payload-safe replay primitives for Gemini turn detection experiments. |
| app/services/warm_transfer.py | b | unmounted | Warm transfer (Pick Up) flow for v2 (Media Streams architecture). |
| app/utils/__init__.py | a | live_or_worker | Shared runtime utilities package marker. |
| app/utils/audio.py | a | live_or_worker | Audio format conversion utilities for Twilio <-> Gemini. |
| app/utils/error_handlers.py | a | live_or_worker | Global error handlers -- the last line of defense. Never drop a call. |
| app/utils/logging.py | a | live_or_worker | Structured JSON logging with call_sid correlation. |
| app/utils/phone.py | a | live_or_worker | Phone number normalization and validation. |
| app/utils/pydantic_settings.py | a | live_or_worker | Pydantic settings helpers for environment and dotenv handling. |
| app/webhooks/__init__.py | a | live_or_worker | Inbound telephony and provider webhooks package marker. |
| app/webhooks/appstore.py | a | live_or_worker | App Store Server Notifications V2 webhook. |
| app/webhooks/jobber.py | a | live_or_worker | Authenticated HTTP webhook receiver for Jobber provider events. |
| app/webhooks/media_stream.py | a | live_or_worker | Twilio Media Streams WebSocket -- bridges audio through Deepgram STT -> Claude -> ElevenLabs TTS. |
| app/webhooks/public_demo.py | d | isolated_demo_entrypoint | Fail-closed Twilio ingress for the public, fictional Kevin phone demo. |
| app/webhooks/relay_stream.py | a | live_or_worker | WebSocket endpoint for Twilio ConversationRelay sessions. |
| app/webhooks/stream_transport.py | a | live_or_worker | Shared Twilio WebSocket transport, playback mark tracking, and live transcript writer. |
| app/webhooks/telegram_callback.py | b | unmounted | Telegram callback handler -- processes button taps from call notifications. |
| app/webhooks/twilio_incoming.py | a | live_or_worker | Twilio incoming call webhook handler. |
| app/webhooks/vapi_events.py | b | unmounted | Vapi webhook event handler. |
| app/worker_main.py | a | live_or_worker | Private staging HTTP worker. Its only routes are health and fixed tasks. |
| app/worker_runner.py | a | live_or_worker | Single-job subprocess protocol. No public-API startup and no infinite polls. |
