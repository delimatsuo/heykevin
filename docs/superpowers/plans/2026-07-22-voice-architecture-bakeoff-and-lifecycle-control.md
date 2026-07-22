# Voice Architecture Bakeoff and Lifecycle Control Plan

> **Status:** Exact-file panel approved on 2026-07-22. Documentation only at
> creation; implementation gates remain mandatory.
> Baseline: `origin/main` at
> `6dc3013df78070cd60871febb1a541977ea4c3b3` on 2026-07-22. This plan does
> not authorize production, real-caller corpus collection, model tools, automatic
> terminal actions, or a caller-experience claim. Each later execution phase has
> its own explicit gate.

**Goal:** Select and implement a voice architecture that keeps normal replies
brief without ending mid-thought, answers the caller's question before asking one
follow-up, recovers naturally from silence, handles interruption correctly, and
never lets model wording authorize a side effect or hangup.

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
and closure separately:

```text
planned
  -> generation_started
  -> audio_started
  -> delivery_acknowledged | partial | cancelled | failed
```

`delivery_acknowledged` means the expected semantic act was confirmed in generated
content and its final media receipt was resolved as played. It does not mean the
human heard, understood, or accepted it.

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

### 3.6 Question and silence contract

1. Reserve one `QuestionIntent(slot, turn, act_id)` before generating speech.
2. Generate at most one question in that response.
3. Confirm that the expected question was semantically present.
4. Transition to `delivery_acknowledged` only after its final playout receipt.
5. Arm the first silence timer only after delivery acknowledgement.
6. Any caller activity cancels the timer.
7. On the first timeout, issue one natural presence check as its own semantic act.
8. Arm a second timer only after the presence check is delivered.
9. On the second timeout, plan a closing act.
10. End the call only after the closing act is delivered and no newer caller
    activity, pending question, tool result, or owner action supersedes it.

A goodbye phrase, provider completion signal, or playback mark alone can never end
the call.

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
| B2: ConversationRelay challenger | Twilio ConversationRelay -> final prompt event -> same controller/text generator -> streamed tokens | Compare managed turn/TTS signaling and observability | Must prove reconnect recovery, provider privacy eligibility, interruption correctness, and no Twilio-specific semantic coupling |
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

### Task 0.1: Write the bakeoff ADR

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

### Task 0.2: Write the security/privacy control annex

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

**Gate:** Provider execution is blocked until every matrix field has a reviewed
answer. Unknown retention or enabled provider request/response logging, data
sharing, or tracing is a no-go, not a waiver. Any approved session resumption or
provider cache is synthetic-only, retention-pinned, and covered by a before/after
residue audit.

### Task 0.3: Reconcile release gates

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

### Task 2.1: Strengthen media-session provenance

**Files:**

- Update `app/webhooks/media_stream.py` and the incoming-call token issuer.
- Add focused authentication/race tests.

Require:

- Twilio request-signature verification before WebSocket acceptance, media reads,
  or provider startup;
- a one-time, short-lived token issued against environment, call, and contractor,
  with only its digest stored;
- atomic token consumption that binds the signed start event's Stream SID and
  erases the token digest on consume, expiry, or teardown;
- replay and concurrent-consume rejection;
- call/stream/tenant/environment mismatch rejection;
- server-derived contractor identity;
- fail-closed expiry and teardown.

The current accept-then-RTDB-token flow is not sufficient evidence by itself.

### Task 2.2: Create the qualification environment contract

**Files:**

- Create `docs/voice-architecture-bakeoff-qualification.md`.
- Create the checked-in secret-free manifest template
  `tests/fixtures/voice_architecture_bakeoff/manifest.json`.

The contract names dedicated nonproduction provider projects/keys, Twilio
resources, Firestore/RTDB stores, quotas, cost caps, retention, deletion, and
evidence locations. It contains credential references only, never credentials.

Tools, writes, automatic terminal actions, provider request/response logging, data
sharing, tracing, and recording remain off throughout the bakeoff. Any reviewed
resumption/cache exception is synthetic-only, retention-pinned, and residue-audited.

### Task 2.3: Add negative security verification

Test:

- missing, invalid, replayed, expired, or cross-environment tokens;
- forged stream/call/tenant IDs;
- unknown, malformed, oversized, duplicate, and stale provider events;
- cross-tenant state, memory, transcript, and post-call access;
- unexpected tools and malicious tool arguments;
- disabled gates, confirmation, idempotency, timeout, cancellation, late result,
  and uncertain external outcome;
- log canaries across application logs, Cloud Logging exports, reports, RTDB,
  Firestore, and provider traces;
- concurrency, long calls, reconnect storms, buffer limits, and hard cost caps.

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
- noise, packet variation, and telephony PCMU;
- pricing-only/no-callback and callback rejection;
- safety guidance, including plumbing/electrical hazards;
- exact-quote refusal and unsupported promises;
- private-memory non-disclosure;
- reconnect and provider disconnect;
- multilingual and mid-call code switching;
- unsupported-language fallback.

Before sealing, the owner must approve a finite release-language contract naming
every qualified language, code-switching pair, and unsupported-language fallback.
The matrix must include both Latin- and non-Latin-script languages relevant to the
product market, but the bakeoff evidences only the explicitly named matrix. It does
not substantiate a broader "all languages" release claim. No candidate may reduce
the approved matrix after unsealing.

### Task 3.2: Use two evaluation tiers

**Development tier:** Authored scenarios and deterministic mocks may be iterated.
They are not selection evidence.

**Sealed tier, per candidate:**

- two independent windows;
- at least 12 persistent calls and 60 caller turns per window;
- at least 10 deliberate interruptions per window;
- at least 8 silence/long-pause cases per window;
- every language cohort represented in both windows;
- multiple stochastic runs of every semantic-critical scenario;
- identical audio, order randomization, provider configuration pinning, and
  evaluator version.

The sealed tier records manifest, source, setup, evaluator, and artifact digests.
Changing a threshold, prompt, provider config, model, corpus, or evaluator after
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
- randomized candidate/order labels and no access to provider identity;
- unanimous completeness and safety-content agreement for an automatic pass;
- a predesignated independent adjudicator for other disagreements;
- inter-rater reliability of at least 0.80 by the sealed statistic and retest
  agreement of at least 95%; failure invalidates the semantic result;
- if an automated audio evaluator is used, at least 95% precision and recall on a
  separately held validation set for completeness and safety labels;
- an automated score may not overrule unresolved human disagreement.

### Task 3.4: Seal and approve provider execution

**Files:**

- Create `scripts/run_voice_architecture_bakeoff.py`.
- Create `tests/unit/test_run_voice_architecture_bakeoff.py`.
- Create
  `tests/fixtures/voice_architecture_bakeoff/provider_approval.schema.json`.

Before any Stage-4 provider connection, bind these values in one reviewed approval
artifact stored outside the repository and passed with `--approval`:

- exact source SHA and candidate arm;
- corpus, setup, prompt/configuration, evaluator, and security-annex digests;
- provider, model, API version, endpoint, and dedicated credential reference;
- nonproduction identity, region, logging/data-sharing/tracing state, retention,
  and cache/resumption state;
- request, attempt, concurrency, duration, byte, audio-duration, retry, token, and
  cost caps;
- artifact TTL and residue-audit destination;
- staff, security/privacy, and conversation-product decisions with no unresolved
  P1.

The runner must be dry-run-only by default and fail before network access when the
approval is absent, expired, mismatched, incomplete, or points at production. A
provider-connected mode requires an explicit `--execute-provider` flag plus the
matching approval artifact. Tests must prove that changing any sealed digest or
bound prevents execution.

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
authorize provider execution; the separately reviewed artifact does.

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
| Fabricated commitment or unsupported promise | zero |
| Safety required-content coverage | 100%; evaluated separately from normal brevity |
| Silence lifecycle | 100% correct arm/cancel/presence-check/second-timeout/close sequence |
| Partial or cleared speech committed as delivered | zero |

### 9.2 Latency and transport gates

| Gate | Requirement |
| --- | ---: |
| Ground-truth last speech sample -> first playback evidence | p95 <= 1,500 ms; max <= 2,500 ms |
| Candidate-detected activity end -> first media sent | p95 <= 1,500 ms; max <= 2,500 ms |
| First media sent -> first playback evidence | measured for 100% of eligible turns; threshold sealed before execution |
| Normal response playout | target p95 <= 4,000 ms; max <= 6,000 ms |
| Enterprise infrastructure ceiling | p95 <= 6,000 ms; max <= 8,000 ms |
| Safety response playout | p95 <= 12,000 ms; max <= 15,000 ms, with complete required content |
| Interruption -> Twilio clear | p95 <= 250 ms; max <= 500 ms |
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

### 9.4 Security/privacy gates

Zero:

- unauthenticated, replayed, expired, or mismatched streams accepted;
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
- Both independent windows pass; averages cannot hide a failed window.

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

The final ADR records the raw gate results, weighted result, dissent, known risks,
and evidence that would change the decision.

---

## 11. Stage 4: Candidate Adapters

Build candidates in isolated worktrees and staging-only paths. Do not merge
historical voice branches wholesale.

Every provider-connected command requires the Task 3.4 approval artifact. Candidate
packages are adapter-only and must not be imported by production routing during the
bakeoff.

### Task 4.0: Implement shared speech control for every candidate

**Files:**

- Create `app/services/voice_speech_control.py`.
- Create `tests/unit/test_voice_speech_control.py`.
- Create `tests/fixtures/voice_architecture_bakeoff/spoken_plans.json`.

This provider-neutral module owns the shared implementation of:

- typed `SpokenPlan` and semantic acts;
- direct-answer-before-follow-up ordering;
- one `QuestionIntent` reservation per eligible turn;
- `SpeechAuthorization` and forbidden-act rejection;
- bounded wording generation and sentence/segment validation;
- high-risk full-text validation before TTS;
- normal versus safety response budgets;
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
- identical B1/B2 behavior for the same typed observation and state;
- fake-clock determinism and payload-safe failures.

**Commands:**

```bash
pytest tests/unit/test_voice_speech_control.py -q
ruff check app/services/voice_speech_control.py \
  tests/unit/test_voice_speech_control.py
```

**Gate:** Zero unauthorized acts reach the candidate adapters or TTS, B1/B2 parity
is exact on the shared fixtures, every pending question is reserved before speech,
and all semantic-act terminals are coherent.

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

**Gate:** Zero invalid state mutations, 100% correction handling on the sealed
correction set, at least 99% field-level accuracy on the sealed observation set,
and no B1/B2 configuration divergence.

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
  confirmation plus played delivery acknowledgement;
- first silence timer arm/cancel after the question is delivered;
- one presence-check act and its completed delivery;
- second silence timer and closing-act proposal;
- terminal authorization only after closing delivery and no newer activity;
- caller activity, owner pickup/decline, interruption, reconnect, voicemail, and
  supersession;
- epoch invalidation, stale-event rejection, and exactly-once terminal state.

Both modules use an injected fake clock and deterministic reducers. The coordinator
is bakeoff-only: it is not imported by `media_stream.py`, production routing,
`GeminiPipeline`, or `VoicePipeline`, and it does not authorize live wiring or
staging. Stage 6 still requires a separate winner-specific integration plan.

Test first for:

- identical state/plan/speech/call transitions across all candidate adapters;
- reservation before speech and pending-question activation only after semantic
  confirmation plus played acknowledgement;
- no timer after partial, cleared, failed, or interrupted questions;
- first-timer arm, caller-activity cancellation, one delivered presence check,
  second timer, delivered closure, and exactly-once termination;
- cancellation/supersession by caller activity, owner action, interruption,
  reconnect, voicemail, and epoch change at every lifecycle point;
- late, duplicate, out-of-order, cross-call, and stale-epoch events;
- reconnect reconstruction from delivered acts only;
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

**Gate:** All candidates pass the identical coordinator/lifecycle contract; the
silence and terminal matrices are 100% deterministic; no candidate adapter owns a
timer or terminal action; and static isolation proves no live import.

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

Run two sealed synthetic configurations: the current 128-token baseline and one
preregistered runaway-only ceiling. Never remove the ceiling. The runaway-only
configuration also has independent audio-duration, byte, wall-clock, request, and
cost bounds. A bound hit fails the turn and candidate window.

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

**Gate:** Tools are absent, terminal actions are suppressed, all bounds fail
closed, and lifecycle evidence is complete. Without a proven pre-response permit,
Arm A remains a native-quality control and is not selectable for policy-controlled
Business mode.

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

**Gate:** Zero audio before permit, zero invalid state mutation, complete act/audio
binding, all hard latency and language gates, and no legacy ownership leakage.

### Task 4.5: Arm B2 ConversationRelay challenger

**Files:**

- Create `app/services/voice_candidates/conversation_relay.py`.
- Create `tests/unit/test_voice_candidate_conversation_relay.py`.
- Update `scripts/run_voice_architecture_bakeoff.py` to register Arm B2.

Test first for:

- final prompt, partial prompt, text token, last token, interruption, and
  tokens-played mapping into the shared lifecycle;
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

**Gate:** B2 passes the shared semantic/latency/security gates, disconnect recovery
has zero duplicated or stale acts, and Twilio-specific shapes do not enter state or
planner modules.

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

**Gate:** Any audio-before-permit, late-fragment contamination, missing common
end-to-end clock, or latency miss makes Arm C ineligible and stops its sealed run.

---

## 12. Stage 5: Execute and Seal the Provider-Connected Bakeoff

No provider-connected run occurs until Tasks 0.0-4.6 pass their gates and Task 3.4
has a matching, unexpired approval artifact. This stage is nonproduction and uses
only the sealed synthetic or purpose-recorded consented corpus.

### Task 5.1: Implement the controlled caller-side PCMU harness

**Files:**

- Create `scripts/voice_bakeoff_caller.py`.
- Create `tests/unit/test_voice_bakeoff_caller.py`.
- Update `scripts/run_voice_architecture_bakeoff.py` to invoke the caller harness.

The caller harness must:

- use one monotonic clock for the ground-truth last input speech sample and first
  received playback sample;
- send the identical sealed PCMU corpus and timing schedule to every arm;
- capture the returned caller-side PCMU audio in the approved encrypted ephemeral
  evidence location, never in the repository or ordinary logs;
- record only allowlisted lifecycle events and artifact digests;
- randomize candidate and scenario order from the sealed manifest;
- apply request, call, duration, byte, retry, concurrency, and cost caps;
- abort and close all sessions on a stop trigger;
- keep Twilio recording, Voice Trace, provider request/response logging, data
  sharing, and tracing disabled.

Test first for common-clock accuracy, deterministic schedules, PCMU round trip,
candidate-order randomization, encrypted artifact handling, cap enforcement,
cancellation, teardown, and raw-payload non-retention.

**Commands:**

```bash
pytest tests/unit/test_voice_bakeoff_caller.py \
  tests/unit/test_run_voice_architecture_bakeoff.py -q
ruff check scripts/voice_bakeoff_caller.py \
  tests/unit/test_voice_bakeoff_caller.py
```

**Gate:** The dry-run harness reproduces the sealed schedule and common clock for
all arms, writes only to the approved encrypted location, and leaves zero residue
after its teardown test.

### Task 5.2: Run sealed window 1

Before the first connection:

1. Reverify source SHA, clean worktree, candidate package digests, manifest,
   evaluator, security annex, provider configuration, approval expiry, quotas, and
   privacy settings.
2. Confirm the environment is nonproduction and isolated.
3. Confirm tools and automatic terminal actions are absent.
4. Confirm provider/Twilio logging, data sharing, tracing, recording, and Voice
   Trace are off.
5. Confirm the encrypted ephemeral output directory is empty and TTL-controlled.

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

Repeat the same command for B1, B2, and C using their sealed arm and output paths.
Do not improvise retries, thresholds, prompts, model settings, or scenario order. A
hard-gate or stop-trigger failure ends that arm's window and records it as
ineligible; it is not patched during a sealed window.

**Gate:** At least 12 complete calls, 60 caller turns, 10 interruptions, and 8
silence/long-pause cases exist for each arm that remains eligible, with 100%
coherent event terminals and no out-of-cohort or forbidden fields.

### Task 5.3: Evaluate, adjudicate, and audit window 1

Stream the allowlisted event file directly to the aggregate evaluator:

```bash
python scripts/evaluate_voice_architecture_bakeoff.py \
  < /approved/ephemeral/window-1/allowlisted-events.ndjson
```

Then:

1. Verify evaluator/source/configuration/manifest digests.
2. Run the sealed three-rater audio protocol on the encrypted caller-side PCMU
   artifacts.
3. Resolve disagreements only through the preregistered adjudicator procedure.
4. Calculate the sealed reliability statistic and retest agreement.
5. Produce aggregate semantic, latency, lifecycle, language, security, and cost
   results.
6. Run provider-console, Twilio, log-sink, filesystem, RTDB/Firestore, credential,
   active-session, and integration-sandbox residue audits.
7. Delete ephemeral audio only according to the approved TTL/retention procedure;
   retain evidence digests and aggregate labels.

**Gate:** Every hard gate and reliability gate passes for an arm to advance to
window 2. Any privacy drift, residue, raw payload, or evidence-integrity failure
invalidates the window and stops provider execution.

### Task 5.4: Run independent sealed window 2

Window 2 uses fresh calls and sessions, an empty encrypted output directory, the
same immutable source/configuration/corpus/evaluator digests, and the manifest's
second randomized order. Reperform every preflight and privacy check from Task 5.2.

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
averages cannot hide a failed window.

### Task 5.5: Seal the aggregate decision package

**Files:**

- The repository receives no raw events or audio.
- Generate an external aggregate report whose digest is later referenced by the
  selection ADR.

The package contains exact source/configuration/manifest/evaluator/approval
digests, per-window hard-gate tables, weighted metrics for eligible arms only,
adjudication reliability, privacy/residue results, cost totals, stop events, and a
candidate eligibility decision. It contains no transcript, audio, phone, raw
provider/Twilio/internal ID, credential, or customer data.

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

The ADR records every hard gate, both sealed windows, weighted results, provider
privacy eligibility, dissent, known risks, and evidence that would change the
decision. A no-winner result is valid and stops integration work.

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
8. Any later real-caller feedback requires a separately reviewed protocol outside
   this plan; do not infer caller experience from logs.
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
- audio before speech authorization where authorization is claimed;
- missed, repeated, or multiple questions;
- premature closure or inactive silence recovery;
- stale audio after interruption/reconnect;
- a missing or contradictory lifecycle receipt;
- a sensitive log field or raw evidence leak;
- unauthenticated/replayed stream or cross-tenant behavior;
- unexpected tool, external write, transfer, notification, or terminal action;
- provider privacy-setting drift;
- cost, concurrency, duration, or buffer-budget breach;
- exact-SHA or evidence-digest mismatch.

Rollback must restore the recorded revision, configuration, secrets, and safety
flags; revoke experiment credentials where relevant; drain in-flight tasks; and
audit external stores and providers for residue or uncertain writes. Code rollback
does not undo an external side effect, so reconciliation is mandatory.

---

## 17. Production Boundary

This plan does not authorize production.

Production remains blocked until:

- the winning architecture passes every hard gate in two sealed bakeoff windows;
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
2. Write ADR 0002 and the security/privacy control annex.
3. Re-review the audit and both documents with staff, security/privacy, and
   conversation-product reviewers.
4. Implement only the versioned lifecycle types, payload-safe telemetry projection,
   and aggregate evaluator with tests.
5. Instrument the native control without controller wiring or behavior changes.
6. Run offline and mocked verification.
7. Stop for exact-diff review before any provider-connected bakeoff or staging
   deployment.

There is no token-cap, prompt, model, VAD, pacing, controller-wiring, staging, or
production change in this first slice.
