# Typed Receptionist Observation Qualification Plan

> **Status:** Current-main Gate 0A closure remains blocked as of 2026-07-19.
> The historical audit baseline is
> `218822f2a2d1fa06d285de12d1ebeaecd26f6461`; current `main` is
> `baf2fd9fee82e4a769550a556ebf308c3a5704d9`. Gate 0 artifacts are unchanged,
> but the live Gemini and legacy voice pipeline files drifted after the reviewed
> baseline, and the hard-pinned qualification command correctly rejects the
> current tree with `immutable_source_mismatch`. See
> `docs/gate0a-current-main-drift-reconciliation.md`. This revision authorizes
> documentation, SHA-bound offline audits, synthetic fixtures, and
> mocked-provider tests only. It does not authorize repinning the runner,
> provider execution, real caller data, a runtime worker, live pipeline wiring,
> staging, production, deployment, or changes to PR #76.

**Goal:** Determine whether Hey Kevin can reliably assemble a retrospective caller
turn from Gemini Live events and extract a validated, multilingual
`CallerObservation` without adding language phrase tables or changing caller-visible
behavior.

**Architecture:** Raw Gemini events pass through a Gemini-specific adapter into a
provider-neutral `CallerTurnAssembler`. Only a retrospective, bounded
`CallerTurn` may reach a separate observation extractor. The extractor returns a
canonical structured payload that is semantically validated and mapped into the
existing deterministic `CallerObservation` domain type. No component in this plan
is imported by the live pipelines.

**Outcome:** A go/no-go evidence package for a later shadow-runtime plan. Even a
passing result does not prove current-turn control, prevent repeated questions,
authorize real caller data, or authorize release.

---

## 1. Decision and Supersession

Draft PRs #87 (`codex/receptionist-controller-shadow`) and #90
(`codex/receptionist-turn-parity`) depend on the removed
`IntakeState.observe_caller_turn(text)` API. Their language-specific transcript
parser conflicts with the merged controller contract. Freeze both PRs and
cherry-pick none of their implementation commits.

This plan supersedes only those draft PRs' parser and shadow-wiring assumptions. It
does not close, rewrite, force-push, or otherwise modify them. Closing or marking
them superseded is a separate user decision after a replacement exists.

The merged controller rules remain non-negotiable:

1. `IntakeState` accepts only typed, provider-neutral `CallerObservation` values.
2. `IntakeState`, `DialoguePlanner`, and `InstructionComposer` contain no model
   clients, transcript parsing, phrase tables, or language-specific rules.
3. Schema-conformant model output remains untrusted until semantic validation.
4. Missing, ambiguous, malformed, late, or rejected input causes no state change.
5. Offline or retrospective evidence cannot be labeled as live behavior evidence.

### 1.1 Historical rebaseline

The original Task 0A-0C creation sequence is historical. At immutable baseline
`218822f2a2d1fa06d285de12d1ebeaecd26f6461`, the following artifacts already
exist and must be audited rather than recreated:

| Plan requirement | Existing artifact and test | Rebaseline result and known gap |
| --- | --- | --- |
| Provider-neutral retrospective assembly | `app/services/caller_turns.py`, `tests/unit/test_caller_turns.py`, `tests/fixtures/caller_turn_events/permutations.json` | focused synthetic-permutation coverage passed; no demonstrated offline contract gap |
| Gemini event decoding and aggregate evaluator | `app/services/gemini_turn_events.py`, `scripts/evaluate_caller_turn_assembly.py`, associated unit tests | payload-free evaluator passed with exact fixture and source digests; no demonstrated offline contract gap |
| Dry-run-first qualification command | `scripts/qualify_gemini_caller_turn_assembly.py`, manifest, qualification document, and unit tests | command and manifest exist with dry-run-first bounds; provider execution remains unreviewed and unauthorized, not an offline-code gap |
| Live-path isolation | `app/services/gemini_pipeline.py`, `app/services/voice_pipeline.py` | static search found no Gate 0 artifact import or wiring; no demonstrated live-coupling gap |

The rebaseline ran 95 focused Gate 0 tests and 737 full unit tests, with Ruff and
Bandit passing on the Gate 0 source set. The synthetic evaluator passed with its
source and fixture digests pinned, while retaining every provider, caller-data,
staging, production, and release authorization field as `false`. Targeted privacy
scans found only synthetic boundary data and fixed hashes, not credentials or caller
data.

The Gate 0A deliverable is an exact-SHA audit matrix. It must identify each
requirement, the existing implementation and test, any demonstrated gap, and the
expected offline evidence. Only a demonstrated gap may authorize a new offline
test or implementation ticket. Re-running the historical creation tasks is not
authorized.

### 1.2 Current-main drift reconciliation

The 2026-07-19 reconciliation at
`baf2fd9fee82e4a769550a556ebf308c3a5704d9` found no Gate 0 import or wiring in
either live pipeline and no change to the Gate 0 source, tests, evaluator,
fixtures, ADR, or qualification runbook. It did find behavior-affecting live
pipeline drift after `218822f`, including generation bounds, queue bounds,
greeting construction, assistant-disclosure instructions, and voice-turn latency
telemetry. Those changes are outside this plan's authority and invalidate a
baseline-to-current no-diff claim.

The current qualification command remains fail closed. Do not update its immutable
source hashes as part of Gate 0A. The exact hash inventory, drift classification,
verification results, and successor requirements are recorded in
`docs/gate0a-current-main-drift-reconciliation.md`. A fresh staff/security review
and separately authorized merge of that reconciliation are required before a
separate user decision may authorize drafting any Gate 0B successor plan.

## 2. Gate 0: Establish the Input Contract First

### 2.1 Why Gate 0 blocks extraction work

The current `GeminiPipeline._flush_caller_transcript()` is an opportunistic callback,
not a final-turn guarantee. It flushes when model output appears, before tool
execution, and during reconnect. Gemini documents that input transcription is sent
independently from other server messages with no guaranteed ordering. The
transcription object has text but no finality marker.

Therefore:

- model audio may start before all input transcript fragments arrive;
- `turnComplete`, interruption, tool, reconnect, and transcription events can be
  observed in different orders;
- a flush-based sidecar cannot control the response already being generated;
- any assembled turn in this architecture is retrospective;
- extraction Tasks 1-4 must not freeze an input contract until Gate 0 passes.

Official references:

- https://ai.google.dev/api/live
- https://ai.google.dev/gemini-api/docs/live-api/capabilities
- https://ai.google.dev/gemini-api/docs/live-api/tools
- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview
- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite
- https://ai.google.dev/gemini-api/docs/structured-output
- https://ai.google.dev/gemini-api/docs/interactions-overview
- https://ai.google.dev/gemini-api/docs/zdr
- https://ai.google.dev/gemini-api/docs/changelog

### 2.2 Event and turn contracts

Use two ownership boundaries:

```text
Gemini raw server message
  -> GeminiTurnEventAdapter
  -> provider-neutral CallerTurnEvent
  -> CallerTurnAssembler
  -> RetrospectiveCallerTurn
```

`GeminiTurnEventAdapter` performs bounded shape decoding only. It emits typed events
such as:

- `input_transcript_fragment`
- `model_output_started`
- `generation_complete`
- `turn_complete`
- `interrupted`
- `tool_call_started`
- `tool_call_cancelled`
- `connection_closed`
- `reconnect_started`
- `pipeline_stopped`

`CallerTurnAssembler` has no Gemini imports. It uses an injected monotonic clock and
an explicit candidate quiescence policy. It never claims provider finality.

`RetrospectiveCallerTurn` contains:

- schema version;
- call-local epoch and monotonic turn ID, retained locally only;
- bounded normalized transcript;
- completion status: `retrospective_complete`, `partial`, `cancelled`, or `dropped`;
- bounded close reason enum;
- timing needed for offline readiness metrics.

It contains no call SID, phone, caller identity, contractor identity, raw event,
audio, prompt, model output, exception text, or provider credential.

### 2.3 Event permutation coverage

The deterministic suite must cover at least:

- one and many transcript fragments before model output;
- model output before the first or last transcript fragment;
- transcript fragments before and after `generationComplete` and `turnComplete`;
- `interrupted` followed by `turnComplete`;
- interruption while transcript fragments are pending;
- tool call before/after transcript fragments and tool cancellation;
- long within-turn pauses and rapid consecutive caller activities;
- duplicate fragments and duplicate terminal events;
- connection close before any terminal event;
- reconnect with pending fragments;
- stop/cancellation with a pending timer;
- late old-epoch events after reconnect;
- empty, oversized, malformed-Unicode, and control-character fragments;
- adversarial event volume and timer/resource exhaustion.

Every event fixture is synthetic or irreversibly payload-redacted. Raw provider
traces are not committed, logged, or retained after fixture reduction.

### 2.4 Gate 0 empirical qualification

Synthetic permutations cannot prove actual provider ordering. Before Gate 0 can
pass, an explicitly approved bounded provider run must use only synthetic scripts
spoken by consenting adult test speakers. It must include telephony codec loss,
noise, pauses, corrections, numbers, barge-in, tools, and reconnect.

The run pre-registers candidate quiescence policies and measures:

- activity-to-turn assignment accuracy;
- missing, duplicate, and cross-turn fragment rates;
- late-fragment distribution;
- partial/cancelled/dropped rates;
- retrospective-turn readiness relative to model output and the next caller
  activity;
- per-language and code-switch direction breakdowns.

Gate 0 passes for retrospective extraction only when:

- exactly one assembled turn is produced for at least 99% of eligible activities;
- cross-turn transcript contamination is zero on the holdout;
- duplicate application is zero;
- partial/cancelled/dropped outcomes are classified correctly in every lifecycle
  test;
- no late fragment arrives outside the selected policy in a way that changes an
  accepted turn;
- every language stratum meets the same assembly threshold;
- all resource, cancellation, reconnect, and teardown tests fail closed.

If no bounded policy passes, stop. Do not build the extractor around the current
Gemini transcription surface.

### 2.5 Gate 0 ADR decision

Record an ADR that states:

1. whether retrospective turn assembly is viable;
2. the exact evidence and limitations;
3. the selected bounded finalization policy or a no-go decision;
4. that this surface cannot provide same-turn control;
5. which future pre-response control surfaces remain candidates;
6. that no shadow/runtime plan exists until Gate 0 receives a go decision.

### 2.6 Rebaseline evidence separation

Gate 0A may prove deterministic offline contract coverage only. Its evaluator must
continue to report `turn_assembly_validated: false`,
`provider_execution_authorized: false`, `real_caller_data_authorized: false`,
`staging_authorized: false`, and `release_authorized: false`. A synthetic fixture
pass cannot establish Gemini event ordering, provider behavior, caller-heard
latency, same-turn control, or a go decision for Gate 0B.

Gate 0B is the separately approved empirical provider qualification. It is the
only gate that can evaluate the pre-registered ordering and lifecycle thresholds,
and it still cannot establish caller experience or active control without later
Twilio/media-egress evidence.

## 3. Evidence Taxonomy

Every report uses a versioned schema with independent booleans. A combined `pass`
or model `winner` must not imply readiness beyond the measured layer.

Required fields:

```json
{
  "turn_assembly_validated": false,
  "semantic_extraction_validated": false,
  "shadow_operational_isolation_validated": false,
  "caller_experience_neutrality_validated": false,
  "active_control_validated": false,
  "provider_execution_authorized": false,
  "real_caller_data_authorized": false,
  "staging_authorized": false,
  "production_authorized": false,
  "release_authorized": false
}
```

Tests prevent omission, renaming, or provider/CLI override of these fields.

## 4. Canonical Observation Contracts

This section is implemented only after Gate 0 passes.

### 4.1 One schema source

Create one versioned Pydantic `CallerObservationPayload` as the source for both:

- provider JSON Schema generation; and
- provider response decoding and validation.

Its `to_domain()` method is the only mapping to the merged
`receptionist_state.CallerObservation` dataclass. Do not maintain a parallel JSON
schema by hand.

The provider payload includes only fields required by the domain observation. Do
not request `business_scope_reason`; its free-form text is unnecessary for planning
and creates an avoidable privacy surface. `service_object` remains a bounded
canonical noun phrase and is never logged or included in reports.

### 4.2 Language semantics

`language` means the BCP-47 response language implied by the most recent actionable
caller content. An explicit request to continue in a language overrides recency.
Proper nouns and loanwords do not switch language. If the response language is not
clear, return `null` and make no language-state change.

Fixtures must cover both directions of each code-switch pair, explicit language
requests, proper nouns, accented speech, and ambiguous short utterances. No phrase
table is added to controller or adapter code.

### 4.3 Exact provider-bound context

`ObservationContext` uses an explicit constructor and allowlist. It may contain only:

- `business_scope`
- `intent`
- `service_action`
- `urgency`
- `callback_intent`
- `callback_confirmation`
- `address_need`
- `identity_present: bool`
- `identity_confirmation_pending: bool`
- `callback_number_present: bool`
- `callback_confirmation_pending: bool`

It excludes call/contractor IDs, names, phone digits, service object/history,
addresses, known facts, memory, asked slots, prompts, transcript history, and all
other free text. Never serialize `IntakeState.to_dict()` or a generic model dump.
Corrections that cannot be resolved from the current turn and this context return no
observation. Callback confirmation or rejection is accepted only when
`callback_confirmation_pending` is true. `callback_number_present` proves that a
number exists; it does not prove which question an ambiguous confirmation answers.
Confirmation additionally requires `callback_number_present`, but it does not
require the caller to repeat digits in the confirmation turn.

A newly extracted `callback_phone_last_four` is accepted only when the same four
digits are grounded in the current normalized caller transcript. Grounding uses
Unicode decimal-digit normalization and bounded separator removal only; it does not
add language-specific number-word tables. If the current transcript does not
contain a matching numeric candidate, return no new last-four observation and let
the planner clarify later.

Local epoch, turn ID, fixture labels, and report metadata are never sent to the
provider. The adapter reattaches local metadata only after transport returns.

### 4.4 Input and envelope bounds

`ObservationRequest` retains local metadata and one accepted retrospective turn.
The provider payload contains only:

- prompt/schema version;
- normalized transcript, at most 4,000 Unicode code points and 16,000 UTF-8 bytes;
- the exact `ObservationContext`.

Oversized input is rejected; it is not silently truncated. Unicode is normalized
with a documented fixed form. Null/control characters are rejected.

`ObservationEnvelope` contains:

- schema version, local epoch, and local turn ID;
- status: `accepted`, `empty`, `timeout`, `provider_error`, `invalid_json`,
  `schema_rejected`, `semantic_rejected`, or `cancelled`;
- `CallerObservation | None`;
- exact model ID and bounded latency;
- no prompt, transcript, raw output, rationale, provider body, or exception text.

### 4.5 Semantic rejection

Reject the entire observation rather than partially applying it when:

- any unknown field, wrong type, invalid enum, or prose outside the schema appears;
- text is over-length or contains controls;
- a phone value is not exactly four digits;
- identity confirmation lacks `identity_present` or a pending confirmation;
- callback confirmation or rejection lacks `callback_confirmation_pending`;
- callback confirmation lacks `callback_number_present`;
- a newly extracted callback last-four is not grounded by matching normalized
  digits in the current caller turn;
- urgency/emergency or callback values are inconsistent;
- a service object copies unrelated instructions or contact/location content;
- local epoch/turn state is stale, duplicate, cancelled, or closed.

Rejected output never calls `IntakeState.apply_caller_observation()`.

## 5. Two Separate Qualification Corpora

### 5.1 Turn assembly and audio corpus

Use at least 200 PII-free telephony-audio turns, with at least 20 per language group.
Inputs are purpose-recorded by consenting adults reading synthetic scripts. TTS may
augment but not replace human-speaker coverage.

Cover English, Spanish, Portuguese, French, Mandarin, Hindi, Arabic, and at least
one lower-resource language. Include codec degradation, packet timing variation,
background noise, pauses, fast speech, self-correction, number dictation,
barge-in, tools, reconnect, and two-way code-switching.

Store provenance, consent/usage rights, codec, language, condition labels, and split
membership. Store no voice biometric, caller ID, production audio, or real call
transcript.

### 5.2 Semantic extraction corpus

Use at least 400 synthetic text turns, split before provider execution into a
development set and sealed holdout. Require at least 40 holdout examples per
language and at least 50 holdout positives plus 50 holdout negatives for each
critical field where applicable.

At minimum:

- 20% no-fact or conversational turns;
- 20% correction, negation, or self-repair turns;
- 20% mixed-language or code-switched turns;
- emergency/non-emergency near-neighbor pairs;
- all callback intents, confirmations, denials, and corrections;
- indistinguishable-history collision pairs where identical caller text such as
  "yes" follows identity confirmation, callback confirmation, or no pending
  question;
- callback confirmation where pending context and an existing number allow "yes"
  without repeated digits, plus grounded and ungrounded new last-four pairs;
- identity confirmation/denial with valid and invalid context;
- in-scope, out-of-scope, and unclear requests;
- ambiguous service action/object combinations;
- injection, oversized-input, malformed-Unicode, and resource-exhaustion probes;
- multi-turn sequences with expected reducer state and planner action after each
  accepted observation.

Open text uses fixture-authored normalized acceptable-value sets. No model or
embedding judge decides correctness.

### 5.3 Language and safety gates

For the sealed semantic holdout, a candidate passes only when:

- 100% of transport outcomes map to a bounded envelope;
- macro precision is at least 0.98 and macro recall at least 0.95 across enum and
  boolean fields;
- emergency has zero false positives and zero false negatives across at least 50
  positive and 50 near-neighbor negative holdout examples;
- identity confirmation, callback confirmation, and callback last-four have zero
  false positives in their minimum-support strata;
- correction, negation, and no-fact safety subsets are 100% correct;
- full-observation exact match is at least 0.95;
- service-object acceptable-value accuracy is at least 0.95;
- language accuracy is at least 0.95 overall and at least 0.90 per language and
  code-switch direction;
- expected reducer state and expected planner action match on every safety-critical
  sequence and at least 0.98 of all sequences;
- timeout plus provider-error rate is at most 1%;
- p95 provider latency is at most 1,500 ms with a 2,000 ms timeout;
- observation is ready before the next scripted caller activity in at least 99% of
  eligible sequence cases;
- estimated cost is at most USD 0.002 per turn;
- malformed, stale, duplicate, and cancelled results always fail closed;
- all fixtures and reports pass PII, provenance, and secret scans.

These thresholds qualify retrospective extraction only. No winner is valid.

## 6. Provider and Data Boundary

### 6.1 Synthetic-only in this plan

Repository fixtures, Gate 0 provider traces, and Interactions qualification requests
use only synthetic or purpose-recorded consented test content. There is no CLI flag,
environment override, or admin path that permits a real caller transcript.

Before any future runtime plan can send real caller data, all of these are required:

- approved ZDR for the exact paid project;
- separate DPA/subprocessor, geography, consent/privacy-notice, and retention review;
- a dedicated project/service identity/credential and least-privilege Secret
  Manager IAM;
- explicit user approval of that exact evidence.

Paid tier and `store=false` alone are insufficient because Interactions storage and
abuse-monitoring retention are separate controls.

### 6.2 Candidate transport

Qualify, without assuming a winner:

- `gemini-3.1-flash-lite` for low-latency lightweight extraction;
- `gemini-3.5-flash` as a counterbalanced quality candidate.

Use the stateless Interactions API with:

- `store=false` on every request;
- API key in the `x-goog-api-key` header, never a query parameter;
- no previous interaction, background mode, tools, grounding, explicit cache, or
  server-side state;
- exact model allowlist, response-size limit, 2-second deadline, and no retry;
- injected `httpx` transport for tests;
- raw provider output discarded after validation.

Use a dedicated paid-project key for approved synthetic qualification. Do not reuse
or print the live-call key.

### 6.3 Fair bounded run

- Pre-register exact model IDs, schema/prompt versions, thresholds, maximum three
  attempts, candidate-order seed, and fixture digest.
- Counterbalance candidate order by shard.
- Replace an attempt only for a recorded whole-run transport outage.
- Never tune against the sealed holdout.
- Record exact code SHA, model ID, attempts, latency, token use, cost, and all
  evidence-taxonomy fields.
- Persist aggregate metrics only, never prompts, transcripts, raw output, audio, or
  exception bodies.

Provider execution requires separate user approval after mocked tests and dry-run
evidence. This plan document does not grant it.

## 7. Task-Level TDD Plan

Each task starts with a focused failing test, records RED, implements the minimum
change, records GREEN, runs Ruff on touched Python, and creates a scoped commit.

### Task 0A: Audit the provider-neutral turn assembly contract

**Existing artifacts at the rebaseline SHA:**

- `app/services/caller_turns.py`
- `tests/unit/test_caller_turns.py`
- `tests/fixtures/caller_turn_events/permutations.json`

**Tests first:** all section 2.3 permutations, injected-clock finalization, status
classification, bounds, normalization, duplicate/stale rejection, resource caps,
deterministic serialization, and teardown with no pending timers.

**Gap-only policy:** record the requirement-to-test matrix first. Do not modify the
immutable event/turn types or pure assembler unless the matrix identifies a missing
permutation, bound, lifecycle case, or isolation regression. It must remain absent
from live pipelines.

### Task 0B: Audit the Gemini event adapter and Gate 0 evaluator

**Existing artifacts at the rebaseline SHA:**

- `app/services/gemini_turn_events.py`
- `scripts/evaluate_caller_turn_assembly.py`
- `tests/unit/test_gemini_turn_events.py`
- `tests/unit/test_caller_turn_assembly_eval.py`
- `docs/adr/0001-gemini-retrospective-caller-turns.md`

**Tests first:** bounded raw-shape decoding, unknown fields/events, every terminal
order, no raw payload in reports, evidence taxonomy, fixture digest, exact model/SHA,
and no live import.

**Gap-only policy:** verify the raw-message adapter, local evaluator, and ADR
against the matrix. The default evaluator remains fixture-only and performs zero
provider calls. Any new change requires a demonstrated offline gap.

### Task 0C: Audit the dry-run-first Live qualification command

**Existing artifacts at the rebaseline SHA:**

- `scripts/qualify_gemini_caller_turn_assembly.py`
- `tests/unit/test_qualify_gemini_caller_turn_assembly.py`
- `tests/fixtures/caller_turn_audio/manifest.json`
- `docs/gemini-caller-turn-assembly-qualification.md`

The command has no model, project, credential, or endpoint default that can make a
request. Default and `--dry-run` invocations validate the complete plan and perform
zero DNS, socket, or provider activity.

Before execution, build one canonical Live setup document from the approved source
SHA and synthetic contractor configuration. Exclude credentials and caller data,
but include every behavior-affecting field:

- exact model resource, API version, and endpoint;
- canonical system-instruction digest and synthetic prompt-fixture digest;
- complete generation configuration;
- input/output transcription configuration;
- automatic activity detection sensitivities, padding, and silence duration;
- activity handling and turn coverage;
- complete tool declaration digest and tool-response policy;
- reconnect, context-restoration, and retry policy;
- selected turn-assembly quiescence policy;
- WebSocket limits and timeouts;
- runner and evaluator commit SHA plus file digests.

The pre-registration stores the canonical setup digest, its non-sensitive canonical
document, and an explicit list of deviations from the setup produced by the current
immutable `GeminiPipeline` source SHA. An unexplained deviation is a hard failure;
the runner cannot silently substitute defaults.

**Tests first:**

- `--execute` is required for any WebSocket connection;
- default and `--dry-run` perform zero DNS, socket, or provider activity even when
  valid-looking qualification credentials are present in the environment;
- execution requires an approval-named exact model, API version, official endpoint,
  non-production project, dedicated credential reference, manifest path, manifest
  digest, attempt cap, wall-clock cap, session timeout, and maximum cost;
- the canonical Live setup document/digest, quiescence policy, runner/evaluator SHA
  and file digests, explicit setup deviations, model, project, endpoint, credential
  reference, manifest digest, and all caps are copied into the machine-readable
  pre-registration block before connection;
- the command rejects a production project, live-call credential, unknown endpoint,
  changed manifest digest, missing consent/provenance, real-call label, unbounded
  attempts, or absent cost cap;
- setup-digest tests fail on any prompt, generation, transcription, VAD, activity,
  turn-coverage, tool, reconnect, quiescence, WebSocket-limit, timeout, runner, or
  evaluator drift;
- the reviewed manifest contains only synthetic scripts and purpose-recorded audio
  from consenting adults, with language, codec, condition, rights, and split labels;
- a mocked WebSocket covers setup failure, timeout, cancellation, interruption,
  tool call/cancellation, reconnect, partial run, malformed/oversized message, and
  abnormal close;
- raw messages, transcript text, audio bytes, prompts, credentials, and exception
  bodies remain in memory only as long as needed for reduction and never reach a
  file, report, standard logging, or test snapshot;
- persisted output contains aggregate metrics, event-type counts, bounded status
  enums, fixture digest, exact execution identity, and evidence-taxonomy fields
  only;
- a partial or interrupted run is nonauthorizing and cannot be promoted to a pass.

**Gap-only policy:** verify the mocked-testable injected WebSocket transport and
in-memory reducer against the matrix. The current `--execute` path is deliberately
hard-disabled and must continue to report `execution_blocked`; approval alone cannot
make it runnable. Gate 0B requires a separately reviewed successor plan and
implementation to merge before any execution approval can bind that successor's
pre-registration block.

### Gate 0A: Offline review

Stop after the SHA-bound Task 0A-0C audit. Require focused/full unit tests, Ruff,
Bandit, secret/PII scan, event-fixture provenance, no pipeline diff, and a panel
review. Record the evaluator output as offline synthetic evidence only; a `status`
of `pass` cannot override the false evidence-taxonomy fields. No provider call is
authorized.

### Gate 0B: Reserved successor empirical qualification

Gate 0B cannot use the current qualification command. Its provider path is
deliberately hard-disabled. First, a separately reviewed successor plan and
implementation must merge from fresh `main`, preserving no live-pipeline wiring and
binding its own immutable source SHA. Only then may a separate execution approval
name the exact model, API version, endpoint, non-production project, credential
reference, manifest digest, attempt cap, wall-clock cap, timeout, cost cap, canonical
Live setup digest, and successor runner/evaluator SHA. That successor may then run
the bounded purpose-recorded audio matrix. Put aggregate redacted evidence and the
finalized go/no-go ADR on a separate evidence branch and PR from current `main`.
Tasks 1-4 remain prohibited until that evidence PR receives staff/security review,
passes CI, and merges. If any pre-registered value drifts or Gate 0 fails, stop this
plan and record a no-go ADR.

### Task 1: Add canonical observation contracts

**Create:**

- `app/services/receptionist_observation.py`
- `tests/unit/test_receptionist_observation.py`

**Tests first:** one generated schema source, exact context allowlist, no generic
state serialization, bounds, language semantics, every semantic rejection,
indistinguishable-history collision pairs for pending confirmations, deterministic
acceptance of pending callback confirmation without repeated digits, rejection when
pending or existing-number context is absent, grounded last-four acceptance,
ungrounded last-four rejection, deterministic envelopes, raw-data exclusion, and
nonauthorization metadata.

### Task 2: Build semantic fixtures and evaluator

**Create:**

- `tests/fixtures/receptionist_observations/manifest.json`
- `tests/fixtures/receptionist_observations/development.json`
- `tests/fixtures/receptionist_observations/holdout.json`
- `scripts/evaluate_receptionist_observations.py`
- `tests/unit/test_receptionist_observation_eval.py`

**Tests first:** schema/provenance/split validation, duplicate detection, minimum
strata, all deterministic metrics, multi-turn reducer/planner outcomes, sealed
holdout behavior, and transcript-free reports.

### Task 3: Add the isolated Gemini candidate adapter

**Create:**

- `app/services/gemini_observation_extractor.py`
- `tests/unit/test_gemini_observation_extractor.py`

Prefer existing `httpx`. Tests capture the full outbound request and prove header
authentication, `store=false`, context allowlist, forbidden feature absence,
deadline, cancellation, no retry, bounded errors, credential redaction, response
discard, and zero network activity at import.

Do not import this module from `app/services/gemini_pipeline.py` or
`app/services/voice_pipeline.py`.

### Task 4: Add a dry-run-first qualification command

**Create:**

- `scripts/qualify_receptionist_observation_models.py`
- `tests/unit/test_qualify_receptionist_observation_models.py`
- `docs/receptionist-observation-qualification.md`

The default invocation performs zero requests. Execution requires an explicit
synthetic-only flag, exact candidate list, paid-project confirmation, attempt cap,
and external report path. Tests prove no real-data override, deterministic
counterbalancing, bounded interrupted runs, no fixture overwrite, no-winner support,
and fixed report gates.

### Gate A: Offline extraction review

Stop after Tasks 1-4 with only mocked transport and dry-run evidence. Require:

- focused and full unit suites pass;
- Ruff, Bandit, secret, PII, provenance, and `git diff --check` pass;
- source inspection proves neither live pipeline imports new modules;
- live pipeline files are unchanged from the immutable reviewed base SHA;
- no provider call occurred;
- staff/security review approves the exact commit.

### Gate B: Synthetic extraction provider approval

Only after separate user approval, execute the bounded candidate matrix. A panel
reviews the redacted report and chooses a passing candidate or no winner. No runtime
work follows automatically.

## 8. Future Runtime Preconditions, Not Authorized Tasks

If and only if Gate 0 and Gate B pass, write a new plan before building a worker or
editing live code. That future plan must include:

- retrospective-only claims and a separate ADR for any pre-response control path;
- staging code hard-deny and verified staging service identity;
- no observation credential mounted in production and deployment config forcing
  the feature false;
- audited admin enablement with reason, exact SHA, contractor, environment, expiry,
  before/after state, and test-caller ingress allowlist;
- dynamic fail-closed kill switch with a defined propagation SLA that stops new and
  cancels queued/in-flight work;
- explicit transcript/turn/request byte limits, turns per call, requests per
  contractor, total active workers, process-wide backlog, quota behavior, and daily
  spend shutdown;
- a dedicated aggregated metrics sink that cannot inherit ambient `call_sid`, emits
  no per-call/contractor/turn ID or exception text, defines IAM/retention/cardinality,
  and isolates sink failure from voice execution;
- removal/redaction of the existing urgency log that includes full caller text
  before any correlated runtime experiment;
- paired counterbalanced shadow-off/on synthetic calls at pre-registered concurrency
  with caller-heard measurements for speech-end-to-first-audio, barge-in-to-audio
  stop, audio gaps/dropouts, talk-over, completion, event-loop lag, and kill-switch
  timing;
- pre-registered noninferiority margins, sample sizes, attempts, and measurements at
  Twilio/media egress rather than internal queue timestamps;
- separate approvals for code review, merge, staging deploy, flag enablement,
  production, and active control.

Runtime failure or inconclusive UX evidence blocks progression. Retrospective shadow
metrics can measure contention, lifecycle, ordering, and kill-switch operation. They
cannot prove multilingual semantics, same-turn control, repeated-question prevention,
or caller-experience improvement.

## 9. PR Ownership and Order

1. This plan and the original Gate 0A-0C artifacts are already present on baseline
   `218822f`. Begin with the SHA-bound audit matrix, not a historical creation PR.
2. If the audit identifies a gap, create one new fresh-main branch containing only
   the documented offline gap closure and rerun Gate 0A. If no gap exists, create
   no implementation PR.
3. Gate 0B cannot run from the current hard-disabled command. Only after a reviewed
   successor plan and implementation merge from fresh `main` may a separate explicit
   authorization bind that successor's exact source SHA and preregistration. Put only
   aggregate redacted evidence and the finalized ADR on a new evidence PR from
   current `main`.
4. Tasks 1-4 remain prohibited until the Gate 0B evidence/ADR PR passes review and
   CI and records a go decision. Then use a new fresh-main branch.
5. Any worker/runtime implementation requires a new reviewed plan and new branch.

Each successor starts from fresh `main` after its predecessor merges. Before every
PR, record the exact base and head SHA. Use that immutable base for isolation checks,
then perform a fresh-main drift check before review.

PRs #76, #87, and #90 remain untouched by this plan.

## 10. Verification Commands

Commands are executed as their files become available:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_caller_turns.py tests/unit/test_gemini_turn_events.py tests/unit/test_caller_turn_assembly_eval.py -q
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_qualify_gemini_caller_turn_assembly.py -q
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_observation.py tests/unit/test_receptionist_observation_eval.py tests/unit/test_gemini_observation_extractor.py tests/unit/test_qualify_receptionist_observation_models.py -q
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_state.py tests/unit/test_dialogue_planner.py tests/unit/test_instruction_composer.py tests/unit/test_receptionist_replay.py tests/unit/test_receptionist_intelligence.py -q
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit -q
uv run --python 3.12 --with '.[dev]' ruff check app tests scripts
git diff --check
```

At every offline gate:

```bash
git diff <immutable-reviewed-base-sha> -- app/services/gemini_pipeline.py app/services/voice_pipeline.py
rg -n "caller_turns|gemini_turn_events|receptionist_observation|gemini_observation_extractor" app/services/gemini_pipeline.py app/services/voice_pipeline.py
```

Expected: no pipeline diff and no imports. Security verification also uses the
repository's established Bandit/secret commands and a targeted source, fixture,
report, and docs scan for keys, tokens, OAuth codes, full phone numbers, addresses,
caller names, call SIDs, real transcripts, and unlicensed audio.

## 11. Stop Conditions

Stop and return to architecture review when:

- no bounded retrospective turn policy passes Gate 0;
- cross-turn contamination or unclassifiable late fragments occur;
- a candidate cannot meet minimum language, safety, state, or planner gates;
- structured-output, retention, or `store=false` behavior changes;
- any step requires real caller data or synchronous Live function calls;
- a prompt, transcript, audio, raw output, exception, ID, or credential reaches a
  report or log;
- tests cannot prove live-pipeline isolation;
- passing evidence would require moving thresholds or editing the holdout;
- a result is used to claim current-turn control or caller-visible improvement.

## 12. Explicitly Deferred

- Runtime shadow worker and live Gemini integration.
- Active controller instructions and controller-owned tool eligibility.
- Typed assistant-turn observation and actual-versus-proposed comparison.
- Durable customer memory and PR #76 decisions.
- Gemini Live model promotion or voice-pipeline consolidation.
- ElevenLabs/Deepgram changes.
- Staging flags, real caller data, production credentials, deployment, and release.

Every deferred item requires a separate evidence-backed plan and approval boundary.
