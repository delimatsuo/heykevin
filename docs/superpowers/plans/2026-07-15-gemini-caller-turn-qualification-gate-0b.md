# Gemini Caller-Turn Qualification Gate 0B Plan

> **Status:** Revised after panel review; pending final panel confirmation. This
> revision authorizes documentation only.
> Offline implementation starts only after panel approval. It does not authorize a
> Gemini request, credential creation, corpus collection, staging, production,
> deployment, runtime wiring, or release.

**Date:** 2026-07-15

**Base:** `d7969acfd17018028c3aed86cc2733deffa9b1f7`

**Branch:** `codex/gemini-caller-turn-qualification-gate-0b`

**Predecessor:** PR #108, offline Gate 0A caller-turn assembly

## 1. Objective And Decision Boundary

Build a separately reviewed, fail-closed successor that can collect empirical
evidence from synthetic scripts spoken as purpose-recorded, non-customer test audio,
measure Gemini Live input-transcription ordering and fidelity, and evaluate the
merged retrospective caller-turn assembler against the Gate 0 thresholds.

This branch stops after the executor, corpus contract, evaluator, dry-run evidence,
and preregistration mechanism pass offline review and merge. Provider execution is a
later operation from the exact merged SHA and requires a second approval naming the
complete preregistration digest and every bounded execution value.

Gate 0B answers only:

> For one exact Gemini Live model and setup, does a bounded retrospective
> quiescence policy assemble purpose-recorded caller activities without cross-turn
> contamination, while transcription fidelity and wire-observable provider
> interaction integrity also satisfy their preregistered sample gates?

It does not authorize or prove:

- same-turn control or repeated-question prevention;
- semantic observation extraction;
- a production model migration;
- live-pipeline imports or runtime/shadow wiring;
- real caller data, customer audio, or contractor context;
- acoustic quality, semantic response quality, or caller-experience neutrality;
- staging, production, feature flags, deployment, or release.

## 2. Current Source Of Truth

### 2.1 Merged Gate 0A

PR #108 merged these offline-only surfaces:

- `app/services/caller_turns.py`
- `app/services/gemini_turn_events.py`
- `scripts/evaluate_caller_turn_assembly.py`
- `scripts/qualify_gemini_caller_turn_assembly.py`
- `tests/fixtures/caller_turn_audio/manifest.json`
- focused tests, ADR, and runbook

The Gate 0A command remains permanently non-executing. Gate 0B must use a new
command and must not turn the old `--execute` flag into a provider path.

### 2.2 Existing reusable replay primitives

`app/services/voice_turn_replay.py` already provides deterministic Twilio codec
rendering, bounded PCM validation, paced frame schedules, paired arm ordering, and
payload-safe aggregate evaluation. Gate 0B may reuse or narrowly extend those pure
primitives.

It must not promote `scripts/benchmark_gemini_turn_detection.py` into a
qualification runner. That command is intentionally `cold_single_turn`, uses a
small public smoke corpus, emits diagnostic-only evidence, and does not measure
transcription assembly, consecutive activities, fresh-connection restart isolation,
or selected quiescence policies.

### 2.3 Current live runtime remains immutable

The live pipeline still defaults to `gemini-2.5-flash-native-audio-latest`. Gate 0B
does not edit or import from `app/services/gemini_pipeline.py` or
`app/services/voice_pipeline.py`. A passing result for another exact model is
model-specific research evidence, not permission to change the runtime model.

## 3. Current Official Gemini Constraints

The implementation review must recheck these official sources because Live models,
preview behavior, and pricing can change:

- <https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview>
- <https://ai.google.dev/gemini-api/docs/live-api/capabilities>
- <https://ai.google.dev/gemini-api/docs/live-api/best-practices>
- <https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket>
- <https://ai.google.dev/gemini-api/docs/live-api/ephemeral-tokens>
- <https://ai.google.dev/api/live>
- <https://ai.google.dev/gemini-api/docs/pricing>
- <https://ai.google.dev/gemini-api/docs/usage-policies>
- <https://ai.google.dev/gemini-api/docs/zdr>
- <https://ai.google.dev/gemini-api/terms>
- <https://ai.google.dev/gemini-api/docs/changelog>

As of this plan date:

- `gemini-3.1-flash-live-preview` is the current low-latency Live model and remains
  Preview, so Gate 0B can qualify it only for research evidence;
- Gemini 3.1 uses `thinkingLevel`, not the Gemini 2.5 `thinkingBudget` field;
- one server event can contain multiple content parts, so every part and terminal
  field in an event must be processed without early return;
- input transcription is independent of other server messages and has no guaranteed
  ordering;
- input audio is raw little-endian PCM16, natively 16 kHz;
- audio should be paced in small chunks, with 20-40 ms as the normal test range;
- automatic VAD and explicit activity start/end are supported;
- Gemini 3.1 tool calls are synchronous;
- `usageMetadata` exposes total and modality token counts;
- Live billing is token-based and prior session context can be billed again;
- the raw WebSocket guide currently documents the API key in the URL query string.

The last point conflicts with the older header-only assumption. Gate 0B uses the
documented raw `v1beta` WebSocket transport because the experiment must preserve the
exact wire-level event ordering consumed by the existing adapter. This is an explicit
security exception, not a reusable application transport. The authenticated URL is
constructed only inside the connector from a dedicated paid-project,
API-restricted qualification key. The connector allowlists the exact TLS host,
disables redirects, proxy environment use, APM/WebSocket tracing, crash reporting,
TLS key logging, and library debug output, and never exposes a URL-bearing object.
Event-loop, uncaught-task, library, and connector exceptions become bounded URI-free
codes. Canary-secret tests cover stdout, stderr, structured logs, task exceptions,
crash handlers, snapshots, and every report writer. The preregistration binds the
project number, key-resource identity, quota, query-free canonical endpoint, and
activation time; campaign completion, expiry, or abort requires immediate key
revocation evidence. A future ephemeral-token or `v1alpha` variant is a different
protocol requiring a different plan and approval.

## 4. Qualification Architecture

```text
External consented corpus directory (never committed)
  -> consent, manifest, PCM, and provenance validators
  -> DEVELOPMENT_COLLECTION
  -> independent in-memory adapter/measurement agreement
  -> immutable policy-selection digest
  -> POLICY_SELECTION_LOCKED
  -> custodian releases session-disjoint holdout assets
  -> HOLDOUT_COLLECTION using only the selected policy
  -> encrypted audit capsule + payload-safe primitive records
  -> independent evaluator recomputes all sample gates
  -> COMPLETED, ABORTED, or INVALIDATED campaign
  -> go/no-go ADR evidence
```

Ownership boundaries:

- corpus validation and audio rendering contain no network client;
- connector construction contains no evaluator or report-writing policy;
- raw provider messages remain in memory only and are reduced independently by two
  allowlisted implementations before disposal;
- a short-lived encrypted audit capsule stores only canonical references and the
  allowlisted adapted transcript-event fields needed for post-run recomputation;
- fixed-schema primitive records contain no text and are independently derived from
  the audit capsule rather than trusted from the executor;
- the evaluator recomputes metric algebra, aggregates, and every gate from the audit
  capsule and primitive records; it never trusts runner verdicts;
- no component imports the live pipelines;
- the approval verifier runs before credentials are read or a connector is created.

## 5. Exact Candidate And Setup Policy

The initial candidate is:

```text
model: gemini-3.1-flash-live-preview
api_version: v1beta
response_modality: AUDIO
thinking_level: minimal
temperature: 0.4
voice: Puck
input_audio_transcription: enabled
output_audio_transcription: enabled
automatic_vad.start_sensitivity: high
automatic_vad.end_sensitivity: high
automatic_vad.prefix_padding_ms: 100
automatic_vad.silence_duration_ms: 500
activity_handling: START_OF_ACTIVITY_INTERRUPTS
turn_coverage: TURN_INCLUDES_ONLY_ACTIVITY
tools: synthetic no-op declarations only for tool scenarios
tool_response: synchronous synthetic response, no external side effect
context_restoration: none
session_resumption: disabled
grounding/search/cache/proactive_audio/affective_dialogue: disabled
```

No `latest` alias is allowed. The exact setup JSON, official endpoint, model code,
transport type, prompt digest, tool digest, VAD values, voice, thinking level,
transcription flags, turn coverage, WebSocket limits, and every deviation from the
immutable live setup are included in the preregistration.

If the exact model is removed, changed, or superseded before execution, stop. Do not
substitute a model under the existing approval.

The Gemini 2.5 production alias is not a Gate 0B comparison arm. Existing 2.5 smoke
evidence remains nonqualifying. Any production model migration requires a separate
plan after Gate 0B.

## 6. Corpus And Privacy Contract

### 6.1 Storage boundary

Purpose-recorded human voice is identifiable test data with potentially biometric
characteristics even when the spoken script contains no PII. Voiceprint extraction,
speaker identification, customer audio, and real-call provenance are prohibited.
Audio, consent records, provider transcript text, and subject mappings are not
committed to Git.

They live in separate access-restricted, encrypted asset, consent-registry, holdout,
audit-capsule, and report locations. Each location has a named owner, exact ACL,
encryption identity, access audit, retention deadline, deletion procedure, and
deletion attestation. The exact local retention period is preregistered and cannot
extend beyond 30 days after the evidence panel. Provider-retained data is governed by
a separate ZDR attestation or an explicit residual-retention acceptance naming the
privacy owner and exact execution approval; local deletion never claims to delete
provider records.

The signed, versioned consent registry is separately controlled from the corpus. It
binds a corpus-scoped subject ID to consent version, adult attestation, recording
date, purpose, voice and codec transformations, transmission to Google, provider
model/project, geographic and residual-retention disclosure, possible abuse review,
evidence use, withdrawal state, rights, and deletion deadline. The corpus validator
checks current consent and withdrawal immediately before every attempt. Withdrawal
invalidates the corpus digest, preregistration, and approval; the runner cannot
silently remove or replace a subject.

Execution requires a paid, non-production Gemini project whose data-use and
retention settings have been verified. A holdout custodian controls holdout assets
and subject mappings and independently attests split disjointness before releasing
holdout only after the policy-selection digest is durable.

The repository contains only:

- schema examples with invented digests and no audio;
- synthetic unit fixtures;
- the pending corpus contract;
- aggregate, payload-safe evidence after an approved run.

### 6.2 Namespaced Gate 0B corpus schema v1

The schema ID is `gate_0b_corpus_v1`; it does not reuse the existing voice-replay
manifest version numbers. The successor manifest groups activities into sessions so
cross-turn contamination
and fresh-connection restart isolation are measurable. Every session and activity
has a safe local ordinal, never a participant name, phone, caller ID, contractor ID,
or provider ID.

Required corpus properties:

- exactly 256 eligible caller activities;
- exactly eight preregistered language strata: English, Spanish, Portuguese, French,
  Mandarin, Hindi, Arabic, and one named lower-resource language;
- exactly 32 activities per language: 16 development and 16 holdout;
- at least four consenting adult human speakers per language, with at least two in
  each split and no subject or session crossing splits;
- TTS may augment but not replace human coverage;
- exactly 128 custodian-sealed holdout activities;
- subject-disjoint and session-disjoint development and holdout splits;
- at least two consecutive activities in every contamination test session;
- in every language and split, four primary-condition activities each for clean,
  Twilio-codec-only, acoustic impairment, and interaction stress;
- exact preregistered stress cells for jitter/packet loss, clipping, echo/crosstalk,
  far-field/low volume, background noise, long pauses, fast speech, correction,
  number dictation, synchronous tool use, tool cancellation/interruption, and
  fresh-connection restart, with at least eight activities per split per stress and
  at least four represented languages where the stress is language-independent;
- for every non-English language and split, at least one English-to-language and one
  language-to-English code-switch activity with script-authored critical spans;
- exactly 64 separately scheduled no-speech/noise windows, 32 per split, excluded
  from eligible-activity denominators and used only for false-activation rates;
- unique audio digests and bounded duration/size per activity;
- synthetic script digest, script provenance, consent-record digest, usage rights,
  language, corpus-scoped subject pseudonym, condition, scenario, session ordinal,
  activity ordinal, and split for every activity.

The lower-resource language is fixed before recording. It cannot be selected after
seeing provider results. The preregistration contains the exact allocation table,
observed-rate denominators, and exact Clopper-Pearson 95% interval method. These
sample sizes support only a literal, time-bounded sample result; they cannot establish
99% population reliability, broad multilingual equity, accessibility, or excluded
speech-population performance. Sensitive health/disability labels are not collected
to manufacture coverage; any excluded population is named as a limitation.

### 6.3 Actual audio validation

The validator does more than trust a codec label. It rejects unless:

- the path remains inside the external corpus root and is a regular file;
- the file digest matches the manifest;
- PCM byte length is even and matches the declared duration and sample rate;
- samples are signed little-endian 16-bit mono;
- duration, silence ratio, peak amplitude, and clipping ratio are within fixed bounds;
- labeled speech boundaries are valid and inside the file;
- the deterministic PCM -> mulaw 8 kHz -> PCM16 16 kHz render digest matches the
  preregistered derived digest;
- development/holdout and subject-disjointness checks pass;
- consent, withdrawal, paid-project, retention, and custodian attestations pass;
- no real-call, production, caller, or customer provenance label appears.

Published reports never include a subject dimension or speaker-level statistic.
Cross-tabs and cells with fewer than eight observations are suppressed only in
published output; unsuppressed internal failures still veto the gate.

## 7. Ground Truth And Quiescence Selection

The provider has no transcript-final marker. Ground truth therefore comes from the
known synthetic activity schedule plus script-authored references, not provider
terminal flags.

The network executor computes no verdict. Two independently implemented in-memory
reducers must agree on raw-message-to-adapted-event reduction before raw messages are
discarded. An allowlisted encrypted audit capsule then records campaign-local
ordinals, relative receipt times, event kinds, epoch/terminal facts, canonical script
references, and adapted transcript fragments. It excludes provider IDs, paths,
credentials, request/session IDs, tool payloads, and every unapproved raw field. A
separate custodian holds the capsule key. The evidence panel deletes the capsule and
key after recomputation and records a deletion attestation.

A deterministic, versioned aligner maps each normalized fragment to one activity
reference for attribution only. It never rewrites, merges, deduplicates, or reorders
the event stream supplied to `CallerTurnAssembler`. Normalization, Unicode version,
language-specific segmentation, cumulative-versus-delta behavior, edit operations,
ambiguity margin, critical spans, CER thresholds, and WER thresholds where meaningful
are preregistered before development collection. It has no model or embedding judge.

Assignment and recognition fidelity are separate outcomes. Low-fidelity, ambiguous,
or unassigned output remains in every relevant denominator and cannot satisfy either
assembly or fidelity. Corrupted-but-nearest transcripts must fail. Script-authored
critical spans cover digits, negation, correction, identity-like confusables, and
each code-switch direction.

Pre-register a small bounded quiescence set, initially:

```text
100 ms, 250 ms, 500 ms, 750 ms
```

The campaign uses an atomic, forward-only phase machine:

```text
PREREGISTERED
  -> DEVELOPMENT_COLLECTION
  -> POLICY_SELECTION_LOCKED
  -> HOLDOUT_COLLECTION
  -> COMPLETED
```

Every attempt has its own forward-only record. A preregistered infrastructure outage
before `POLICY_SELECTION_LOCKED`, before any holdout materialization/access, and
before any holdout provider request records terminal `ATTEMPT_ABORTED` while leaving
the campaign in `DEVELOPMENT_COLLECTION`; a new signed attempt may restart
development within the three-attempt campaign cap. Every other failure moves the
campaign to terminal `ABORTED` or `INVALIDATED`; no backward transition, reset, or
reuse is allowed. After policy lock, any failure ends the campaign and a replacement
requires a new sealed holdout, preregistration, campaign, and user approval.

Development event streams are replayed through every candidate policy. The
deterministic selection rule chooses the lowest-latency policy satisfying all
development safety gates. The policy-lock digest binds the complete
development audit/primitive-record root, evaluator/source/schema/environment
identities, metric definitions, thresholds, selection algorithm, and selected
policy. Only then may the custodian materialize holdout assets. Holdout sessions run
exactly once through only the selected policy. Any non-selected-policy holdout fact,
development/holdout session overlap, or pre-lock holdout access invalidates the
campaign.

If the reducers, aligner, fidelity metrics, encrypted audit path, or independent
recomputation cannot be validated on multilingual synthetic fixtures, Gate 0B stops
before any provider call.

## 8. Evidence Artifact And Independent Evaluation

The runner writes an encrypted, access-restricted fixed-schema primitive record for
every scheduled activity and no-speech window outside the repo, including missing or
dropped outcomes. Primitive records contain campaign-local randomized ordinals,
bounded numeric fields, fixed enums, lengths, edit-operation counts, derived
CER/WER, ambiguity margin, critical-span outcomes, lifecycle facts, timing bins, and
domain-separated campaign-keyed HMAC commitments. They contain no text, audio,
prompt, tool arguments, exception text, credential, subject identifier, file path,
provider request ID, or provider session ID. HMACs provide binding and tamper
evidence only; they do not prove fidelity calculations.

The independent measurement component recomputes primitive records from the audit
capsule. The evaluator recomputes metric algebra, aggregates, phase consistency, and
all gates from those records. The complete fixed-cardinality record set is bound by
a signed Merkle root. Published evidence contains only suppressible aggregates and
the signed root; primitive records and the audit capsule follow the asset deletion
deadline.

It includes:

- schema version and complete preregistration block;
- source SHA and SHA-256 for every executable dependency;
- execution timestamps, `provider_revision` when exposed or explicit `null`, and a
  digest-bound source-fact bundle containing canonical official URLs, retrieval
  times, normalized relevant facts, and ETag/Last-Modified values when available;
- manifest, corpus, setup, pricing, prompt, tool, runner, adapter, assembler,
  renderer, and evaluator digests;
- campaign, attempt, phase-transition, logical-session, connection, epoch, activity,
  no-speech-window, fresh-restart, and provider-request counts;
- selected and candidate quiescence policies;
- count and bounded timing histograms by split, language, scenario, and condition;
- assignment, missing, duplicate, contamination, late-fragment, partial, cancelled,
  dropped, malformed, stale, teardown, and resource outcomes;
- usage token counts by modality and pinned-price cost totals;
- bounded provider-close/error enums;
- observed rates, exact numerators/denominators, and Clopper-Pearson intervals;
- every evidence-taxonomy boolean, defaulting false.

The evidence separates:

- `attempt_authorization_validated`;
- `authorization_consumed`;
- `provider_execution_started`;
- `attempt_completed`;
- `assembly_sample_passed`;
- `transcription_fidelity_sample_passed`;
- `provider_interaction_integrity_sample_passed`;
- `gate_0b_sample_passed`, true only when all three sample gates pass.

The composite is bound to the exact model, setup, corpus, policy digest, source,
environment, and execution window. `future_execution_authorized`,
`enterprise_readiness_validated`, `accessibility_validated`,
`broad_multilingual_support_validated`, `semantic_extraction_validated`,
`caller_experience_neutrality_validated`, `model_migration_authorized`,
`runtime_wiring_authorized`, `controller_work_authorized`, `staging_authorized`,
`deployment_authorized`, `production_authorized`, and `release_authorized` always
remain false.

### 8.1 Wire-observable interaction integrity

Gate 0B does not emulate a 24 kHz client playout queue and does not measure Twilio or
ElevenLabs. It does enforce a narrow provider-stream veto so an assembly pass cannot
hide obviously unusable interaction behavior. The measurement clock is a
digest-bound monotonic implementation; output audio is counted from chunk lengths
and receipt times and immediately discarded.

The preregistered sample gate requires:

- 100% timing coverage for every eligible automatic-VAD activity;
- speech-end-to-first-received-audio p95 at most 1,500 ms and maximum at most
  2,500 ms;
- zero current-activity response-audio chunks before that caller activity ends;
- zero model-audio or false-activity responses in the 64 no-speech/noise windows;
- prior-turn interruption/new-activity-to-last-received-audio p95 at most 250 ms and
  maximum at most 500 ms;
- zero audio after teardown or a terminal cancellation;
- zero missing interruption/cancellation/terminal classifications;
- zero abnormal closes, runaway output, response timeout, or response gap over the
  preregistered 500 ms bound.

Output-turn attribution is deterministic. A local response ordinal opens on the
first model-content/audio event after the preceding response terminal and closes on
`turnComplete`, `interrupted`, cancellation, or teardown. Audio already associated
with the open prior response remains prior-turn audio after a new caller activity
starts; the first response opened for that new activity is current-activity audio.
Overlapping response ordinals, audio without an attributable response, or a terminal
that cannot close exactly one ordinal are malformed and fail the gate.

First-audio and interruption measurements are receipt-time provider proxies, not
caller-heard SLOs. Queue discard, acoustic overlap, real barge-in-to-audio-stop,
audio quality, Twilio egress, ElevenLabs, and caller experience require a later Gate
0C plan and remain unvalidated.

### 8.2 Assembly and fidelity sample gates

The approval digest binds these inherited assembly gates and the exact preregistered
normalization/fidelity configuration. On development and then separately on holdout:

- at least 99% of eligible activities produce exactly one assembled caller turn,
  both overall and within every language stratum;
- contamination, duplicate assembly, cross-epoch acceptance, and late-fragment
  mutation are exactly zero;
- terminal, partial, cancelled, stale, malformed, dropped, teardown, and fresh-
  restart lifecycle classification is correct for 100% of scheduled cases;
- missing, low-fidelity, ambiguous, and unassigned activities remain in the
  denominator and cannot be reclassified away;
- character error rate is at most 5% overall and at most 10% per language and primary
  condition;
- for languages with preregistered deterministic word segmentation, word error rate
  is at most 10% overall and at most 15% per language;
- digits, negation, correction, identity-like confusables, and code-switch critical
  spans have 100% exact fidelity;
- both code-switch directions meet the per-language CER gate; and
- any malformed, partial, interrupted, inconsistent, or missing evidence fails
  closed.

These are observed sample gates. Exact numerators, denominators, and confidence
intervals must be reported; the thresholds do not create a population-level claim.

The evaluator validates schema, signatures, Merkle/HMAC bindings, identities,
phase transitions, totals, strata, histogram bounds, and cross-field consistency
before recomputing every gate. It does not trust runner `pass`, eligibility,
selection, authorization, or metric fields.

A partial, interrupted, inconsistent, over-budget, over-time, missing-usage, or
provider-error run is nonauthorizing and produces a no-go result.

## 9. Source Identity And Clean-Tree Enforcement

Before preregistration and again immediately before credential lookup and connector
construction, reject unless:

- `HEAD` equals the approved source SHA;
- the worktree has no staged, unstaged, or untracked files;
- the repository root and every executable dependency resolve inside the approved
  worktree;
- every code-owned executable dependency matches its preregistered SHA-256;
- the committed Git blob for each dependency matches the worktree bytes.

Execution uses a checked-in hash-bearing `uv.lock`, Python `3.12.13`, `uv 0.11.7`,
frozen/no-sync execution, and an immutable container/interpreter image digest. No
dependency resolution, installation, or
environment mutation may occur after an attempt is claimed. The preregistration
binds exact Python patch version, `uv` version, OS/platform/architecture, Unicode data
version, monotonic clock implementation and resolution, OpenSSL, `websockets`, CA
bundle, installed distribution names/versions/hashes, verified import origins, and
audio-codec golden digests. The executor records the same identities before and after
the run; drift invalidates evidence.

At minimum, bind:

- the new runner and evaluator;
- `app/services/caller_turns.py`;
- `app/services/gemini_turn_events.py`;
- reused `app/services/voice_turn_replay.py` primitives;
- `app/utils/audio.py` and every imported project-owned transitive module;
- corpus validator and aligner;
- setup/prompt/tool/pricing fixtures;
- `app/services/gemini_pipeline.py`, `app/services/voice_pipeline.py`, and
  `app/config.py` as immutable comparison inputs.

Any mutation, untracked Python module, import-path escape, symlink escape, digest,
lock, import-origin, container, interpreter, clock, CA, codec, or base/head drift
blocks execution.

## 10. Approval Artifact And Credential Boundary

The implementation PR merges before a runnable approval is generated.

From a new clean detached worktree at the merged SHA:

1. Materialize and validate the external corpus.
2. Generate a dry-run preregistration JSON outside the repository.
3. Compute its canonical SHA-256.
4. Run staff and security review of the exact merged SHA, corpus summary, transport,
   caps, and preregistration.
5. Ask the user to approve the exact preregistration digest and values.
6. The approval custodian, whose signing key is unavailable to the executor, creates
   a detached, short-lived signed campaign approval plus one signed attempt
   authorization outside the repository.

The approval contains only:

- approval schema version, scope `gate_0b_purpose_recorded_turn_assembly`, pinned
  issuer public-key identity, `campaign_id`, immutable `authorization_id`, nonce, and
  maximum three separately authorized attempt IDs;
- exact preregistration SHA-256 and source SHA;
- exact model, API version, endpoint/transport, non-production project label, and
  dedicated credential reference;
- manifest/corpus/setup/pricing/runner/evaluator digests;
- whole-run, session, activity, fresh-restart, provider-request per-run and campaign,
  timeout, wall-clock, audio-duration, and cost caps;
- approval timestamp and expiry no later than 24 hours;
- exact consent/retention/ZDR-or-residual-retention acceptance identities;
- every nonauthorization field from Section 8 set false.

The executor accepts no CLI or environment override for approved values. It verifies
the asymmetric signature against the pinned issuer key, validates campaign and
attempt identity, and atomically consumes the attempt before credential lookup or
provider DNS. The attempt is consumed even if the process crashes, setup fails, or no
provider request succeeds.

The campaign uses a single-host, access-restricted, durable ledger outside the repo
with an OS-exclusive lease, atomic transaction, fsync, hash chain, custodian
signature, and append-only attempt records. It reserves worst-case request and cost
allowance before execution, prevents concurrent campaign use, reconciles actual
usage, and never automatically recovers an expired lease. A replacement requires a
new signed attempt authorization that names the previous attempt and one exact
preregistered infrastructure-outage enum. Replacement is permitted only while the
campaign remains in `DEVELOPMENT_COLLECTION` and the ledger proves that no holdout
asset was materialized/accessed and no holdout provider request was claimed. Ledger
access is local and performs no network operation; “before DNS” means before any
provider/control-plane DNS.

A digest/signature mismatch, expiry, consumed attempt, lease conflict, missing field,
insufficient reserved allowance, or ledger inconsistency blocks before credential
lookup, DNS, or connector construction.

The credential is dedicated to the qualification project, restricted to the Gemini
API, absent from live services, revocable, and supplied through the approved secret
delivery path. Never reuse or print `GEMINI_API_KEY`, a live-call key, an admin token,
or a production service credential.

## 11. Attempt, Time, And Cost Semantics

Use distinct terms and hard ceilings:

- `whole_run_attempts`: maximum 3; normally 1;
- `sessions_per_run`: maximum 64;
- `eligible_activities_per_run`: exactly 256;
- `no_speech_windows_per_run`: exactly 64;
- `activities_per_session`: maximum 10;
- `fresh_connection_restarts_per_session`: maximum 1;
- `provider_requests_per_run`: maximum 128, derived from the sealed schedule;
- `provider_requests_across_approved_attempts`: maximum 384;
- `session_timeout_seconds`: maximum 120;
- `whole_run_wall_clock_seconds`: maximum 3,600;
- `input_audio_seconds_per_run`: maximum 3,600;
- `output_audio_seconds_per_run`: maximum 1,800;
- `cost_usd_per_session`: maximum 0.25;
- `cost_usd_per_whole_run`: maximum 10.00;
- `cost_usd_across_approved_attempts`: maximum 30.00.

The approval may set lower values but never higher ones except that exact corpus and
no-speech counts cannot change. A request allowance is consumed immediately before
connector construction. No retry occurs for a case, session, provider error, setup
rejection, timeout, malformed response, or failed gate. A whole-run replacement is
allowed only for a preregistered infrastructure-outage enum before policy lock and
before any holdout materialization, access, or provider request. It requires a new
signed attempt authorization, retains the failed aggregate artifact, and consumes
one of the three campaign attempts. Any failure at or after policy lock terminates
the campaign; another run requires a new sealed holdout, preregistration, campaign,
and user approval. Lease recovery is an audited, fail-closed custodian operation,
never an automatic retry.

Cost is computed from provider `usageMetadata` modality counts and a versioned,
digest-bound pricing artifact based on the official pricing page. Missing or
internally inconsistent usage metadata blocks the run. Audio-duration estimates are
an additional pre-send ceiling, not a substitute for billed-token accounting. The
ledger reserves USD 10 and 128 requests before each attempt and reconciles actual
usage without releasing the consumed attempt.

Terminology is exact: a logical session is one corpus conversation; a connection is
one WebSocket/Bidi request; an epoch is the assembler connection generation; and a
fresh-connection restart creates a new provider session/request while retaining only
the local logical-session identity needed to test stale-fragment isolation. With
session resumption disabled, no restart is called provider-context continuity. Any
activity crossing the boundary is classified fail-closed and every late prior-epoch
event is stale.

## 12. Task-Level TDD Plan

Each task records RED, implements the minimum change, records GREEN, runs Ruff on
touched Python, and creates one scoped commit. No provider call occurs in Tasks 1-8.

### Task 1: Add Gate 0B contracts and schema

**Create:**

- `app/services/caller_turn_qualification.py`
- `tests/unit/test_caller_turn_qualification.py`
- `tests/fixtures/caller_turn_qualification/gate-0b-corpus-v1.example.json`
- `tests/fixtures/caller_turn_qualification/pricing.json`
- `uv.lock`

**Tests first:** strict manifest/session/activity schema, bounded safe identifiers,
subject/session-disjoint splits, exact 256/128/32/16 strata and 64 no-speech windows,
condition/scenario allocation, consent/withdrawal/retention attestations, actual PCM
validation, derived codec digest, pricing schema, phase/evidence/audit/primitive
schemas, unknown-field rejection, and payload-free published serialization.

### Task 2: Add clean source and approval identity

**Create:**

- `app/services/qualification_identity.py`
- `scripts/verify_qualification_environment.py`
- `tests/unit/test_qualification_identity.py`

**Tests first:** staged/unstaged/untracked rejection, detached and linked worktrees,
source SHA mismatch, Git blob mismatch, executable dependency mutation, untracked
module shadowing, symlink/path escape, frozen lock/import/container/interpreter/clock/
CA/codec identity, asymmetric approval signature and trust root, expiry, nonce,
one-use attempt consumption, exclusive lease, atomic request/cost reservation,
crash-consumed attempt, tamper-evident ledger, replacement linkage, changed
digest/value, pre-lock-only replacement, post-lock/holdout replacement rejection,
exact Python/uv environment identity, and proof that failure occurs before
credential, DNS, or connector access.

### Task 3: Reuse and extend deterministic audio replay

**Modify:**

- `app/services/voice_turn_replay.py`
- `tests/unit/test_voice_turn_replay.py`

**Tests first:** namespaced Gate 0B session rendering, 20/30/40 ms pacing, Twilio
mulaw round-trip digest, timing variation without payload mutation, labeled activity
boundaries, consecutive activities, no-speech windows, interaction scheduling,
fresh-connection restart boundaries, and resource bounds.

Keep existing smoke behavior and hashes backward compatible unless an explicit test
proves the change is intentionally versioned.

### Task 4: Add deterministic multilingual alignment

**Create:**

- `app/services/caller_turn_alignment.py`
- `tests/unit/test_caller_turn_alignment.py`

**Tests first:** Unicode/language-specific normalization and segmentation,
punctuation/spacing, cumulative/delta fragments, CER/WER edit operations,
code-switching, critical spans, digits, negation, correction, unique mapping,
ambiguity margin, corrupted-but-nearest failure, below-threshold mapping, immutable
assembler input, bounds, and no model or embedding judge.

### Task 5: Add independent measurement and evaluator contracts

**Create:**

- `app/services/caller_turn_measurement.py`
- `scripts/evaluate_gemini_caller_turn_qualification.py`
- `tests/unit/test_caller_turn_measurement.py`
- `tests/unit/test_evaluate_gemini_caller_turn_qualification.py`

**Tests first:** independent reducer agreement, encrypted allowlisted audit capsule,
custodian-key separation, keyed commitments, fixed-cardinality primitive records,
signed Merkle root, no text in primitive/published evidence, recomputed normalization/
assignment/CER/WER/critical spans, phase transitions, development-only selection,
policy-lock binding, one selected-policy holdout, per-language sample thresholds,
wire-interaction thresholds, exact Gate 0 assembly thresholds, contradictory records,
partial-run no-go, small-cell publication suppression, and all nonauthorization
fields.

### Task 6: Add the injected Gate 0B session executor

**Create:**

- `scripts/run_gemini_caller_turn_qualification.py`
- `tests/unit/test_run_gemini_caller_turn_qualification.py`

**Tests first:** dry-run zero network, approval/ledger-before-credential, connector
injection, exact TLS host and no proxy/redirect/debug/crash/TLS-key-log path, canary
secret sinks, official setup shape, all parts in one 3.1 server event, independent
reducer agreement, real receipt-clock injection, paced sending, synchronous mock
tools, interruption, cancellation, consecutive turns, fresh-connection restart epoch,
GoAway/close, malformed/oversized messages, usage metadata, provider-request and
cost reservation, current-versus-prior output-turn attribution, premature current
response rejection, bounded prior-turn interruption tail, total session/run
deadlines, output disposal, and capsule handoff.

The executor computes no verdict. The selected authentication transport has failure-
path tests proving credentials and authenticated URLs cannot enter output, logs,
exception chains, snapshots, crash handlers, or reports.

### Task 7: Add preregistration and runbook

**Create:**

- `docs/gemini-caller-turn-qualification-gate-0b.md`

**Modify:**

- `docs/adr/0001-gemini-retrospective-caller-turns.md` only to describe the pending
  Gate 0B process; do not record a go decision.

**Tests first:** CLI help and dry-run output name every immutable value, exact command
examples write outside the repo, no credential default exists, and all evidence
booleans remain false.

### Task 8: Offline verification and implementation PR

Require:

- focused and full unit suites;
- touched-file Ruff;
- Bandit on all executor/evaluator code;
- secret, PII, fixture provenance, license, and consent-contract scans;
- frozen-lock, import-origin, environment-identity, and audit-capsule threat tests;
- lock consistency plus before/after environment and clean-tree identity assertions;
- `compileall` and `git diff --check`;
- dependency/import and clean-tree mutation tests;
- proof that live pipeline files have no diff or imports;
- zero DNS/socket/provider attempts under tests and dry-run;
- staff, security/privacy, product/caller-experience, and QA review of the exact
  commit.

Merge the implementation PR before generating an executable preregistration.

### Task 9: Separate preregistration approval

From exact merged `main`, generate and review:

- clean-tree/source identity;
- corpus and consent summary;
- holdout-custodian, paid-project, ZDR-or-residual-retention, and deletion controls;
- complete canonical setup and deviations;
- model/endpoint/transport and credential reference;
- candidate policies and deterministic selection rule;
- issuer trust root, campaign/attempt authorization IDs, ledger location, and all
  attempts, sessions, activity, no-speech, fresh-restart, provider-request, time,
  audio, and cost caps;
- pricing identity;
- output and cleanup paths;
- canonical preregistration SHA-256.

Stop and ask for explicit user approval quoting those values. This plan and the
implementation PR do not authorize Task 10.

### Task 10: Approved execution and evidence PR

Only after exact approval:

1. Run the approved matrix without changing any value.
2. Retain only the approved encrypted audit capsule, text-free primitive records,
   and aggregate report during the bounded evidence-review window.
3. Let the independent evaluator recompute from the encrypted audit capsule; complete
   the evidence-panel review, then delete the capsule/key and record attestation.
4. Perform local/provider retention, residue, credential revocation, secret, PII,
   provenance, and ledger audits.
5. Finalize the ADR with go or no-go for the exact model/setup/policy/window.
6. Create a separate evidence branch and PR from current `main`.

No extraction or runtime task starts automatically after a go result.

## 13. Verification Commands

Commands become valid as files land:

```bash
uv lock --check
uv sync --locked --extra dev --python 3.12.13
uv run --locked --no-sync --extra dev --python 3.12.13 \
  python scripts/verify_qualification_environment.py --phase before

uv run --locked --no-sync --extra dev --python 3.12.13 python -m pytest \
  tests/unit/test_caller_turn_qualification.py \
  tests/unit/test_qualification_identity.py \
  tests/unit/test_caller_turn_alignment.py \
  tests/unit/test_caller_turn_measurement.py \
  tests/unit/test_run_gemini_caller_turn_qualification.py \
  tests/unit/test_evaluate_gemini_caller_turn_qualification.py -q

uv run --locked --no-sync --extra dev --python 3.12.13 python -m pytest \
  tests/unit/test_caller_turns.py \
  tests/unit/test_gemini_turn_events.py \
  tests/unit/test_caller_turn_assembly_eval.py \
  tests/unit/test_qualify_gemini_caller_turn_assembly.py \
  tests/unit/test_voice_turn_replay.py -q

uv run --locked --no-sync --extra dev --python 3.12.13 \
  python -m pytest tests/unit -q

uv run --locked --no-sync --extra dev --python 3.12.13 ruff check \
  app/services/caller_turn_qualification.py \
  app/services/qualification_identity.py \
  app/services/caller_turn_alignment.py \
  app/services/caller_turn_measurement.py \
  app/services/voice_turn_replay.py \
  scripts/verify_qualification_environment.py \
  scripts/run_gemini_caller_turn_qualification.py \
  scripts/evaluate_gemini_caller_turn_qualification.py \
  tests/unit/test_caller_turn_qualification.py \
  tests/unit/test_qualification_identity.py \
  tests/unit/test_caller_turn_alignment.py \
  tests/unit/test_caller_turn_measurement.py \
  tests/unit/test_run_gemini_caller_turn_qualification.py \
  tests/unit/test_evaluate_gemini_caller_turn_qualification.py

uv run --locked --no-sync --extra dev --python 3.12.13 \
  python scripts/verify_qualification_environment.py --phase after

git diff --check
git diff d7969ac -- app/services/gemini_pipeline.py app/services/voice_pipeline.py
rg -n "caller_turn_qualification|qualification_identity|caller_turn_alignment" \
  app/services/gemini_pipeline.py app/services/voice_pipeline.py
```

Expected: no live-pipeline diff/import, no provider access, and no payload/credential
artifact.

## 14. Stop Conditions

Stop and return to review when:

- the exact Gemini model or official API behavior changes;
- the selected transport cannot keep credentials out of application output/logs;
- the corpus lacks consent, rights, provenance, subject-disjointness, or coverage;
- the paid-project, ZDR/residual-retention, holdout-custodian, access, or deletion
  controls are unverified;
- multilingual alignment cannot be validated without a model judge;
- the two independent reducers disagree, audit-capsule custody fails, or primitive
  facts cannot be recomputed independently;
- any raw payload, transcript, audio, prompt, path, credential, subject, caller,
  contractor, provider request, or exception body reaches published evidence;
- any transcript or reference escapes the encrypted, access-audited audit capsule;
- any executable dependency or worktree byte differs from preregistration;
- approval is missing, expired, or mismatched;
- an authorization signature, attempt ledger, lease, reservation, or one-use claim is
  missing, inconsistent, consumed, concurrent, or tampered;
- usage metadata is missing or cost cannot be bounded;
- a cap is reached or an unplanned retry would be required;
- any holdout value is inspected for tuning;
- holdout is materialized before the policy lock, a session crosses splits, or any
  non-selected policy touches holdout;
- any provider call would use a production project, live credential, real caller data,
  or unreviewed model/setup;
- no bounded policy passes every Gate 0 threshold;
- transcription fidelity or wire-observable interaction integrity fails its sample
  gate even when assembly passes.

If no policy passes, finalize a no-go ADR. Do not add prompt rules, relax thresholds,
increase caps, rerun holdout, substitute a subject/model/setup, or build extraction
around the current transcription surface.

## 15. Required Review Boundary

This plan must receive panel approval before implementation. The implementation must
then receive exact-commit staff, security/privacy, product/caller-experience, and QA
approval before merge.
Provider execution requires a later user message approving the exact canonical
preregistration digest and values. Approval at any earlier layer does not flow into a
later layer.
