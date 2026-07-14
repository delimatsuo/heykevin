# Gemini Live Provider Qualification Guardrails

Date: 2026-07-13

## Current Decision

No candidate is authorized by this qualification work for a provider-selection
change, staging release, production release, or new customer-audio exposure. No
provider-selection matrix can run or evaluate today: no reviewed
persistent-session corpus is committed. The executable commands produce
diagnostic evidence only, never a provider decision.

This work does not change `app/services/gemini_pipeline.py`,
`app/services/voice_pipeline.py`, the active model, staging, or production.

## Enterprise Eligibility Policy

A production-default candidate must meet every condition below before any
provider-selection matrix is authorized:

- It is generally available. A preview candidate needs a written, owner-approved
  exception with a rollback and migration plan.
- It has at least 365 days of published support runway at the time of selection,
  or an owner-approved migration exception.
- It supports the documented release-language set, not merely the languages in
  the offline fixture.
- Its privacy posture, including zero-data-retention requirements and any abuse
  monitoring exception, is affirmatively verified for customer data.
- It is evaluated with the production automatic-VAD architecture and meets a
  speech-end-to-first-audio p95 of at most 1,500 ms and maximum of at most
  2,500 ms.
- It passes the independent telephone-path, tool, barge-in, playout, reconnect,
  multilingual, and revision-filtered release gates.

The default policy is intentionally conservative. A documented exception is
possible, but it must name an owner, an expiry, a rollback, and the migration
condition that removes the exception.

## Candidate Snapshot

As of this document date, neither current candidate satisfies the default
production policy:

- Gemini Developer API `gemini-3.1-flash-live-preview` is a Preview model.
- Vertex `gemini-live-2.5-flash-native-audio` is generally available, but its
  published retirement date of 2026-12-13 is inside the 365-day runway policy.

The current candidates may remain research inputs after a separately approved
exception, but neither is a default production selection. Before investing in a
matrix, confirm a supported Vertex successor or approve an explicit migration
plan.

## Evidence Layers

### 1. Cold Connection Smoke

`scripts/smoke_gemini_live_providers.py` schedules two 12-attempt benchmarks:
two providers, six public cases, and two VAD arms. It has a hard 24-attempt
ceiling, no retry escalation, and sets `matrix_authorized: false` in every
result. A smoke is ready only when each underlying benchmark reports a pass and
the smoke's bounded aggregate checks also pass.

Each attempt creates a new WebSocket, so its report is explicitly labeled
`cold_single_turn`. It can diagnose setup, first-audio, terminal, provider-error,
and bounded WebSocket-close behavior. It cannot establish call stability,
context continuity, reconnect handling, tool behavior, or receptionist quality.

The lower-level benchmark is also permanently labeled `offline_diagnostic_only`
and `cold_single_turn`. It cannot emit a qualification scope. Its automatic-VAD
p95 and maximum limits are pinned at 1,500 ms and 2,500 ms in code; no command
line or programmatic argument can relax them.

### 2. Persistent Multi-Turn Qualification

No executor exists yet. A future runner must use one persistent WebSocket for a
multi-turn cohort, record reconnect and context-continuity outcomes, and label
the report `persistent_multi_turn`. It must use a broadened, independently
reviewed public corpus with multiple speakers, release languages, pauses, noise,
short replies, and fragmented media conditions.

Its count-only session diagnostics must include the number of sessions, minimum
turns per session, reconnect attempts and failures, and context-continuity
failures. Any reconnect or continuity failure makes that provider ineligible.

The current FLEURS fixture has two independent sources and several deterministic
transforms. It is suitable for smoke diagnostics, not a matrix-grade production
language or speaker claim.

### 3. Offline Matrix Evaluation

The pure evaluator is deliberately disabled today. It has no caller-supplied
corpus-hash parameters and fails closed until a reviewed persistent matrix corpus
and its exact manifest/corpus SHA-256 values are committed in the source. This
prevents an arbitrary report caller from declaring a corpus approved.

Once that corpus and a persistent-session runner are separately shipped, the
evaluator will accept only `persistent_multi_turn` reports with both providers,
both fixed seeds, the committed corpus identity, full paired coverage, at least
30 attempts per arm, zero raw errors, complete arm diagnostics, and every
benchmark gate passed. It independently checks those fields instead of trusting
the report status. Cold smoke reports cannot enter the evaluator.

Its outcomes are deliberately limited:

- `no_provider_eligible` when neither provider meets every evidence gate;
- `one_provider_eligible` when only one does; or
- `both_providers_eligible` when both do.

These are evidence outcomes only. The evaluator has no candidate output and
always sets `selection_authorized: false` and `release_authorized: false`.
When both are evidence-eligible, it reports whether their worst-seed automatic
latency is within a precommitted practical-equivalence margin of 100 ms p95 and
250 ms maximum. A human review still owns the production choice.

## Privacy And Operational Controls

Only the repository-pinned public FLEURS fixture may enter the smoke. It stores
no transcript text, customer audio, phone number, request ID, tenant data, or
credentials. Reports retain only aggregate latency, bounded error counts, model
and provider labels, seed, session scope, and corpus hashes.

Provider payload text, close reasons, private WebSocket close codes, bearer
tokens, API keys, and project identifiers are excluded from reports. Session
resumption, context caching, tools, grounding, search, and provider payload
logging remain disabled for this evidence path.

A future approved run must use a durable, least-privilege, keyless identity and
record before/after residue audits. Creating and cleaning up temporary project
identities is not an acceptable normal smoke workflow.

## Historical Evidence

The two public-fixture smokes on 2026-07-13 are nonqualifying diagnostics:

- At commit `95524f4`, Developer completed 12 turns with zero errors, but
  automatic p95/max was 2,027 ms. Vertex stopped after a manual timeout.
- At commit `17c8937`, Developer stopped after a manual close. Vertex completed
  12 turns with zero errors, but automatic p95/max was 1,582 ms and manual p95
  was 1,533 ms.

Neither run had the current bounded close classifications. Neither is evidence
for provider selection. The previous 264-attempt executor was superseded and
must not be revived without this policy, a matrix-grade corpus, a persistent
multi-turn runner, exact-SHA CI, and explicit authorization.

## Primary Sources

- Gemini 3.1 Flash Live Preview:
  <https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview>
- Gemini Developer Live capabilities:
  <https://ai.google.dev/gemini-api/docs/live-api/capabilities>
- Vertex Live release notes:
  <https://docs.cloud.google.com/vertex-ai/generative-ai/docs/release-notes>
- Vertex model lifecycle:
  <https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions>
- Vertex zero data retention:
  <https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/zero-data-retention>
