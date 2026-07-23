# Voice Architecture Bakeoff and Lifecycle Control Plan

> **Status:** Documentation-only architecture plan amended after formal panel
> review on 2026-07-22. This exact file does not authorize implementation until
> staff-architecture, security/privacy, and conversation-product reviewers approve
> it with no unresolved P1.
> Baseline: `origin/main` at
> `6dc3013df78070cd60871febb1a541977ea4c3b3` on 2026-07-22. This plan does
> not by itself authorize production, provider connection, customer/incidental
> caller corpus collection, model tools, automatic terminal actions, or a
> caller-experience claim. Each later execution phase has its own explicit gate.

**Goal:** Select and implement a voice architecture that keeps normal replies
brief without ending mid-thought, answers the caller's question before asking one
follow-up, recovers naturally from silence, handles interruption correctly, and
never lets model wording authorize a side effect or hangup.

Related decision record: [ADR 0002](../../adr/0002-voice-architecture-bakeoff.md).

**Primary decision:** Stop using `max_output_tokens` as the normal conversation
length control. Treat any hard output ceiling as a runaway guardrail: a ceiling hit
is a failed turn that requires explicit recovery, never an accepted response. Run a
preregistered, nonproduction bakeoff before selecting the live architecture.

**Outcome:** An evidence-backed architecture decision, followed by a separately
planned and reviewed integration and exact-SHA staging qualification. No candidate
is preselected or receives scoring preference from its label or implementation
maturity.

---

## 1. Why This Plan Exists

### 1.1 Caller evidence

The response-length experiment did not meet its caller-experience objective:

- Before the cap, normal responses included 7.120 s and 6.828 s of generated
  playout. First provider audio was 868-972 ms in that trace, Twilio first-media
  send was 1 ms, and inbound media peaked at 177 ms. The actionable problem was
  long replies, not monotonic transport slowdown.
- A 128-token cap reduced generated replies to roughly 3.4-3.7 s, but callers
  still reported mid-phrase endings.
- A staging-only 192-token candidate produced a three-turn trace of 4.600 s,
  5.840 s, and 5.840 s. Twilio returned the final playback mark for every turn,
  with no `clear`, interruption, automatic terminal action, or outbound delivery
  error, while the caller still heard incomplete speech.
- The repeated duration plateaus make a hard-ceiling interaction a leading
  hypothesis, not a proven cause. The live service does not correlate provider
  `generationComplete`, usage, semantic completeness, and playout at a common
  response identity.

The 192-token staging evidence came from revision
`kevin-api-staging-00122-lat`, SHA
`316defba0420d92ec7c9207a9331be2514f24c7b`, with model tools and automatic
terminal actions disabled. It is evidence about that revision only. The plan
baseline remains current `origin/main`, where `MAX_RESPONSE_OUTPUT_TOKENS` is 128.

No raw caller transcript or audio is part of this plan.

### 1.2 Code evidence

The current live Gemini path cannot enforce the existing controller policy before
speech:

1. `app/services/gemini_pipeline.py` enqueues Gemini model audio before flushing
   the caller transcript.
2. `docs/adr/0001-gemini-retrospective-caller-turns.md` correctly says the
   retrospective caller-turn surface cannot control the response already being
   generated.
3. `IntakeState`, `DialoguePlanner`, `InstructionComposer`, and replay fixtures
   exist, but the live Gemini path intentionally does not import them.
4. The Gemini silence-check implementation exists, but `GeminiPipeline.start()`
   does not start that task because transcript timing races provider VAD.
5. Call completion can still be inferred from generated goodbye phrases. Staging
   safety controls suppress the action, but phrase detection is not a valid final
   lifecycle authority.
6. `receptionist_replay.py` explicitly reports
   `assistant_text_semantics_validated=false`, `latency_measured=false`,
   `live_behavior_validated=false`, and `release_authorized=false`.

This is not a request for more prompt exceptions. The missing capability is a
mechanical boundary between caller-turn completion, application policy, generated
speech, caller delivery, and terminal actions.

### 1.3 Industry architecture evidence

Current official guidance converges on explicit stages and lifecycle signals:

- Gemini Live exposes automatic or manual activity detection, interruption,
  `generationComplete`, `turnComplete`, session resumption, and `GoAway`. It does
  not document an application-owned permission event that automatically blocks
  every native-audio response until a Hey Kevin planner runs.
- Twilio Media Streams separates media, `mark`, and `clear`. A returned mark proves
  Twilio resolved buffered media; it does not prove semantic completeness or human
  understanding.
- Twilio ConversationRelay exposes final prompt events, streamed text output,
  interruption information, and provider-side latency signals, at the cost of a
  different failure and reconnect surface.
- Vapi documents a staged pipeline: user audio -> VAD -> transcription -> start
  decision -> LLM -> TTS -> assistant audio, with separate start- and stop-speaking
  plans.
- Deepgram/Pipecat patterns similarly separate transport, turn taking,
  transcription, generation, TTS, interruption, and actually spoken context.

References:

- <https://ai.google.dev/gemini-api/docs/live-api/capabilities>
- <https://ai.google.dev/gemini-api/docs/live-api/session-management>
- <https://ai.google.dev/gemini-api/docs/live-api/tools>
- <https://www.twilio.com/docs/voice/media-streams/websocket-messages>
- <https://www.twilio.com/docs/voice/conversationrelay/websocket-messages>
- <https://www.twilio.com/docs/voice/conversationrelay/best-practices>
- <https://docs.vapi.ai/customization/voice-pipeline-configuration>
- <https://docs.vapi.ai/observability/simulations-advanced>
- <https://developers.deepgram.com/docs/build-voice-agent-with-pipecat-and-deepgram>

---

## 2. Panel Decision and Non-Negotiable Conditions

The plan was challenged by independent staff-architecture, security/privacy, and
conversation-product reviewers. All three approved the direction with conditions;
all three blocked the current 192-token staging candidate as a product-qualified
solution.

### 2.1 Shared decision

1. Freeze cap, prompt, model, VAD, and pacing tuning until lifecycle evidence can
   distinguish generated, delivered, interrupted, partial, and semantically
   complete responses.
2. Do not wire the deterministic controller into a live provider path until that
   path proves a pre-response speech-authorization boundary.
3. Do not treat a Twilio playback mark as proof that the caller heard, understood,
   consented, or answered.
4. Keep provider adapters, policy, playout, call lifecycle, and side-effect
   authorization separately owned.
5. Run hard eligibility gates before any weighted comparison.
6. Keep production blocked. A later production decision requires explicit owner
   authorization in that session.
7. Separate offline construction, bounded provider-capability probing, sealed
   technical selection, and closed-loop caller validation. Evidence from an
   earlier tier cannot substitute for a later tier.
8. Permit no provider execution from a self-asserted document. Execution requires
   authenticated multi-role approval, one-use authorization, and technical proof
   that the resolved identity cannot reach production.
9. Treat failed-turn repair, caller-heard interruption, false barge-in, and
   accessibility support boundaries as shared lifecycle requirements rather than
   provider-specific prompt behavior.

### 2.2 Current candidate disposition

| Candidate or artifact | Disposition |
| --- | --- |
| Active 192-token staging revision | Preserve as evidence; no success or release claim; no further tuning in place |
| Main's 128-token cap | Historical baseline only; not an approved long-term control |
| Existing retrospective caller-turn assembler | Retain for retrospective telemetry or explicitly supersede; never relabel as a pre-response gate |
| Existing `IntakeState`/planner/composer | Reuse after a provider-neutral pre-response contract exists |
| Existing `VoicePipeline` | Reuse components only; do not promote its model-owned tools, lexical goodbye, batch TTS, or timers as the new architecture |
| Production | Out of scope and blocked |

---

## 3. Target Ownership Model

The bakeoff may use different providers, but every selectable candidate must expose
the same application-owned lifecycle boundaries.

```text
Authenticated telephony transport
  -> InputTurnLifecycle
       -> caller activity started / ended
       -> transcript or native input candidate
       -> typed CallerObservation
  -> IntakeState + DialoguePlanner
       -> typed SpokenPlan
       -> optional ToolProposal
  -> SpeechAuthorization
       -> approved semantic acts
       -> bounded wording generation
       -> act validation
  -> AssistantGenerationLifecycle
       -> generation started / complete / failed
  -> TTS or native audio adapter
  -> PlayoutLifecycle
       -> media sent / played / partial / cleared / failed
  -> CallLifecycle
       -> pending question
       -> silence recovery
       -> interruption recovery
       -> terminal eligibility

ToolProposal
  -> independent ToolOrchestrator
       -> authentication + tenant scope
       -> capability + confirmation + idempotency
       -> read/write execution or rejection
```

### 3.1 Separate lifecycles

Do not create a third monolithic voice pipeline. Define four versioned lifecycles
with shared correlation keys:

- `InputTurnLifecycle`: caller activity, candidate finality, typed observation, and
  late-fragment handling.
- `AssistantGenerationLifecycle`: authorization, provider generation, semantic-act
  boundaries, completion, cancellation, and failure.
- `PlayoutLifecycle`: generated audio, media sent, playback acknowledgement,
  partial delivery, clear, and delivery failure.
- `CallLifecycle`: pending question, silence, reconnect, terminal eligibility, and
  exactly-once completion.

The contract must explicitly reuse or supersede `CallerTurnEvent` and
`CallerTurnAssembler`. It must not create parallel terms with ambiguous ownership.

### 3.2 Versioned `VoiceEvent`

Every event must use a closed, bounded schema and include:

- schema version;
- source/provenance: `twilio_authenticated`, `provider_untrusted`, or
  `local_authoritative`;
- environment;
- internal contractor, call-session, and stream bindings;
- monotonic sequence and timestamp;
- call epoch, input turn, response generation, and semantic-act IDs where
  applicable;
- sensitivity classification;
- a typed, size-bounded payload.

Unknown, malformed, oversized, duplicated, stale-epoch, cross-environment, or
out-of-order events fail closed without retaining the raw payload.

Internal tenant and call bindings are not log fields. Logs use an
environment-specific HMAC pseudonym.

### 3.3 Versioned `VoiceCommand`

Every command must use a closed allowlist and include:

- schema version;
- environment plus explicit server-derived contractor/tenant, call-session, and
  relevant stream bindings;
- action ID and epoch;
- expiry;
- capability and sensitivity;
- required confirmation policy;
- idempotency key;
- strict typed arguments.

The model cannot choose a tenant, credential, contractor, call session, or
authorization scope.

### 3.4 Semantic speech acts

Track each answer, question, safety instruction, acknowledgement, presence check,
repair, and closure separately:

```text
planned
  -> generation_started
  -> audio_started
  -> transport_resolved | partial | cancelled | failed
  -> caller_playback_observed | playback_inferred | observation_unavailable
```

`transport_resolved` means that a provider or Twilio transport receipt resolved the
bounded media buffer. It is not called `played`, does not mean the caller heard,
understood, or accepted anything, and cannot by itself arm silence or authorize
closure. `caller_playback_observed` is evidence from the sealed caller-side audio
harness that the expected audio reached the caller-side telephone stream; it still
does not prove human understanding or acceptance. `playback_inferred` is a
preregistered conservative no-response deadline after `transport_resolved` for an
operational runtime without real-time caller-side observation. It is recorded as an
inference, remains cancellable by new caller activity, and never establishes a
caller-heard claim.

The authoritative semantic-confirmation source is candidate-specific but sealed
before execution:

- B1 and B2 bind the validated authorized text/act to the exact TTS audio and
  playout IDs.
- Native audio output transcription is supporting evidence only, never the
  authoritative source. A native arm must bind the planned act to generated audio
  through an independently validated method at the required confidence. If it
  cannot, it fails the semantic-act and two-phase question gates.

After interruption or reconnect, rebuild controller/model context from acts whose
delivery state is known. Cleared or partial acts must not become committed caller
context.

### 3.5 Speech authorization

No unvalidated model token may directly ask a high-risk question, disclose private
context, promise work, transfer a call, or trigger closure.

- The planner emits a typed `SpokenPlan` containing eligible acts and forbidden
  acts.
- High-risk questions, safety instructions, commitments, transfers, and terminal
  language are application-rendered or fully validated before TTS.
- Lower-risk wording may stream only within a bounded authorized act, with a
  sentence/segment validator that can stop before TTS rather than after the caller
  hears invalid content.
- The model owns warmth, phrasing, and language inside the authorized act. It does
  not own the next action or side effect.

### 3.6 Question, silence, and failed-turn repair contract

1. Reserve one `QuestionIntent(slot, turn, act_id)` before generating speech.
2. Generate at most one question in that response.
3. Confirm that the expected question was semantically present.
4. Record `transport_resolved` only after its final transport receipt. In the
   qualified harness, record `caller_playback_observed` only from caller-side
   audio; never infer it from a mark, generated text, or provider completion.
5. Arm the first silence timer after `caller_playback_observed` in a qualified
   harness. A runtime without that observation may use only the preregistered
   conservative `playback_inferred` deadline; it cannot claim the question was
   heard and must remain cancellable by any newer caller activity.
6. Any caller activity cancels the timer.
7. On the first timeout, issue one natural presence check as its own semantic act.
8. Arm a second timer only after the presence check has caller-side observation or
   the same conservative, explicitly inferred deadline.
9. On the second timeout, plan a closing act.
10. End the call only after the closing act has caller-side observation or the
    same conservative, explicitly inferred deadline, and no newer caller activity,
    pending question, tool result, or owner action supersedes it. Transport
    resolution alone can never authorize terminal action.

A goodbye phrase, provider completion signal, or playback mark alone can never end
the call.

Failed turns use an explicit, typed repair lifecycle:

1. Classify the failure as recoverable voice delivery, uncertain application
   state, security/privacy, or irrecoverable transport loss.
2. Start a bounded dead-air deadline at the first locally known failure. The exact
   deadline is preregistered in the caller-UX acceptance contract and measured on
   the caller-side clock.
3. A recoverable generation, STT, TTS, playout, or reconnect failure may authorize
   at most one `RepairIntent` for the affected act. The repair acknowledges the
   interruption naturally, preserves only confirmed caller facts, and either
   completes the missing act or offers the deterministic fallback.
4. A ceiling hit is a recoverable failed turn only when semantic state is certain
   and transport remains available. The incomplete act is never committed as
   delivered.
5. Security/privacy failures, uncertain external state, or ambiguous tenant/call
   binding fail closed without retry, disclosure, tool use, or side effect.
6. Silent termination is prohibited while an authenticated transport can still
   deliver the repair or fallback. An irrecoverably disconnected transport records
   the failed terminal without pretending that spoken recovery was possible.
7. Every repair is its own semantic act with authorization, audio, playout, and
   terminal evidence. A failed repair cannot recursively retry.

### 3.7 Side-effect authorization

`DialoguePlanner` may propose a read, write, transfer, or notification action. An
independent deterministic `ToolOrchestrator` authorizes and executes it. The
planner may propose a closure speech act, but only `CallLifecycle` may authorize
call termination.

It must enforce:

- authenticated server-derived tenant and credential scope;
- field-minimized reads and private-memory non-disclosure;
- writes disabled by default;
- owner confirmation or explicitly approved automation;
- strict schemas with unknown fields rejected;
- deadline, cancellation epoch, concurrency, rate, and spend bounds;
- transactional outbox and uniqueness constraints;
- no blind retry after an uncertain external result;
- payload-safe audit outcome.

Caller ID or a phone-number match is a routing and lookup hint, never identity
proof for private-memory disclosure or authorization.

---

## 4. Candidate Architectures

All candidates run with tools and automatic terminal actions absent. Each candidate
uses the same phone-codec inputs, controller contract, scenario manifests, clocks,
event schema, evaluators, and hard gates.

| Arm | Architecture | Purpose | Eligibility condition |
| --- | --- | --- | --- |
| A: Native control | Twilio Media Streams -> Gemini Live native audio -> Twilio | Measure native latency, language, and naturalness with complete lifecycle telemetry | Selectable only if complete typed caller input and a planner permit always precede model audio in every required scenario |
| B1: Streamed chained reference | Twilio Media Streams -> turn detector/Deepgram -> observation extractor -> controller -> streaming text model -> streaming ElevenLabs -> existing mark/clear transport | Reference candidate using current transport and provider components | Must meet latency, language, semantic, interruption, reconnect, privacy, and security gates without reusing unsafe legacy ownership |
| B2: ConversationRelay challenger | Twilio ConversationRelay -> final prompt event -> same controller/text generator -> streamed tokens | Compare managed turn/TTS signaling and observability | Control-only unless a bounded capability probe proves an authoritative real-time, response-correlated normal-playback completion receipt; selectable only after that proof plus all shared gates |
| C: Native manual-turn probe | External/manual activity detection -> controller permit -> Gemini native audio | Test whether native audio can gain a real pre-response application gate | Feasibility only until zero audio-before-permit is proven across pauses, interruptions, reconnects, and required languages |

Pipecat may be evaluated as an implementation framework for an eligible chained
arm. It is not a fifth architecture arm.

No candidate wins because it is already implemented, preferred by a provider, or
cheaper to patch. Hard eligibility precedes weighted scoring.

---

## 5. Stage 0: Decision and Security Documents

### Task 0.0: Audit existing voice candidate branches before creating code

**Files:**

- Create `docs/voice-architecture-bakeoff-artifact-audit.md`.

Audit exact diffs from current `origin/main` for, at minimum:

- draft PR #130 / `codex/voice-completion-instrumentation`, which is 11 commits
  and roughly 9,819 additions over the plan baseline and already contains
  controlled-pipeline, turn, and coordinator implementations;
- draft PR #131 / `codex/native-live-dialogue-safety`, which contains the
  staging-only 192-token and safety-envelope evidence;
- draft PR #127 / `codex/voice-response-length-192-recovery`;
- current-main `caller_turns.py`, `gemini_turn_events.py`, controller, replay,
  media-stream, Gemini, and legacy voice components.

For every Stage 1-4 requirement, classify the existing implementation as:

- `reuse_exactly` with commit, file, tests, and evidence;
- `reuse_after_isolation` with the unsafe ownership that must be removed;
- `rewrite` with the demonstrated contract gap;
- `reject` with the reason and supersession plan;
- `not_implemented`.

Do not cherry-pick, merge, rebase, deploy, or run a historical provider path during
the audit. A large test suite, prior staging revision, or draft PR is not evidence
that its architecture meets this plan.

**Commands:**

```bash
git diff --stat origin/main...codex/voice-completion-instrumentation
git diff --name-status origin/main...codex/voice-completion-instrumentation
git diff --stat origin/main...codex/native-live-dialogue-safety
git diff --stat origin/main...codex/voice-response-length-192-recovery
git diff --check
```

**Gate:** Staff architecture, security/privacy, and conversation-product reviewers
approve the artifact matrix with no unresolved P1. Only an approved `reuse_*`
entry may alter a later task's create-versus-modify decision.

### Task 0.1: Pin provider capabilities and evidence classes

**Files:**

- Create `docs/voice-architecture-provider-capabilities.md`.

For every candidate, pin the official or directly observed source for:

- authenticated input finality and late-fragment behavior;
- the pre-response application permit, or its documented absence;
- generation start, completion, cancellation, and failure;
- authoritative real-time normal-playback completion, partial delivery, clear, and
  delivery failure;
- caller activity/interruption timing and the caller-heard stop boundary;
- reconnect, session resumption, epoch invalidation, and terminal behavior;
- every public ingress and its pre-media authentication sequence;
- provider account/project/subaccount/region attestation and privacy controls.

Each capability is classified as `official_contract`, `offline_static`,
`bounded_connected_probe`, `sealed_selection`, or `unavailable`. A post-call
dashboard event, final text token, generation-complete signal, or transport queue
receipt cannot be relabeled as real-time caller playback completion.

The matrix also assigns each later assertion to exactly one evidence tier:

1. offline/static construction;
2. bounded, synthetic, non-scoring provider capability probe;
3. sealed provider-connected technical selection;
4. closed-loop consenting-participant acceptance.

**Gate:** No candidate adapter is built past a cheap interface stub when a required
selectable capability is `unavailable`. Every selectable arm must pass the
caller-side probe for its response-correlated playback evidence and conservative
runtime alternative; no transport receipt is promoted to caller playback. B2
remains a control-only arm unless that probe validates a response-correlated
normal-playback receipt against caller-side audio. The shared lifecycle is never
weakened to keep an arm selectable.

### Task 0.2: Write the bakeoff ADR

**Files:**

- Create `docs/adr/0002-voice-architecture-bakeoff.md`.
- Link it from this plan and from
  `docs/adr/0001-gemini-retrospective-caller-turns.md` without changing ADR 0001's
  retrospective scope.

The ADR must pin:

- candidate arms and exclusions;
- exact baseline SHA;
- hard eligibility gates and weighted tie-breaker;
- shared event/command vocabulary;
- corpus and holdout rules;
- provider/model/API versions;
- attempt, wall-clock, timeout, and cost caps;
- go/no-go and evidence-integrity rules;
- the fact that a bakeoff result does not authorize production.

**Gate:** Independent staff, security/privacy, and conversation-product reviewers
must approve the ADR with no unresolved P1.

### Task 0.3: Write the security/privacy control annex

**Files:**

- Create `docs/security/voice-architecture-bakeoff-controls.md`.
- Update `docs/security/phase0-side-effect-matrix.md` only if the annex identifies
  a missing canonical side-effect row.

The annex must contain:

1. A trust-boundary and data-flow diagram.
2. A per-candidate provider matrix naming which party receives audio, transcript,
   prompts, metadata, generated text, and synthesized audio.
3. Retention, training/data-sharing, abuse-monitoring, logging/tracing, region,
   DPA/subprocessor, recording/Voice Trace, resumption/cache, and deletion posture.
4. Dedicated nonproduction identities, credentials, quotas, KMS keys, data stores,
   log sinks, Twilio resources, and integration sandboxes.
5. The immutable telemetry allowlist and forbidden-field list.
6. The command capability and side-effect matrix.
7. Abuse, concurrency, message-size, retry, duration, and spend budgets.
8. Stop, rollback, credential-revocation, task-drain, and residue-audit procedures.
9. Trusted provider-execution signer roles and quorum, signer-key provenance,
   algorithm and key identifiers, verification trust store, rotation/revocation,
   approval-envelope immutable custody and access control, nonce consumption,
   explicit no-self-approval/no-break-glass execution, and production-deny
   controls.
10. Rater and participant access, consent, withdrawal, encryption/key custody,
    cache/derivative deletion, and residue-receipt procedures.

**Gate:** Provider execution is blocked until every matrix field has a reviewed,
source-pinned, and executable answer with an owner, expiry/recheck date, and
residue-verification method. Unknown or unverified retention, training/data
sharing, tracing, region, account isolation, deletion, or enabled provider
request/response logging is a no-go, not a waiver. Any approved session resumption
or provider cache is synthetic-only, retention-pinned, and covered by a
before/after residue audit.

### Task 0.4: Reconcile release gates

**Files:**

- Update `docs/voice-enterprise-release-gates.md`.

Changes must:

- replace transcript-fragment latency with two clocks: common corpus/ingress
  ground-truth last-speech-sample -> first-playback evidence for fair selection,
  and candidate-detected activity-end -> first-media-sent for endpointing
  diagnosis;
- separate transport completion from semantic completion;
- add semantic-act, question, silence, safety, and terminal gates;
- add provider privacy and security eligibility;
- preserve exact-SHA, revision-scoped, payload-safe evidence;
- preserve every existing disclosure, transcript-encryption, tenant-isolation,
  retention, deletion, export, and counsel-review blocker unless the canonical
  release-gate document explicitly supersedes it through separate review;
- keep production explicitly owner-authorized.

---

## 6. Stage 1: Minimal Lifecycle and Telemetry Contract

Stage 1 must not refactor the full voice pipelines or wire `IntakeState` live. It
creates the smallest shared contract and measurement harness needed for a fair
bakeoff.

### Task 1.1: Add provider-neutral lifecycle types

**Files:**

- Create `app/services/voice_lifecycle.py`.
- Create `tests/unit/test_voice_lifecycle.py`.
- Update `app/services/caller_turns.py` only to add an explicit adapter or
  supersession boundary; do not relabel retrospective completion.

Test first for:

- schema-version rejection;
- unknown event and command rejection;
- source/provenance validation;
- environment/call/stream mismatch;
- monotonic sequence and epoch handling;
- duplicate, stale, late, and out-of-order events;
- bounded fields and oversized payload rejection;
- semantic-act state transitions;
- idempotent commands;
- raw-payload non-retention on every failure path.

### Task 1.2: Add payload-safe telemetry projection

**Files:**

- Create `app/services/voice_telemetry.py`.
- Create `tests/unit/test_voice_telemetry.py`.

The log projection is an immutable allowlist of bounded enums, booleans, counts,
ordinals, monotonic durations, and mapped error classes. It may emit:

- environment-specific HMAC session pseudonym and candidate arm;
- activity start/end;
- response authorization;
- generation start/complete/cancel/fail;
- provider `generationComplete`, `turnComplete`, and interruption as separate
  facts;
- first/generated/final audio durations and usage counts;
- Twilio first media, mark, clear, queue drain, and delivery status;
- semantic-act state and terminal proposal/authorization outcome;
- silence timer armed/cancelled/fired;
- reconnect lifecycle;
- configured ceiling and a local `ceiling_reached_candidate` boolean.

It must never emit:

- transcript or generated wording;
- audio or provider messages;
- phone numbers, even truncated;
- raw provider, Twilio, or internal session identifier, including Call SID, Stream
  SID, contractor ID, customer record, or OAuth subject;
- tool argument/result;
- credential, token, callback code, provider close text, or exception message.

Use an environment-specific HMAC pseudonym for aggregate correlation. The HMAC key
must not be stored in the repository.

### Task 1.3: Instrument the native control without changing behavior

**Files:**

- Update `app/services/gemini_pipeline.py`.
- Update `app/webhooks/media_stream.py`.
- Extend existing focused tests in `tests/unit/test_receptionist_intelligence.py`
  and media-stream tests, or split focused lifecycle tests when the existing file
  would become less maintainable.

Instrument, without enabling tools or terminal actions:

- provider generation and turn completion independently;
- generation/response/act/epoch IDs;
- common corpus/ingress ground-truth last speech sample plus candidate-detected
  caller activity end where available;
- first generated audio, first media sent, and first playback evidence;
- final generation, queue drain, mark resolution, clear, interruption, and
  reconnect;
- configured output ceiling and usage totals.

If native automatic VAD does not expose a reliable client-observable activity-end
event, record that diagnostic field as unavailable. Every arm still requires the
common ground-truth last-speech-sample -> first-playback measurement; a candidate
cannot hide detector delay by emitting activity-end late. In live staging, use a
common client-owned/passive clock. Never substitute a transcript fragment.

Gemini exposes `generationComplete`; do not invent a provider `finishReason` that
the API does not supply. Semantic completion is assessed separately in the
approved synthetic/consented evidence path.

### Task 1.4: Add the aggregate evaluator

**Files:**

- Create `scripts/evaluate_voice_architecture_bakeoff.py`.
- Create `tests/unit/test_evaluate_voice_architecture_bakeoff.py`.

The evaluator must:

- read newline-delimited JSON from stdin only;
- reject raw/unrecognized fields;
- verify exact candidate arm, revision, source SHA, and sealed manifest digest;
- enforce event correlation and terminal coverage;
- distinguish generated, sent, played, cleared, partial, interrupted, and failed;
- report aggregate metrics only;
- fail on incomplete cohorts, contradictory terminals, missing IDs, ceiling-hit
  accepted turns, or privacy canaries.

---

## 7. Stage 2: Authentication and Experiment Isolation

These controls land before any provider-connected bakeoff.

### Task 2.1: Authenticate every telephony ingress before media or provider work

**Files:**

- Create `app/services/voice_session_auth.py` and
  `tests/unit/test_voice_session_auth.py` for the provider-neutral signature,
  active-execution, token-store, and binding contract.
- Define the interfaces consumed by the bakeoff-only Media Streams,
  ConversationRelay, token-issuer, and callback routes. Task 4.7 creates and
  mounts those isolated routes. Task 2.1 must not edit `app/webhooks/media_stream.py`,
  the production-shaped token issuer, or any live route; a winner-specific,
  separately reviewed integration plan owns such changes.
- Add focused authentication/race tests.

Every Media Streams, ConversationRelay, capability-probe, sealed-window, and
reconnect ingress uses the same logical state machine:

```text
UNTRUSTED_HANDSHAKE
  -> SIGNATURE_VALIDATED
  -> AUTH_PENDING
  -> AUTHENTICATED | REJECTED
```

Require:

- every HTTP/TwiML route that mints a stream/session token validates the Twilio
  signature against its configured canonical external `https` URL and allowlisted
  nonproduction account/subaccount before minting or returning TwiML. It verifies
  the active call, environment, server-derived tenant, and epoch, then atomically
  enforces one issuance per call epoch. Replaying the signed request cannot mint a
  fresh token;
- Twilio request-signature verification against the configured canonical external
  `wss` URL before WebSocket acceptance. The Task-0.1 matrix pins Twilio's
  official canonical HTTP and WebSocket signature construction separately.
  Forwarded host/protocol values are rejected unless produced by an explicitly
  allowlisted trusted proxy; no authority is inferred from client-supplied headers;
- after signature validation, acceptance only into `AUTH_PENDING`, with one
  provider-documented logical setup envelope bounded by frame count, bytes, and
  monotonic timeout. Any media, duplicate setup, or excess frame rejects the
  connection;
- a one-time, short-lived token issued against environment, call, and contractor,
  passed only in protected custom parameters and never a URL or log, with only its
  digest stored;
- a short-lived active-execution record created atomically in the dedicated
  authentication-token store only after the Task-3.4 envelope is locally verified
  and its nonce consumed, and before any PSTN or workload provider contact. It
  binds the approval ID/self-digest, candidate arm, evidence tier/window, manifest
  and configuration digests, approved PSTN source/destination reference digests,
  expiry/caps, expected call, and epoch;
- every HTTP/TwiML token issuer, WebSocket setup, reconnect, and provider/status/
  evidence callback requires that active record and binds its digest plus the
  invocation fields into the issued token or callback capability. After the
  attested Twilio control-plane call-creation request, the runner atomically binds
  its returned call identifier before any token issuer or callback is accepted; a
  route racing before that binding fails closed, and a different or incidental
  call cannot join it;
- atomic token consumption that binds provider account, environment, server-
  derived tenant, call, stream/session, and epoch, then erases the digest on
  consume, expiry, rejection, or teardown;
- replay and concurrent-consume rejection;
- call/stream/tenant/environment mismatch rejection;
- server-derived contractor identity;
- a fresh token and epoch for every reconnect or replacement stream;
- expiry, cap exhaustion, cancellation, or teardown atomically revokes the active
  execution record and every derived token/callback capability. A stale runtime or
  route with no matching active record fails closed before media/provider work;
- before `AUTHENTICATED`, only signature validation and bounded reads/writes in the
  dedicated distributed authentication-token store named by the security annex
  are permitted. That store contains only token digests and bounded bindings,
  uses separate least-privileged credentials, and enforces atomic consume plus
  short TTL deletion;
- no audio read, provider construction/startup, policy event,
  application/business datastore access or mutation, or resource-intensive task
  before `AUTHENTICATED`;
- fail-closed expiry, bounded teardown, and static route/import proof that all
  bakeoff ingress modules are absent from production registration.

The current accept-then-RTDB-token flow is not sufficient evidence by itself.

### Task 2.2: Create the qualification environment contract

**Files:**

- Create `docs/voice-architecture-bakeoff-qualification.md`.
- Create the checked-in secret-free manifest template
  `tests/fixtures/voice_architecture_bakeoff/manifest.json`.

The contract names dedicated nonproduction provider projects/keys, Twilio
resources, Firestore/RTDB stores, quotas, cost caps, retention, deletion, and
evidence locations. It contains credential references only, never credentials.
The experiment execution principal and every resolved external-dependency
credential must be technically incapable of reaching production resources;
application labels or a production denylist are defense in depth, not the primary
isolation boundary.

Tools, writes, automatic terminal actions, provider request/response logging, data
sharing, tracing, and recording remain off throughout the bakeoff. Any reviewed
resumption/cache exception is synthetic-only, retention-pinned, and residue-audited.

### Task 2.3: Add negative security verification

Test:

- missing, invalid, replayed, expired, or cross-environment tokens;
- invalid, cross-account, canonical-URL-mismatched, or replayed HTTP/TwiML token-
  minting requests, plus concurrent one-issuance-per-call-epoch races;
- missing, expired, revoked, wrong-arm, wrong-tier/window, wrong-manifest/config,
  wrong-PSTN-reference, wrong-call/epoch, or cap-exhausted active-execution records;
- incidental signed calls and stale runtimes attempting to mint tokens or invoke
  callbacks outside the consumed approval;
- proxy-header spoofing, canonical URL variants, cross-account signatures,
  pre-authentication media floods, duplicate setup envelopes, concurrent token
  consumption, reconnect token reuse, and cross-epoch binding;
- forged stream/call/tenant IDs;
- unknown, malformed, oversized, duplicate, and stale provider events;
- cross-tenant state, memory, transcript, and post-call access;
- unexpected tools and malicious tool arguments;
- disabled gates, confirmation, idempotency, timeout, cancellation, late result,
  and uncertain external outcome;
- log canaries across application logs, Cloud Logging exports, reports, RTDB,
  Firestore, and provider traces;
- concurrency, long calls, reconnect storms, buffer limits, and hard cost caps.
- route/import enumeration proving every bakeoff Media Streams,
  ConversationRelay, token-issuer, and callback entrypoint is not registered by
  the production application.

Tests must prove that the only pre-authentication datastore activity is the
bounded dedicated authentication-token-store path. No provider, audio, policy,
media, or application/business Firestore/RTDB task is constructed or started
before authentication succeeds.

Any sensitive-log canary, cross-tenant access, unexpected terminal action, or
external write stops the experiment.

---

## 8. Stage 3: Corpus and Bakeoff Harness

### Task 3.1: Build a privacy-approved corpus manifest

Use synthetic phone-codec audio by default. Purpose-recorded consenting-adult audio
may be added only after the security annex defines rights, consent-record digests,
encryption, TTL deletion, adjudication, and residue audits.

Do not use staging calls, production calls, customer transcripts, customer audio,
phone identities, provider raw messages, or credentials.

The sealed corpus must cover:

- direct questions before follow-up;
- one-question maximum;
- phone-number confirmation and correction;
- caller correction of name, service, urgency, address, and callback intent;
- silence after a delivered question;
- long within-turn pauses;
- brief acknowledgements and backchannels;
- deliberate interruption at early, middle, and final speech positions;
- non-interrupting backchannels and noise that must not clear assistant speech;
- noise, packet variation, and telephony PCMU;
- pricing-only/no-callback and callback rejection;
- safety guidance, including plumbing/electrical hazards;
- exact-quote refusal and unsupported promises;
- private-memory non-disclosure;
- reconnect and provider disconnect;
- low-confidence input, output-ceiling, generation timeout, STT, TTS, playout,
  reconnect, uncertain-state, and irrecoverable-transport repair behavior;
- requests to repeat, speak more slowly, wait longer, opt out, or use voicemail;
- multilingual and mid-call code switching;
- unsupported-language fallback.

Before sealing, the owner must approve a finite release-language contract naming
every qualified language, code-switching pair, and unsupported-language fallback.
The matrix must include both Latin- and non-Latin-script languages relevant to the
product market, but the bakeoff evidences only the explicitly named matrix. It does
not substantiate a broader "all languages" release claim. No candidate may reduce
the approved matrix after unsealing.

This bakeoff is voice-only. TTY, RTT, and DTMF control are not claimed as supported
unless the release-language/accessibility contract explicitly adds and qualifies
them. When an unsupported access mode is signaled and authenticated voice remains
available, Kevin must give a truthful, nonterminal limitation and offer an
approved alternative such as repeat, slower speech, more time, opt-out, or
voicemail. DTMF can never bypass authentication, lifecycle, terminal, or
side-effect authorization.

### Task 3.2: Use two evaluation tiers

**Development tier:** Authored scenarios and deterministic mocks may be iterated.
They are not selection evidence.

**Sealed tier, per candidate:**

- two independent windows;
- at least 12 persistent calls and 60 caller turns per window;
- at least 10 deliberate interruptions per window;
- at least 8 silence/long-pause cases per window;
- every language cohort meets a preregistered numeric minimum in both windows;
- multiple stochastic runs of every semantic-critical scenario;
- identical audio, order randomization, provider configuration pinning, and
  evaluator version.

Before sealing, the manifest and ADR must numerically define:

- the applicable denominator for every hard gate and scenario family;
- risk-weighted minimum adversarial opportunities for every zero-tolerance gate;
- per-language and per-code-switch-pair counts for language-dependent semantic,
  safety, correction, interruption, and fallback outcomes;
- the confidence or precision rule for rate gates, including the 95% direct-
  relevance gate;
- which metrics may be pooled and which may not; language-dependent semantic and
  safety outcomes may not be pooled across languages;
- missing-data, early-stop, attrition, participant-withdrawal, replacement, and
  invalid-window rules;
- a mandatory `insufficient_evidence` / no-winner result when a denominator,
  confidence rule, or cohort minimum is not met.

At minimum, each semantic-critical scenario family has eight applicable cases per
window, each zero-tolerance gate has ten adversarial opportunities per window, and
each declared language has twelve caller turns per window spanning direct answer,
correction, interruption, silence, and safety or safe fallback. A stricter
power/precision calculation overrides these floors. The evaluator rejects a
manifest that merely says a cohort is "represented." If the approved cost cap
cannot support valid evidence, narrow the candidate set or declared language
contract before sealing; never weaken counts or thresholds after unsealing.

The sealed holdout instances, audio, order, and expected labels remain in an
external access-controlled evidence store until source and configuration freeze.
Candidate implementers may see schemas, behavioral requirements, development
fixtures, and rubric definitions, but not holdout instances or order. The sealed
tier records manifest, source, setup, evaluator, and artifact digests. Changing a
threshold, prompt, provider config, model, corpus, evaluator, or holdout after
unsealing invalidates the result.

### Task 3.3: Evaluate caller-heard audio

Payload-safe runtime counters cannot prove semantics. The isolated evidence path
must evaluate actual PCMU telephone audio using blinded human adjudication or an
independently validated audio evaluator.

The rubric records only synthetic scenario IDs and bounded labels:

- complete thought;
- direct answer relevance;
- question count;
- required safety content coverage;
- correction uptake;
- repeat question;
- unsupported promise;
- premature closure;
- naturalness and prosodic completeness;
- language match.

The evaluator output contains aggregate labels and digests, not caller or customer
content.

Before unsealing, preregister the adjudication protocol:

- three independent blinded raters for every semantic-critical turn;
- every language-dependent turn is rated by people fluent in that language, and a
  code-switched turn by raters fluent in both named languages;
- randomized candidate/order labels and no access to provider identity;
- unanimous completeness and safety-content agreement for an automatic pass;
- a predesignated independent adjudicator for other disagreements;
- inter-rater reliability of at least 0.80 by the sealed statistic and retest
  agreement of at least 95%; failure invalidates the semantic result;
- if an automated audio evaluator is used, at least 95% precision and recall on a
  separately held validation set for completeness and safety labels;
- an automated score may not overrule unresolved human disagreement.

Raters use named least-privileged identities, short-lived access, a dedicated KMS
key, and audited streaming-only review where feasible. Local downloads, screen or
audio capture, copy/export, and free-text derivative notes are prohibited. The
security annex defines confidentiality/data-processing obligations, consent
withdrawal cutoff, and deletion receipts covering caches, exports, backups, and
derived artifacts. Raw access logs and encrypted evidence remain outside the
repository; only bounded labels and digests reach aggregate reports.

### Task 3.4: Seal and approve provider execution

**Files:**

- Create `scripts/run_voice_architecture_bakeoff.py`.
- Create `tests/unit/test_run_voice_architecture_bakeoff.py`.
- Create
  `tests/fixtures/voice_architecture_bakeoff/provider_approval.schema.json`.

Before any Stage-4 provider connection, bind these values in one signed approval
envelope stored in immutable, IAM-controlled storage outside the repository and
passed with `--approval`:

- exact source SHA and candidate arm;
- corpus, setup, prompt/configuration, evaluator, security-annex, and caller-UX
  acceptance-contract digests;
- a closed external-dependency list covering every telephony, STT, model, TTS,
  storage, and provider control-plane service the arm can contact. Each entry binds
  service role, provider, model/API version where applicable, endpoint and network
  destination allowlist, dedicated credential reference, actual expected
  account/project/subaccount/region, and nonproduction identity;
- per-entry logging/data-sharing/tracing, retention, recording, and
  cache/resumption state, plus the approved nonproduction PSTN source/destination
  reference digests and evidence KMS key version;
- request, attempt, concurrency, duration, byte, audio-duration, retry, token, and
  cost caps;
- artifact TTL and residue-audit destination;
- approval ID, envelope self-digest, nonce, issuance time, expiry, and atomic
  one-use-consumption record;
- trusted signer identities and independent role signatures satisfying the sealed
  quorum policy, including staff, security/privacy, and conversation-product
  approval with no unresolved P1. One identity cannot satisfy multiple roles;
  the envelope also binds signer-key provenance, algorithm, key ID, verification
  trust-store version, rotation/revocation status, immutable-custody policy, and
  an explicit no-self-approval/no-break-glass-execution assertion.

The runner must be dry-run-only by default. Before credential resolution, DNS,
socket creation, or provider construction it locally verifies the envelope
signature/quorum, immutable-store provenance, self-digest, nonce, expiry, source
and artifact digests, candidate, complete dependency list, bounds, destination
allowlists, and immutable production denylist. It then atomically consumes the
nonce and creates the bounded Task-2.1 active-execution record before any PSTN or
workload provider contact. A dedicated broker resolves only the approved
nonproduction credentials. Before each dependency receives a workload request,
its provider-specific allowlisted control-plane attestation must prove the actual
account/project/subaccount/region matches that dependency entry and, where the
provider exposes them, the effective logging, data-sharing, tracing, recording,
retention, and cache/resumption settings match the envelope. An unavailable
effective-setting API requires separately signed configuration evidence named in
the annex; an unknown or drifting setting is a no-go. The runner rejects unlisted
dependencies and credential substitution. The execution principal and every
resolved credential have no production permissions.

A provider-connected mode requires an explicit `--execute-provider` flag plus the
matching envelope. Tests must prove that unsigned, forged, wrong-role,
insufficient-quorum, altered, expired, replayed, self-approved, revoked-key,
unknown-trust-store, break-glass, credential-swapped,
secondary-credential-swapped, dependency-omitted, destination-mismatched, or
production-bound envelopes fail at the earliest possible boundary. Invalid local
authorization fails before secret resolution or network access.

Each probe, candidate arm, technical window, and closed-loop window execution uses
a separately scoped envelope and nonce. An envelope can authorize exactly one
runner invocation and cannot be reused across arms, windows, retries, or evidence
tiers.

**Commands:**

```bash
pytest tests/unit/test_run_voice_architecture_bakeoff.py -q
python scripts/run_voice_architecture_bakeoff.py \
  --arm A \
  --manifest tests/fixtures/voice_architecture_bakeoff/manifest.json \
  --approval /approved/nonrepo/provider-approval.json \
  --dry-run
```

**Gate:** A successful dry run proves contract consistency only. It does not
authorize provider execution. Connected execution additionally requires the
signed one-use envelope, runtime identity attestation, technical production
isolation, and the exact evidence-tier permission defined by Task 0.1.

### Task 3.5: Seal the caller-UX acceptance contract

**Files:**

- Create `docs/voice-architecture-caller-ux-acceptance.md`.

The contract pins, before capability probing:

- the failure taxonomy, dead-air deadlines, allowed repair act, one-retry limit,
  confirmed-fact preservation, deterministic fallbacks, and cases that must fail
  closed without retry;
- ground-truth intentional caller-speech onset -> last audible assistant sample,
  missed-interruption, false-clear, and post-interruption coherence thresholds;
- repeat, slower-speech, longer-wait, opt-out, voicemail, unsupported-language,
  and unsupported-access-mode behavior;
- every declared language cohort, per-language rater qualification, and
  code-switch pairing;
- the closed-loop whole-call hard gates, comparison metrics, participant sampling
  and power rule, counterbalancing, handset/network matrix, consent, withdrawal,
  recording default, access, retention, deletion, and no-tuning rule;
- the no-winner response to insufficient evidence, failed privacy approval, or a
  failed closed-loop hard gate.

Every numerical threshold names its evidence source and rationale. A transport
command metric cannot stand in for caller-heard behavior. The contract makes no
claim that TTY, RTT, DTMF, or any language/access mode absent from its finite matrix
is supported.

**Gate:** Staff-architecture, security/privacy, and conversation-product reviewers
approve the exact contract with no unresolved P1. Its digest is bound into every
capability-probe, sealed-window, evaluator, and approval envelope.

---

## 9. Hard Eligibility Gates

A candidate failing any hard gate is ineligible. Weighted scoring cannot rescue it.

### 9.1 Semantic and conversation gates

| Gate | Requirement |
| --- | ---: |
| Non-interrupted semantic completeness | 100% complete thoughts |
| Ceiling/length termination | zero accepted turns; every suspected ceiling hit fails |
| Direct-question ordering | answer before any follow-up question in 100% of applicable turns |
| Direct-question relevance | at least 95% |
| Questions per assistant turn | at most one in 100% of turns |
| Premature closure with pending question | zero |
| Repeated answered-slot questions | zero |
| Correction uptake | 100% on the next eligible turn |
| Sealed typed-observation correction extraction | 100% on applicable sealed cases after source/configuration freeze |
| Sealed typed-observation field accuracy | at least 99% over applicable sealed fields after source/configuration freeze |
| Fabricated commitment or unsupported promise | zero |
| Safety required-content coverage | 100%; evaluated separately from normal brevity |
| Silence lifecycle | 100% correct arm/cancel/presence-check/second-timeout/close sequence |
| Failed-turn recovery | 100% correct classification, bounded repair/fallback, and confirmed-fact preservation across the declared failure matrix |
| Silent termination while authenticated transport is available | zero |
| Retry after security/privacy failure or uncertain state | zero |
| Partial or cleared speech committed as delivered | zero |

### 9.2 Latency and transport gates

| Gate | Requirement |
| --- | ---: |
| Ground-truth last speech sample -> first playback evidence | p95 <= 1,500 ms; max <= 2,500 ms |
| Candidate-detected activity end -> first media sent | p95 <= 1,500 ms; max <= 2,500 ms |
| First media sent -> first playback evidence | measured for 100% of eligible turns; threshold sealed before execution |
| Normal response playout | hard gate: p95 <= 4,000 ms; max <= 6,000 ms |
| Enterprise infrastructure ceiling | p95 <= 6,000 ms; max <= 8,000 ms |
| Safety response playout | p95 <= 12,000 ms; max <= 15,000 ms, with complete required content |
| Interruption -> Twilio clear | p95 <= 250 ms; max <= 500 ms |
| Ground-truth intentional caller-speech onset -> last audible assistant sample | measured for 100% of intentional interruptions; hard threshold pinned in ADR 0002 before capability probing and never weakened after unsealing |
| Missed intentional interruption | zero |
| Erroneous clear on manifest-labeled backchannel/noise | zero |
| Post-interruption coherent replanning | 100%; no repeated committed fact, answered question, stale act, or lost correction |
| Stale post-interruption audio | zero |
| Response terminal coverage | 100% generated responses end in complete, interrupted, cancelled, partial, or failed |
| Reconnect lifecycle coverage | 100%; zero stale epochs or duplicated caller input |

Safety turns are excluded from the normal and enterprise response-duration ceilings
only when the manifest marks them safety-critical. They use the separately sealed
safety-duration threshold above, retain 100% required-content and semantic-
completeness gates, and may never be truncated by a token cap. If the required
guidance cannot fit safely, the planner must split it into complete authorized acts
without leaving the immediate hazard instruction unfinished.

### 9.3 Language gates

- Every declared language cohort meets the semantic gates.
- No candidate loses required language coverage without an explicit product
  decision before unsealing.
- Unsupported-language fallback is safe, understandable, and non-terminal until
  the caller has a chance to respond.
- Code switching does not reset state, repeat answered questions, or select the
  wrong tenant/tool context.
- Repeat, slower-speech, longer-wait, opt-out, and voicemail requests map to shared
  typed caller acts and work consistently in every declared language.
- Every language-dependent result meets its preregistered per-language denominator
  and fluent-rater rule; `represented` alone is never a pass.
- TTY, RTT, DTMF, and other excluded modes are described truthfully and use the
  sealed safe fallback without bypassing authorization or ending the call.

### 9.4 Security/privacy gates

Zero:

- unauthenticated, replayed, expired, or mismatched streams accepted;
- provider/media work begun before ingress authentication;
- unsigned, untrusted, replayed, or credential-mismatched execution approval;
- execution identity with production permission or environment mismatch;
- cross-tenant read, write, disclosure, or state mutation;
- sensitive-log canaries;
- unexpected model tool or terminal action;
- duplicate or uncertain external write;
- provider privacy-setting drift;
- transcript/audio residue outside the approved evidence path;
- abuse, concurrency, retention, or cost-budget breach.

### 9.5 Evidence-integrity gates

- Exact source SHA, candidate configuration, manifest, evaluator, and artifact
  digests are present.
- Every started session, input turn, generation, act, and playout has one coherent
  terminal state.
- No out-of-cohort event appears.
- No raw payload appears in logs or reports.
- Every percentage or zero-tolerance result meets its preregistered applicable
  denominator, confidence/precision rule, and cohort minimum; otherwise the
  result is `insufficient_evidence`.
- Both independent windows pass; averages cannot hide a failed window.

### 9.6 Closed-loop caller-UX gates

After both technical windows, every otherwise eligible arm must pass the sealed
Task-3.5 caller-UX contract in Task 5.5:

- whole-call task completion, direct-answer and pending-question comprehension,
  correction uptake, next-step understanding, failure repair, and closure behavior
  meet their preregistered hard thresholds;
- premature closure, unanswered-question hangup, silent recoverable failure, and
  unauthorized side effect remain zero;
- the participant sample meets the preregistered power/precision, language,
  counterbalancing, handset, network, consent, withdrawal, and privacy rules;
- unavailable privacy approval, insufficient evidence, or any failed hard gate
  produces no winner.

Closed-loop evidence cannot rescue a technical-window failure. Subjective effort,
naturalness, and trust inform comparison only after all hard gates pass.

---

## 10. Weighted Selection After Eligibility

Only candidates passing every hard gate are compared:

| Dimension | Weight |
| --- | ---: |
| Semantic task success and conversation correctness | 30% |
| Caller-heard completeness and naturalness | 20% |
| End-of-turn and first-heard latency | 15% |
| Interruption, silence, and reconnect resilience | 15% |
| Language coverage and parity | 10% |
| Security/privacy surface | 5% |
| Operational complexity, observability, and cost | 5% |

The final ADR records the raw technical and closed-loop gate results, weighted
result, dissent, known risks, and evidence that would change the decision.

---

## 11. Stage 4: Offline Candidate Adapters and Bounded Capability Probes

Build candidates in isolated worktrees and bakeoff-only paths. Do not merge
historical voice branches wholesale or register candidate entrypoints in staging
or production routing.

Tasks 4.0-4.7 are offline/static construction gates. They may use deterministic
mocks and development fixtures, but they cannot claim provider latency, language,
caller-heard completeness, playback receipt, or real-provider speech-permit
success. Task 4.8 is the only Stage-4 provider-connected exception: a bounded,
synthetic, non-scoring capability probe after the security and authorization gates
pass. Selection evidence begins only in Stage 5.

Every provider-connected command requires the Task 3.4 signed one-use approval
envelope. Candidate packages are adapter-only and must not be imported by
production routing during the bakeoff.

### Task 4.0: Implement shared speech control for every candidate

**Files:**

- Create `app/services/voice_speech_control.py`.
- Create `tests/unit/test_voice_speech_control.py`.
- Create `tests/fixtures/voice_architecture_bakeoff/spoken_plans.json`.

This provider-neutral module owns the shared implementation of:

- typed `SpokenPlan` and semantic acts;
- typed `RepairIntent`, failure classification, dead-air deadline, and
  deterministic fallback;
- direct-answer-before-follow-up ordering;
- one `QuestionIntent` reservation per eligible turn;
- `SpeechAuthorization` and forbidden-act rejection;
- bounded wording generation and sentence/segment validation;
- high-risk full-text validation before TTS;
- normal versus safety response budgets;
- shared repeat, slower-speech, longer-wait, opt-out, and voicemail acts;
- authorized text/act -> TTS audio -> playout identity binding;
- semantic confirmation and delivered/partial/cancelled act transitions.

B1 and B2 use the identical module, generator contract, validation configuration,
and prompt/configuration digest. A and C must also pass through the interface to be
selectable; native candidates may use a provider-specific binding adapter, but may
not redefine policy or delivery state.

Test first for:

- answer-first ordering and one-question maximum;
- reservation before generation and cancellation on superseding activity;
- rejection of repeated answered slots, unsupported promises, private disclosure,
  extra questions, and terminal language without `CallLifecycle` eligibility;
- high-risk question and safety instruction held until fully validated;
- bounded streaming of lower-risk sentence/segments;
- normal-budget brevity without incomplete thoughts;
- longer safety budget with complete required content and no cap truncation;
- exact semantic-act/text/TTS/playout correlation;
- partial, clear, interruption, reconnect, timeout, and stale-epoch transitions;
- exactly one recoverable repair attempt, confirmed-fact preservation, no repair
  after uncertain/security state, and no recursive repair;
- identical B1/B2 behavior for the same typed observation and state;
- fake-clock determinism and payload-safe failures.

**Commands:**

```bash
pytest tests/unit/test_voice_speech_control.py -q
ruff check app/services/voice_speech_control.py \
  tests/unit/test_voice_speech_control.py
```

**Offline gate:** Zero unauthorized acts reach the candidate adapters or TTS,
B1/B2 parity is exact on the shared fixtures, every pending question is reserved
before speech, and all semantic-act terminals are coherent.

### Task 4.1: Implement one typed observation extractor for B1 and B2

**Files:**

- Create `app/services/caller_observation_extractor.py`.
- Create `tests/unit/test_caller_observation_extractor.py`.
- Create
  `tests/fixtures/voice_architecture_bakeoff/caller_observations.json`.

The extractor consumes one provider-neutral, candidate-final input turn and emits
the existing typed `CallerObservation`. B1 and B2 must use the identical extractor,
schema, validation, model/version/configuration, confidence policy, and timeout.
Provider-specific event shapes stay in their adapters.

Test first for:

- direct-question kind and answer-first intent;
- new fact versus correction, including correction supersession;
- name, phone, service, urgency, address, callback, and language observations;
- multilingual and code-switched input without phrase tables in domain state;
- missing, ambiguous, low-confidence, malformed, extra-field, and contradictory
  output;
- timeout, provider error, cancellation, reconnect, and late result;
- no state mutation on rejected input;
- no private-memory or tenant identifier copied from untrusted model output.

**Commands:**

```bash
pytest tests/unit/test_caller_observation_extractor.py -q
ruff check app/services/caller_observation_extractor.py \
  tests/unit/test_caller_observation_extractor.py
```

**Offline gate:** Development fixtures produce zero invalid state mutations and no
B1/B2 configuration divergence. Task 4.1 may not access, score, or receive labels
from the external sealed correction/observation holdout. The 100% sealed correction
and at-least-99% sealed field-accuracy gates run only after the exact source and
configuration freeze in Stage 5.

### Task 4.2: Implement the shared bakeoff coordinator and call lifecycle

**Files:**

- Create `app/services/voice_call_lifecycle.py`.
- Create `app/services/voice_bakeoff_coordinator.py`.
- Create `tests/unit/test_voice_call_lifecycle.py`.
- Create `tests/unit/test_voice_bakeoff_coordinator.py`.

The bakeoff coordinator is provider-neutral and used identically by every
selectable arm. It wires only typed contracts:

```text
InputTurnLifecycle
  -> CallerObservation extractor
  -> IntakeState + DialoguePlanner
  -> shared speech control
  -> candidate adapter
  -> PlayoutLifecycle
  -> CallLifecycle
```

Candidate adapters receive and emit only versioned `VoiceEvent` and `VoiceCommand`
values. They may not own policy, silence timers, pending-question state, terminal
eligibility, or side effects.

`CallLifecycle` owns:

- the reserved `QuestionIntent` created before speech;
- transition to active `pending_question` only after authoritative semantic
  confirmation plus `caller_playback_observed` in a qualified harness, or the
  preregistered, explicitly inferred and cancellable `playback_inferred` fallback;
- first silence timer arm/cancel only after that observation or inferred fallback,
  never after `transport_resolved` alone;
- one presence-check act and the same observation-or-inference lifecycle;
- second silence timer and closing-act proposal;
- terminal authorization only after the closing act follows the same
  observation-or-inference lifecycle and no newer activity;
- caller activity, owner pickup/decline, interruption, reconnect, voicemail, and
  supersession;
- failed-turn classification, one authorized repair/fallback, bounded dead-air,
  preservation of confirmed facts, and prohibition on silent termination while
  authenticated transport remains available;
- epoch invalidation, stale-event rejection, and exactly-once terminal state.

Both modules use an injected fake clock and deterministic reducers. The coordinator
is bakeoff-only: it is not imported by `media_stream.py`, production routing,
`GeminiPipeline`, or `VoicePipeline`, and it does not authorize live wiring or
staging. Stage 6 still requires a separate winner-specific integration plan.

Test first for:

- identical state/plan/speech/call transitions across all candidate adapters;
- reservation before speech and pending-question activation only after semantic
  confirmation plus the required caller-side observation or conservative inferred
  fallback, never transport resolution alone;
- no timer after partial, cleared, failed, or interrupted questions;
- first-timer arm, caller-activity cancellation, one delivered presence check,
  second timer, delivered closure, and exactly-once termination;
- cancellation/supersession by caller activity, owner action, interruption,
  reconnect, voicemail, and epoch change at every lifecycle point;
- late, duplicate, out-of-order, cross-call, and stale-epoch events;
- reconnect reconstruction from delivered acts only;
- every declared generation/STT/TTS/playout/reconnect/security/uncertain-state
  failure at each lifecycle point, including repair success, repair failure, and
  irrecoverable disconnect;
- no candidate-specific timer, terminal, tool, or state mutation;
- fake-clock determinism and payload-safe failures.

**Commands:**

```bash
pytest tests/unit/test_voice_call_lifecycle.py \
  tests/unit/test_voice_bakeoff_coordinator.py -q
ruff check app/services/voice_call_lifecycle.py \
  app/services/voice_bakeoff_coordinator.py \
  tests/unit/test_voice_call_lifecycle.py \
  tests/unit/test_voice_bakeoff_coordinator.py
```

**Offline gate:** All candidates pass the identical coordinator/lifecycle
contract; the silence and terminal matrices are 100% deterministic; no candidate
adapter owns a timer or terminal action; and static isolation proves no live
import.

### Task 4.3: Arm A native control

**Files:**

- Create `app/services/voice_candidates/__init__.py`.
- Create `app/services/voice_candidates/native_gemini.py`.
- Create `tests/unit/test_voice_candidate_native_gemini.py`.
- Update `scripts/run_voice_architecture_bakeoff.py` to register Arm A.

Test first for:

- model tools absent from provider setup;
- any unexpected tool call denied without executing it;
- automatic terminal actions suppressed;
- lifecycle mapping for generation, turn completion, interruption, audio, playout,
  reconnect, resumption, and `GoAway`;
- no accepted turn after any token, audio-duration, byte, wall-clock, or cost bound;
- zero audio before permit in the manual-gated selectable variant;
- raw provider payload non-retention.

Define two preregistered configurations for later provider-connected execution:
the current 128-token baseline and one runaway-only ceiling. Exercise both
configurations against offline mocks in this task, then bind the frozen values into
the capability-probe and sealed-window envelopes. Never remove the ceiling. The
runaway-only configuration also has independent audio-duration, byte, wall-clock,
request, and cost bounds. A bound hit fails the turn and any connected window.

**Commands:**

```bash
pytest tests/unit/test_voice_candidate_native_gemini.py -q
ruff check app/services/voice_candidates/native_gemini.py \
  tests/unit/test_voice_candidate_native_gemini.py
python scripts/run_voice_architecture_bakeoff.py \
  --arm A \
  --manifest tests/fixtures/voice_architecture_bakeoff/manifest.json \
  --approval /approved/nonrepo/provider-approval.json \
  --dry-run
```

**Offline gate:** Tools are absent from configuration, terminal actions are
suppressed in mocks, all bounds fail closed, lifecycle mappings are total, and
static isolation passes. Real-provider lifecycle, audio-before-permit, latency,
language, and caller-heard assertions are deferred to Tasks 4.8 and Stage 5.
Without a proven pre-response permit, Arm A remains a native-quality control and is
not selectable for policy-controlled Business mode.

### Task 4.4: Arm B1 streamed chained reference

**Files:**

- Create `app/services/voice_candidates/chained_streaming.py`.
- Create `tests/unit/test_voice_candidate_chained_streaming.py`.
- Update `scripts/run_voice_architecture_bakeoff.py` to register Arm B1.

Reuse Twilio transport, Deepgram, ElevenLabs, and mark/clear components only where
their contracts match. Do not reuse direct model-owned tools, lexical goodbye,
batch-TTS ownership, or legacy timer ownership.

Test first for:

- candidate-final turn before observation extraction;
- validated observation before `IntakeState` mutation;
- planner and speech permit before the first text/TTS segment;
- direct answer before at most one question;
- authorized text/act bound to exact TTS and playout IDs;
- high-risk speech withheld until full validation;
- interruption, partial delivery, reconnect, silence, and terminal cancellation;
- tools and automatic terminal actions absent;
- no stale text/audio or duplicate act after epoch change.

**Commands:**

```bash
pytest tests/unit/test_voice_candidate_chained_streaming.py \
  tests/unit/test_caller_observation_extractor.py -q
ruff check app/services/voice_candidates/chained_streaming.py \
  tests/unit/test_voice_candidate_chained_streaming.py
python scripts/run_voice_architecture_bakeoff.py \
  --arm B1 \
  --manifest tests/fixtures/voice_architecture_bakeoff/manifest.json \
  --approval /approved/nonrepo/provider-approval.json \
  --dry-run
```

**Offline gate:** Mocks prove permit ordering, zero invalid state mutation, complete
act/audio identity mapping, deterministic cancellation, and no legacy ownership
leakage. Real-provider permit, delivery, latency, language, and caller-heard gates
are deferred to Tasks 4.8 and Stage 5.

### Task 4.5: Arm B2 ConversationRelay challenger

**Files:**

- Create `app/services/voice_candidates/conversation_relay.py`.
- Define the ConversationRelay ingress/callback adapter contract consumed by the
  isolated Task-4.7 application; do not create or mount a second route here.
- Create `tests/unit/test_voice_candidate_conversation_relay.py`.
- Create focused adapter-boundary tests; Task 4.7 owns ingress authentication and
  production-route isolation tests.
- Update `scripts/run_voice_architecture_bakeoff.py` to register Arm B2.

Test first for:

- final prompt, partial prompt, text token, last token, interruption, and every
  documented playback-related event mapping into the shared lifecycle without
  assuming that final token or provider generation completion means playback;
- identical observation extractor and controller behavior to B1;
- direct answer and one-question contract;
- partial/cleared speech excluded from delivered context;
- unexpected WebSocket disconnect and TwiML re-establishment;
- context reconstruction with no duplicate acts;
- tools and automatic terminal actions absent;
- provider-specific fields rejected outside the adapter.

**Commands:**

```bash
pytest tests/unit/test_voice_candidate_conversation_relay.py \
  tests/unit/test_caller_observation_extractor.py -q
ruff check app/services/voice_candidates/conversation_relay.py \
  tests/unit/test_voice_candidate_conversation_relay.py
python scripts/run_voice_architecture_bakeoff.py \
  --arm B2 \
  --manifest tests/fixtures/voice_architecture_bakeoff/manifest.json \
  --approval /approved/nonrepo/provider-approval.json \
  --dry-run
```

**Offline gate:** B2 passes the shared mocked semantic/security contract,
disconnect recovery has zero duplicated or stale acts, its ingress is authenticated
and statically absent from production routing, and Twilio-specific shapes do not
enter state or planner modules. B2 remains control-only until Task 4.8 proves an
authoritative real-time normal-playback receipt distinguishable from interruption,
preemption, final-token emission, and post-call Insights.

### Task 4.6: Arm C manual-turn feasibility probe

**Files:**

- Create `app/services/voice_candidates/manual_native.py`.
- Create `tests/unit/test_voice_candidate_manual_native.py`.
- Update `scripts/run_voice_architecture_bakeoff.py` to register Arm C.

In this dedicated experiment only, disable native automatic VAD and send explicit
activity start/end from the shared external detector.

Test first for:

- complete input and typed observation before planner execution;
- planner permit before every native model audio frame;
- activity start/end ordering, long pauses, backchannels, and code switching;
- late transcript/provider events and cross-turn contamination;
- interruption, clear, reconnect, and stale-epoch cancellation;
- tools and automatic terminal actions absent;
- bounded timeout when native generation does not begin or complete.

**Commands:**

```bash
pytest tests/unit/test_voice_candidate_manual_native.py -q
ruff check app/services/voice_candidates/manual_native.py \
  tests/unit/test_voice_candidate_manual_native.py
python scripts/run_voice_architecture_bakeoff.py \
  --arm C \
  --manifest tests/fixtures/voice_architecture_bakeoff/manifest.json \
  --approval /approved/nonrepo/provider-approval.json \
  --dry-run
```

**Offline gate:** Mocks prove permit ordering, stale/late rejection, bounded
generation timeout, and total lifecycle mapping. Real-provider audio-before-permit,
common-clock, and latency assertions are deferred to Tasks 4.8 and Stage 5.

### Task 4.7: Build the isolated bakeoff runtime and caller-side harness

**Files:**

- Create `app/experiments/__init__.py`.
- Create `app/experiments/voice_bakeoff_app.py`.
- Create `tests/unit/test_voice_bakeoff_app_isolation.py`.
- Create `scripts/voice_bakeoff_caller.py`.
- Create `tests/unit/test_voice_bakeoff_caller.py`.
- Update `scripts/run_voice_architecture_bakeoff.py` to invoke the caller harness.

`voice_bakeoff_app.py` is a separate application entrypoint for the experiment. It
owns every bakeoff-only HTTP/TwiML token issuer, Media Streams WebSocket used by
A/B1/C, ConversationRelay WebSocket used by B2, status callback, reconnect route,
and evidence callback named by the Task-0.1 matrix. It mounts no production router,
customer route, tool, write, transfer, notification, or automatic terminal action.
`app.main` and production routing may not import, mount, or dynamically discover
it. Connected startup requires the active Task-2.1 execution-record digest derived
from the consumed envelope, the matching environment/configuration digest, and an
execution identity with no production permissions. Dry-run startup uses a bounded
local fixture that cannot enable a network route.

Every route implements Task 2.1's canonical signature, dedicated authentication-
store, bounded `AUTH_PENDING`, token binding, and pre-media rules. Every external
dependency is declared in the Task-3.4 closed list; the runtime has no generic
provider URL or credential escape hatch. Every route and callback fails closed
when the active execution record is absent, expired, revoked, exhausted, or does
not match the arm, tier/window, manifest/configuration, PSTN references, call, and
epoch.

The caller harness must:

- use one monotonic clock for the ground-truth last input speech sample, intentional
  caller-speech onset, last received assistant sample, and first received playback
  sample;
- send development or sealed PCMU audio and timing schedules without changing
  them per arm;
- capture returned caller-side PCMU audio only in the approved encrypted ephemeral
  evidence location, never in the repository or ordinary logs;
- record only allowlisted lifecycle events and artifact digests;
- randomize candidate/scenario order when a sealed manifest requires it;
- apply request, call, duration, byte, retry, concurrency, and cost caps;
- abort and close all sessions on a stop trigger;
- keep Twilio recording, Voice Trace, provider request/response logging, data
  sharing, and tracing disabled.

Test the application and harness offline for route/import isolation, production-
configuration rejection, canonical auth-store-only pre-auth behavior, dependency-
list enforcement, common-clock accuracy, deterministic schedules, PCMU round trip,
candidate-order randomization, encrypted artifact handling, cap enforcement,
cancellation, teardown, and raw-payload non-retention.

**Offline gate:** Static import/route enumeration proves `app.main` cannot mount or
import the bakeoff app; every candidate and callback has an isolated authenticated
route; the dry-run harness reproduces its schedule/common clock; encrypted evidence
teardown leaves zero residue; and no provider network connection occurs.

### Task 4.8: Run a bounded non-scoring provider capability probe

The probe occurs only after Tasks 0.0-3.5 and Tasks 4.0-4.7 offline
gate pass. It uses development-tier synthetic fixtures that are not part of the
sealed holdout, fixed request/call/audio/time/byte/retry/concurrency/cost caps, and
the exact signed one-use approval envelope. It cannot produce a selection score or
substitute for either Stage-5 technical window.

For each arm, probe only the unresolved Task-0.1 protocol facts:

- runtime attestation of every external dependency's
  account/project/subaccount/region before that dependency is contacted;
- input-finality and pre-response-permit ordering;
- generation completion/cancellation/failure;
- transport resolution, partial, clear, and failed-delivery signals correlated to
  caller-side audio, plus proof whether caller playback can be observed or only
  conservatively inferred;
- caller activity, interruption, audible stop, reconnect, and epoch invalidation;
- proof that no media/provider work begins before ingress authentication.

The probe must use the Task-4.7 application and caller harness with their approved
encrypted evidence path. Manual calls, ad hoc TwiML/WebSocket mounting, or manual
caller-side capture cannot satisfy a capability gate.

Freeze source, model, prompts, configuration, capability mappings, and provider
versions after the probe. A failed capability may eliminate an arm or leave it as
a clearly labeled observational control. It cannot be patched during a sealed
window. Every arm is ineligible for weighted selection unless the probe proves its
caller-side playback evidence and conservative operational alternative can safely
drive pending-question, silence, and terminal lifecycles. Specifically, B2 remains
control-only unless it proves a real-time response-correlated normal-playback
receipt distinguishable from transport resolution.

**Gate:** The capability matrix contains direct evidence for every required
selectable protocol fact, the residue audit passes, and all selected arms are
configuration-frozen. Semantic quality, latency thresholds, language quality, and
caller-experience claims remain untested until Stage 5.

---

## 12. Stage 5: Execute and Seal the Provider-Connected Bakeoff

No sealed provider-connected selection run occurs until Tasks 0.0-4.8 pass their
applicable gates, Task 3.4 has a matching unexpired/unconsumed signed approval
envelope, and the bound Task-3.5 caller-UX contract digest matches. The bounded
non-scoring Task-4.8 capability probe is the only earlier provider-connected
exception. This stage is nonproduction and uses only the sealed synthetic or
purpose-recorded consented corpus.

### Task 5.1: Requalify and freeze the isolated runtime and caller harness

**Files:**

- Use the exact Task-4.7 bakeoff application, caller-harness, runner, and tests.
- Do not create a second route, harness, clock, evidence path, or entrypoint.

After Task 4.8 freezes source and capability mappings, re-run the complete Task-4.7
offline qualification on the exact frozen files. Bind the bakeoff-app route table,
caller-harness, monotonic-clock, evidence-path, dependency-list, and test-result
digests into each Stage-5 approval envelope. Configure the harness with the sealed
PCMU manifest and randomized schedule without changing its implementation.

**Commands:**

```bash
pytest tests/unit/test_voice_bakeoff_caller.py \
  tests/unit/test_voice_bakeoff_app_isolation.py \
  tests/unit/test_run_voice_architecture_bakeoff.py -q
ruff check scripts/voice_bakeoff_caller.py \
  app/experiments/voice_bakeoff_app.py \
  tests/unit/test_voice_bakeoff_caller.py \
  tests/unit/test_voice_bakeoff_app_isolation.py
```

**Gate:** The frozen application/harness passes every Task-4.7 offline gate with
the sealed manifest, all digests match the approval envelope, production route
enumeration remains empty, and teardown leaves zero residue.

### Task 5.2: Run sealed window 1

Before the first connection:

1. Reverify source SHA, clean worktree, candidate package digests, manifest,
   evaluator, security annex, closed dependency list/configuration, approval
   expiry, quotas, and privacy settings.
2. Confirm the environment is nonproduction and isolated.
3. Confirm tools and automatic terminal actions are absent.
4. Confirm provider/Twilio logging, data sharing, tracing, recording, and Voice
   Trace are off.
5. Confirm the encrypted ephemeral output directory is empty and TTL-controlled.
6. Verify the arm-specific approval envelope locally, atomically consume its
   nonce, attest every resolved nonproduction dependency identity before that
   dependency is contacted, and confirm that the execution principal and all
   credentials have no production permissions.

Run every eligible arm in the manifest's randomized order:

```bash
python scripts/run_voice_architecture_bakeoff.py \
  --arm A \
  --window 1 \
  --manifest tests/fixtures/voice_architecture_bakeoff/manifest.json \
  --approval /approved/nonrepo/provider-approval.json \
  --output-dir /approved/ephemeral/window-1/arm-a \
  --execute-provider
```

Repeat the same command only for other arms eligible after Task 4.8, using their
fresh arm-specific envelope, sealed arm, and output paths.
Do not improvise retries, thresholds, prompts, model settings, or scenario order. A
hard-gate or stop-trigger failure ends that arm's window and records it as
ineligible; it is not patched during a sealed window.

**Gate:** At least 12 complete calls, 60 caller turns, 10 interruptions, and 8
silence/long-pause cases exist for each arm that remains eligible, and every larger
per-gate, scenario, language, confidence, or precision minimum from Task 3.2 is
also met. Event terminals are 100% coherent, and no out-of-cohort or forbidden
field is present. An insufficient denominator fails the window as
`insufficient_evidence`; it is not rounded, pooled without permission, or waived.

### Task 5.3: Evaluate, adjudicate, and audit window 1

Stream the allowlisted event file directly to the aggregate evaluator:

```bash
python scripts/evaluate_voice_architecture_bakeoff.py \
  < /approved/ephemeral/window-1/allowlisted-events.ndjson
```

Then:

1. Verify evaluator/source/configuration/manifest digests.
2. Have the external holdout custodian score the frozen typed observations against
   the sealed correction/field labels and emit only bounded correctness labels and
   aggregates. Candidate implementers never receive holdout instances or expected
   fields. Enforce 100% correction extraction and at least 99% applicable field
   accuracy independently in the window.
3. Run the sealed three-rater audio protocol through the approved short-lived,
   least-privileged streaming review surface; language-dependent turns use fluent
   raters.
4. Resolve disagreements only through the preregistered adjudicator procedure.
5. Calculate the sealed reliability statistic and retest agreement.
6. Produce aggregate semantic, latency, lifecycle, language, security, and cost
   results.
7. Run provider-console, Twilio, log-sink, filesystem, RTDB/Firestore, credential,
   active-session, and integration-sandbox residue audits.
8. Delete ephemeral audio only according to the approved TTL/retention procedure;
   produce deletion receipts covering caches, exports, backups, and derivative
   artifacts, and retain only evidence digests and aggregate labels.

**Gate:** Every hard gate and reliability gate passes for an arm to advance to
window 2. Any privacy drift, residue, raw payload, or evidence-integrity failure
invalidates the window and stops provider execution.

### Task 5.4: Run independent sealed window 2

Window 2 uses fresh calls and sessions, a fresh signed one-use envelope, an empty
encrypted output directory, the same immutable
source/configuration/corpus/evaluator digests, and the manifest's second randomized
order. Reperform every preflight and privacy check from Task 5.2.

```bash
python scripts/run_voice_architecture_bakeoff.py \
  --arm A \
  --window 2 \
  --manifest tests/fixtures/voice_architecture_bakeoff/manifest.json \
  --approval /approved/nonrepo/provider-approval.json \
  --output-dir /approved/ephemeral/window-2/arm-a \
  --execute-provider
```

Repeat only for arms that passed window 1. Apply the same sample, stop, evaluator,
adjudication, and residue-audit rules. No threshold or configuration may change
between windows.

**Gate:** An arm must pass every hard gate independently in both windows. Combined
averages cannot hide a failed window. Only arms that pass both windows may enter
the closed-loop caller-UX window.

### Task 5.5: Run the closed-loop caller-UX acceptance window

Open-loop PCMU replay remains the comparable technical selection instrument, but
it cannot prove adaptive conversation, caller comprehension, or whole-call task
success. Before ADR 0003, every arm that passes both sealed technical windows runs
one separately approved, preregistered, counterbalanced closed-loop window.

The window must:

- use purpose-recruited consenting adults, nonproduction phone identities,
  synthetic personas, and no customer, production, or real business data;
- keep tools, writes, transfers, notifications, automatic terminal actions, and
  production routing absent;
- freeze source, model, prompts, external-dependency configuration, task set,
  scoring, and thresholds before recruitment, with no tuning from participant
  results;
- use a power/precision-derived sample size with a floor of 12 participants per
  surviving arm, counterbalanced arm/task order, and the declared language cohorts;
- exercise direct questions, corrections, necessary follow-up, silence, long
  pauses, backchannels, intentional interruption, degraded network, reconnect and
  recoverable failure, repeat/slower/more-time requests, opt-out, voicemail,
  unsupported language/access-mode fallback, and clear closure expectations;
- include real handsets in earpiece and speakerphone modes;
- score full-call task completion, direct-answer comprehension, pending-question
  comprehension, correction uptake, next-step expectations, failure repair, and
  absence of premature closure as preregistered hard gates;
- score caller effort, follow-up necessity, naturalness, and trust as blinded
  comparison metrics that cannot rescue a failed hard gate;
- collect no raw audio by default. If caller-side recording is essential for an
  audible metric, the signed approval must explicitly authorize it and all Task
  0.3 rater/participant custody, encryption, withdrawal, deletion, and residue
  controls apply.

Participants may withdraw until the preregistered cutoff before aggregate sealing;
withdrawal invalidates or replaces the sample only under the sealed rule. Consent
must explain that already published anonymous aggregate statistics cannot be
selectively removed after sealing. A failed or privacy-unapproved window yields no
winner. Any proposed fix requires a new source/configuration freeze and complete
technical plus closed-loop rerun; there is no in-window patch.

**Gate:** Every surviving arm meets the preregistered whole-call hard gates and
sample-validity rules. The privacy/residue audit and deletion receipts pass, and
three-panel review finds no unresolved P1. Closed-loop evidence cannot rescue an
arm that failed a technical hard gate.

### Task 5.6: Seal the aggregate decision package

**Files:**

- The repository receives no raw events or audio.
- Generate an external aggregate report whose digest is later referenced by the
  selection ADR.

The package contains exact source/configuration/manifest/evaluator/approval
digests, both technical-window hard-gate tables, the closed-loop acceptance table,
weighted metrics for eligible arms only, adjudication reliability,
privacy/residue/deletion results, cost totals, stop events, and a candidate
eligibility decision. It contains no transcript, audio, phone, raw
provider/Twilio/internal ID, credential, participant identity, or customer data.

**Gate:** Staff architecture, security/privacy, and conversation-product reviewers
approve the aggregate package and residue audit with no unresolved P1 before a
selection ADR is written.

---

## 13. Stage 6: Winner Decision and Integration-Plan Gate

A winning bakeoff selects a candidate only. It does not authorize live integration,
runtime routing, feature flags, staging, or production.

### Task 6.1: Record the selection ADR

**Files:**

- Create `docs/adr/0003-voice-runtime-selection.md`.
- Store only aggregate, payload-safe report digests and result tables in the repo.

The ADR records every hard gate, both sealed technical windows, the closed-loop
caller-UX window, weighted results, provider privacy eligibility, dissent, known
risks, and evidence that would change the decision. A no-winner or
`insufficient_evidence` result is valid and stops integration work.

**Commands:**

```bash
git diff --check
rg -n "transcript|audio.payload|phone|token|secret" \
  docs/adr/0003-voice-runtime-selection.md
```

**Gate:** Three-panel exact-file approval with no unresolved P1.

### Task 6.2: Write a winner-specific design and implementation plan

**Files:**

- Create
  `docs/superpowers/specs/2026-07-22-selected-voice-runtime-design.md`.
- Create
  `docs/superpowers/plans/2026-07-22-selected-voice-runtime-integration.md`.

The winner-specific documents must name exact implementation files and include:

- provider-neutral coordinator ownership outside provider and Twilio adapters;
- validated `CallerObservation` -> `IntakeState` -> planner flow;
- speech authorization and semantic-act/audio binding;
- active fake-clock-tested silence, interruption, reconnect, and call lifecycles;
- lexical-goodbye removal as an action authority;
- independent `ToolOrchestrator`, transactional outbox, and capability policy;
- exact flags and environment defaults, all off by default;
- current-path coexistence, migration, rollback, and state-version compatibility;
- focused, property/fuzz, replay, full-suite, CI, synthetic, exact-SHA staging, and
  rollback tests;
- observability, privacy, retention, abuse, and cost budgets;
- explicit exclusions and deletion/supersession handling for losing adapters.

The plan must split coordinator, state/speech, call lifecycle, and side-effect work
into independently reviewable tasks with exact files, test-first cases, commands,
and per-task gates. It may reuse selected candidate code only after exact-diff
review; it may not merge historical worktrees wholesale.

### Task 6.3: Approve the integration plan

Obtain independent staff-architecture, security/privacy, and conversation-product
reviews of the exact design and implementation-plan files. Resolve every P1 in the
documents and re-review the corrected exact files.

**Gate:** Until all three reviewers approve with no unresolved P1, live imports,
routing changes, feature flags, staging, real-caller use, and production remain
prohibited.

---

## 14. Verification Order

Every implementation slice follows this order:

1. Focused unit tests for the changed contract.
2. Property/fuzz tests for event ordering, bounds, duplicate/stale epochs, and
   payload non-retention.
3. Existing receptionist controller and replay tests.
4. Existing media-stream, Gemini, legacy voice, side-effect, security, and privacy
   tests affected by the slice.
5. Full unit suite on Python 3.12.
6. Ruff on touched Python files.
7. `git diff --check`.
8. Payload-safe added-line scans for credentials, transcript/audio fields, raw IDs,
   and phone-shaped values.
9. Independent staff and security review for each boundary-changing slice.
10. GitHub CI on the exact candidate SHA.

Offline, mock, or synthetic success must be labeled as such. It is not live caller
proof.

---

## 15. Exact-SHA Staging Qualification

Staging begins only after an architecture passes the sealed bakeoff and integration
verification.

1. Deploy through the protected staging workflow only.
2. Verify `/health` before testing and record service, revision, deploy SHA,
   candidate arm, model/tools/terminal flags, and safe configuration digest.
3. Confirm tools and automatic terminal actions remain off for the initial window.
4. Query Cloud Logging only by the active revision and the allowlisted timing event.
5. Run the existing enterprise matrix plus semantic-act, silence, direct-answer,
   correction, safety, multilingual, reconnect, and pending-question cases.
6. Run two complete windows and the aggregate evaluator.
7. Use only synthetic callers or purpose-recorded consenting test participants
   using nonproduction identities. Exclude customers, incidental callers,
   production forwarding, production phone identities, and informal owner calls.
8. Any later customer, incidental-caller, or informal-owner feedback requires a
   separately reviewed protocol outside this plan; do not infer caller experience
   from logs. The purpose-recruited Task-5.5 research window is the only human
   feedback protocol inside this plan.
9. Re-read `/health` after each window to detect superseding deploys.
10. Rehearse staging rollback and prove the prior SHA and safety configuration are
    restored.

Do not ask the owner for another informal caller test before the candidate has
semantic evidence, exact-SHA observability, and an active silence lifecycle. Such a
call would produce feedback but not enough evidence to locate a failure.

---

## 16. Stop and Rollback Triggers

Stop the current window immediately for:

- any caller-heard incomplete non-interrupted turn;
- any accepted ceiling-hit or unknown response terminal;
- missed failed-turn repair, recursive retry, lost confirmed fact, or silent
  termination while authenticated transport remains available;
- audio before speech authorization where authorization is claimed;
- missed, repeated, or multiple questions;
- missed intentional interruption, false clear on labeled backchannel/noise, or
  incoherent post-interruption replanning;
- premature closure or inactive silence recovery;
- stale audio after interruption/reconnect;
- a missing or contradictory lifecycle receipt;
- a sensitive log field or raw evidence leak;
- unauthenticated/replayed stream or cross-tenant behavior;
- invalid/replayed execution envelope, pre-authentication provider/media work, or
  any actual dependency identity outside the approved nonproduction allowlist;
- unexpected tool, external write, transfer, notification, or terminal action;
- provider privacy-setting drift;
- cost, concurrency, duration, or buffer-budget breach;
- exact-SHA or evidence-digest mismatch;
- insufficient per-gate/per-language denominator, unapproved pooling, holdout
  exposure before freeze, or participant/rater custody violation.

Rollback must restore the recorded revision, configuration, secrets, and safety
flags; revoke experiment credentials where relevant; drain in-flight tasks; and
audit external stores and providers for residue or uncertain writes. Code rollback
does not undo an external side effect, so reconciliation is mandatory.

---

## 17. Production Boundary

This plan does not authorize production.

Production remains blocked until:

- the winning architecture passes every hard gate in two sealed bakeoff windows;
- the winning architecture passes the closed-loop caller-UX acceptance window;
- integration passes full verification and exact-SHA staging twice;
- privacy, retention, disclosure, tenant isolation, and rollback are approved;
- all P1 review findings are resolved;
- production configuration and secrets are independently verified;
- a separate production release plan covering exact artifacts, configuration,
  migration, rollback, monitoring, incident ownership, and post-release audit is
  independently reviewed and approved;
- the owner explicitly authorizes a production release in the current session.

Owner authorization is necessary but cannot convert this nonproduction plan into
production authority without the separately reviewed release plan. No merge, CI
pass, staging pass, caller compliment, or agent recommendation substitutes for
either requirement.

---

## 18. Immediate Execution Slice

The first implementation slice after this plan is approved is deliberately narrow:

1. Complete the exact-diff artifact audit for current main and relevant draft voice
   branches; reuse nothing until the matrix is approved.
2. Write the provider-capability matrix, ADR 0002, security/privacy control annex,
   and caller-UX acceptance contract.
3. Re-review the audit and all four documents with staff, security/privacy, and
   conversation-product reviewers.
4. Implement only the versioned lifecycle types, payload-safe telemetry projection,
   and aggregate evaluator with tests.
5. Instrument the native control without controller wiring or behavior changes.
6. Run offline and mocked verification.
7. Stop for exact-diff review before any provider-connected capability probe,
   bakeoff, or staging deployment.

There is no token-cap, prompt, model, VAD, pacing, controller-wiring, staging, or
production change in this first slice.
