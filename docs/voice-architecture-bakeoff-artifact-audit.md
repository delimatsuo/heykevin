# Voice Architecture Bakeoff Artifact Audit

> **Status:** Read-only Task 0.0 audit on 2026-07-22. This document does not
> authorize a cherry-pick, merge, provider connection, staging deployment, or
> production change.

## 1. Audit scope and provenance

The audit was performed from
`codex/voice-architecture-bakeoff-plan` at commit `8403ee2`, whose plan pins the
following current baseline:

- `origin/main`: `6dc3013df78070cd60871febb1a541977ea4c3b3`
- `codex/voice-completion-instrumentation`:
  `71526efdbe069aae46c861473e4dfd1414e94528`
- `codex/native-live-dialogue-safety`:
  `316defba0420d92ec7c9207a9331be2514f24c7b`
- `codex/voice-response-length-192-recovery`:
  `2614dab2bfdf468ac78734f2877c52d3901104d7`

`git fetch origin --prune` completed before recording those refs. The first two
candidate branches merge directly from the pinned `origin/main`. The 192-token
recovery branch instead merges from older commit
`33e73cfea2b953057c0f320ef33d9afd6239c4bd`; it is not a current-main delta.

The audit used only source, diff, test, and payload-safe instrumentation evidence.
It did not connect to a provider, inspect caller transcripts/audio, use a phone
number, deploy, or alter a historical worktree.

## 2. Candidate inventory

| Artifact | Exact delta from current baseline | Read-only finding | Disposition |
| --- | --- | --- | --- |
| `codex/voice-response-length-192-recovery` | 2 lines in `gemini_pipeline.py` and one focused expectation | Raises only `MAX_RESPONSE_OUTPUT_TOKENS` from 128 to 192. It supplies no lifecycle, caller-heard, semantic, authentication, or safety evidence. It is based on an older main. | `reject` as a bakeoff implementation; retain only as historical cap evidence. |
| `codex/native-live-dialogue-safety` | 8 files, 377 additions / 17 deletions | Adds staging-only tool/terminal suppression, first- and response-end mark instrumentation, payload-safe focused tests, health flags, and a 192-token native configuration. Its marks are transport receipts, not caller-heard or semantic proof. | Selective `reuse_after_isolation`; never merge as an architecture arm. |
| `codex/voice-completion-instrumentation` | 27 files, 9,819 additions / 842 deletions across 11 commits | Adds controlled-turn validation, a coordinator, mark/clear handling, timing logs, an aggregate release evaluator, and staging gating. It also alters live routing, legacy pipelines, deployment workflow, and current production-shaped modules. | Selective `reuse_after_isolation`; `reject` a branch-wide merge or live route reuse. |
| Current `origin/main` caller-turn/controller/replay/Gemini/voice/media components | Existing baseline | Contains useful typed and payload-safe primitives, but no provider-neutral pre-speech contract, sealed evidence path, or authenticated bakeoff ingress. | Component-level classifications below. |

All three branch diffs pass `git diff --check`. Diff cleanliness is not treated as
architecture, security, caller-experience, or release evidence.

## 3. Cross-cutting boundary findings

### 3.1 Retrospective caller turns are not a speech permit

`app/services/caller_turns.py` and `app/services/gemini_turn_events.py` provide
bounded events, deduplication, epoch handling, and payload-safe reports. Their
completion model is explicitly retrospective: it can close after provider model
output, `generationComplete`, `turnComplete`, interruption, reconnect, or a
quiescence deadline. They may be adapted for Stage-1 telemetry and late-fragment
measurement, but cannot be relabeled as a caller-final event that authorizes the
current native response.

### 3.2 Current media ingress is not the Stage-2 authentication boundary

Current `media_stream.py` accepts the WebSocket, receives the `start` message, and
starts bounded ingress reads before it validates the stored token. The planned
authentication boundary instead requires canonical request-signature validation,
a bounded `AUTH_PENDING` state, an active execution record, atomic binding, and no
media/provider/application work before authentication. The current route therefore
requires a rewrite for the bakeoff runtime; its payload-safe mark/clear mechanics
remain separately reviewable transport material.

### 3.3 Current replay is deliberately not live evidence

`receptionist_replay.py` reports that assistant semantics, latency, live behavior,
and release authorization are false. It can support development-fixture structure
and deterministic controller tests only. It cannot serve as a sealed corpus,
caller-heard evaluator, capability probe, staging qualification, or release gate.

### 3.4 A Twilio mark is not a human-delivery receipt

The native-safety and controlled branches improve correlation of first-media and
response-end marks, clear handling, stale-mark rejection, and payload-safe logs.
That work is useful transport instrumentation. A returned mark still proves only
that Twilio resolved buffered media; it cannot prove semantic completeness, human
hearing, or understanding. The bakeoff must keep caller-side PCMU evidence and
semantic-act adjudication independent.

## 4. Stage 1 classification matrix

| Plan requirement | Existing artifacts and evidence | Classification | Required isolation or replacement |
| --- | --- | --- | --- |
| 1.1 Provider-neutral lifecycle types | `caller_turns.py`, `gemini_turn_events.py`, and controlled `voice_turn_coordinator.py` have enums, epochs, terminal states, and focused unit tests. The coordinator is tightly coupled to controlled Gemini/ElevenLabs speech and a legacy `VoicePipeline` subclass. | `reuse_after_isolation` | Create the planned versioned `VoiceEvent`/`VoiceCommand` and independent lifecycles. Adapt bounded event/epoch concepts only; do not import retrospective completion or coordinator policy into live routing. |
| 1.2 Payload-safe telemetry projection | Current `_log_voice_timing`, controlled timing events, redacted caller-turn reports, and `evaluate_voice_release.py` parse aggregate timing fields. Controlled tests cover opaque mark names and short call labels. | `reuse_after_isolation` | Retain allowlist/redaction patterns and aggregate parsing only after the new projection rejects all raw/unknown fields and correlates the new IDs/terminals. Do not reuse historical log names as semantic proof. |
| 1.3 Native-control instrumentation without behavior change | Native-safety adds generation/response-end mark callbacks, staged tool/terminal suppression, health flags, and focused ingress tests. | `reuse_after_isolation` | Extract instrumentation as a no-behavior-change adapter on the native arm. Exclude the 192-token change, direct staging controls, and any lexical-goodbye behavior from the shared contract. |
| 1.4 Aggregate evaluator | `scripts/evaluate_voice_release.py` aggregates payload-safe timing messages and has focused evaluator tests. It does not require the sealed manifest, approval envelope, lifecycle graph, cohort denominator, or no-winner rules. | `reuse_after_isolation` | Reuse only generic parser/percentile ideas after a new stdin-only evaluator validates the exact candidate/configuration/manifest IDs and rejects incomplete or private input. |

## 5. Stage 2 classification matrix

| Plan requirement | Existing artifacts and evidence | Classification | Required isolation or replacement |
| --- | --- | --- | --- |
| 2.1 Every-ingress authentication and provenance | Current route has a custom-parameter token and RTDB lookup. Controlled and native branches add playback handling, not canonical Twilio signature validation, active-execution records, per-invocation approval binding, or all-arm isolated ingress. | `rewrite` | Implement `voice_session_auth.py` and the planned isolated application. Preserve no token values or raw IDs in logs. Current accept-then-read flow is explicitly ineligible. |
| 2.2 Qualification environment contract | Controlled branch has staging allowlist/configuration checks; native branch has staging-only safety flags and health fields. Neither describes dedicated identities, isolated stores, KMS, provider data settings, PSTN references, or deletion/residue controls for every dependency. | `not_implemented` | Write the qualification contract from the approved plan. Historical staging flags may be cited as constraints, not reused as environment proof. |
| 2.3 Negative security verification | Branch tests cover selected tool gates, mark/clear races, bounded ingress, and log redaction. They do not cover signed HTTP/TwiML issuance, signature URL canonicalization, active-execution revocation, every callback, credential substitution, or cross-arm envelope binding. | `rewrite` | Add the full negative matrix specified by Stage 2. Existing redaction/mark-race tests may be ported only after the new ingress contract exists. |

## 6. Stage 3 classification matrix

| Plan requirement | Existing artifacts and evidence | Classification | Required isolation or replacement |
| --- | --- | --- |
| 3.1 Privacy-approved corpus manifest | Replay fixtures and controlled unit inputs are development fixtures. They are not consented PCMU corpus data, finite language contract, holdout store, or rights/retention manifest. | `not_implemented` | Create a synthetic-first manifest and external holdout custody. Do not repurpose staging or historical caller data. |
| 3.2 Development and sealed evaluation tiers | Controlled tests and replay are development-only. No sealed windows, per-language denominators, power/precision rules, or external holdout freeze are present. | `not_implemented` | Implement the tier separation exactly as planned. |
| 3.3 Caller-heard audio evaluation | Native/controlled branches have Twilio mark timing and ordinary unit assertions, but no caller-side PCMU harness, blinded rater protocol, fluent-language rater rule, or encrypted evidence custody. | `not_implemented` | Build the caller-side evidence path and adjudication protocol before treating any candidate as eligible. |
| 3.4 Signed provider-execution approval | Historical feature flags and staging health fields are not a sealed sole-owner authorization, one-use approval envelope, immutable store, per-dependency attestation, or active execution record. | `not_implemented` | Implement the envelope/runner contract. Do not turn existing environment flags into authorization. |
| 3.5 Caller-UX acceptance contract | Controlled coordinator tests cover questions, silence, failures, and recovery in mocks. They do not define the sealed failure taxonomy, caller-heard interruption thresholds, accessibility contract, consenting-participant protocol, or no-winner rule. In particular, those mocks do not prove caller-heard direct-answer ordering/relevance, safety-content completeness, language match/code-switching, or safe unsupported-language/accessibility fallback. | `not_implemented` | Write the exact caller-UX acceptance contract before capability probing. |

## 7. Stage 4 classification matrix

| Plan requirement | Existing artifacts and evidence | Classification | Required isolation or replacement |
| --- | --- | --- |
| 4.0 Shared speech control | `gemini_controlled_turn.py` defines typed spoken turns, deterministic fallback/safety text, answer/question checks, and validation tests. It includes hardcoded domain semantics and direct provider JSON calls. | `reuse_after_isolation` | Extract only typed act validation, bounded validation patterns, and deterministic test cases into the provider-neutral speech-control module. Re-authorize all wording/policy from the new `SpokenPlan`; do not inherit model prompts, fixed word caps, or controlled-pipeline ownership. |
| 4.1 Typed observation extractor | Existing `CallerObservation`, `IntakeState`, controlled observation parsing, and focused tests cover bounded fields/corrections. The controlled implementation is Gemini-specific and embedded in the pipeline. | `reuse_after_isolation` | Build one provider-neutral extractor with an external sealed holdout. Reuse schemas/tests only after removing provider-specific prompt/configuration and any inline state mutation. |
| 4.2 Shared coordinator and call lifecycle | `voice_turn_coordinator.py` plus `test_voice_turn_coordinator.py` demonstrate receipt-gated questions, silence reprompt/close, failure recovery, and stale-turn handling. It is a controlled-pipeline state machine, not the planned four-lifecycle reducer, and it treats Twilio marks as its delivery input. | `reuse_after_isolation` | Rebuild with fake-clock reducers and typed events. Reuse only test scenarios and state-transition ideas; keep semantic confirmation and caller-side evidence separate from a mark. |
| 4.3 Arm A native control | `GeminiPipeline`, native-safety callbacks, and focused tests provide a native audio path, timing events, and staging tool/terminal suppression. The live path can generate audio before a planned application permit and is tied to a hard token cap. | `reuse_after_isolation` | Use instrumentation and transport adapters only. Build the Arm-A adapter behind the new speech-authorization/lifecycle contract; do not treat the current native path as selectable. |
| 4.4 Arm B1 streamed chained reference | `VoicePipeline` supplies Deepgram, ElevenLabs, Twilio media, mark/clear, interruption, and payload-safe log components. It also owns prompts, tools, lexical-goodbye completion, timer behavior, and mutable legacy state. | `reuse_after_isolation` | Reuse individual transport/provider clients after interface review. Reject the legacy pipeline as the B1 coordinator or policy owner. |
| 4.5 Arm B2 ConversationRelay challenger | No ConversationRelay adapter, isolated ingress, playback receipt mapping, or tests exist in the baseline or audited branches. | `not_implemented` | Build only the adapter contract and isolated ingress. Keep B2 control-only until the capability probe proves the required real-time receipt. |
| 4.6 Arm C manual-turn feasibility probe | No external/manual activity detector feeds a native Gemini adapter through the planned application permit. | `not_implemented` | Build the probe separately; do not infer feasibility from retrospective Gemini turn events. |
| 4.7 Isolated bakeoff runtime and caller harness | Existing `media_stream.py` is a production-shaped router. Controlled branch changes its live routing; no separate bakeoff app, all-arm router, controlled caller harness, or sealed encrypted evidence path exists. | `not_implemented` | Create the isolated runtime and harness as planned. Do not mount a historical route in staging or production. |
| 4.8 Bounded provider capability probe | No candidate has an approved envelope, attested every-dependency identity, isolated caller-side evidence path, or frozen configuration. | `not_implemented` | Run only after Stages 0-4.7 gates. Historical staging observations remain diagnostic evidence, not capability proof. |

## 8. Selective reuse ledger

The following are candidates for a later exact-diff review, not permission to copy
them now:

| Candidate material | Source commit/file(s) | Provisional classification | Unsafe ownership to remove |
| --- | --- | --- | --- |
| Bounded caller event/epoch/redaction primitives | `6dc3013` `app/services/caller_turns.py`, `app/services/gemini_turn_events.py` | `reuse_after_isolation` | Retrospective completion authority and fragment content retention. |
| Typed turn validation and deterministic recovery fixtures | `71526ef` `app/services/gemini_controlled_turn.py`, focused tests | `reuse_after_isolation` | Gemini-specific JSON requests, hardcoded domain policy, direct prompt ownership. |
| Receipt-gated state-transition scenarios | `71526ef` `app/services/voice_turn_coordinator.py`, focused tests | `reuse_after_isolation` | Controlled-pipeline lifecycle ownership and mark-as-delivery assumptions. |
| Opaque mark/clear mechanics and redaction tests | `71526ef` / `316defb` `app/webhooks/media_stream.py`, ingress/privacy tests | `reuse_after_isolation` | Existing accept-before-auth route, current live router, `on_transcript` full-transcript RTDB persistence and related live callback data flows, and any caller-experience implication from a mark. |
| Native timing/response-end instrumentation | `316defb` `app/services/gemini_pipeline.py`, `media_stream.py`, focused tests | `reuse_after_isolation` | 192-token tuning, staging-only behavior controls, lexical terminal logic, and direct live wiring. |
| Aggregate timing parser | `71526ef` `scripts/evaluate_voice_release.py`, focused evaluator tests | `reuse_after_isolation` | Historical event schema and release inference. |

The following are rejected for reuse as an implementation unit:

- `2614dab` / `codex/voice-response-length-192-recovery`: cap-only experiment;
- `316defb` / `codex/native-live-dialogue-safety`: direct live-path patch as a
  candidate architecture;
- `71526ef` / `codex/voice-completion-instrumentation`: branch-wide merge,
  controlled pipeline, direct route selection, deployment workflow changes, and
  legacy `VoicePipeline` ownership;
- current `receptionist_replay.py` as semantic, latency, live, or release proof.

## 9. Required next gate

No `reuse_*` classification in this audit changes any create-versus-modify decision
yet. It authorizes no copy, cherry-pick, import, fixture port, configuration change,
or deployment change. An item can be considered later only after the Stage 0
sole-owner authorization is bound to an advisory technical review with no unresolved
P1, its exact source files, removed ownership/import boundary, new target
files/tests, and deterministic plus negative verification. The next work
is Task 0.1 through Task 0.4 documentation only: the provider
capability matrix, bakeoff ADR, security/privacy control annex, caller-UX
acceptance contract, and reconciled release gates. Those exact files, together with
this audit, require sole-owner authorization after advisory technical review with no
unresolved P1 before Stage 1 code begins.
