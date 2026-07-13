# Gemini Live Provider Qualification

Date: 2026-07-13

## Decision Scope

This is an offline feasibility comparison, not a live-path change or release
authorization. It compares the current Gemini Developer API voice candidate
with Google's generally available enterprise Live model using the same public,
transcript-free replay corpus and release gates.

The comparison answers one question: which currently available Gemini Live
treatment is the stronger candidate for a later end-to-end qualification? It
does not authorize staging, production, Provisioned Throughput, customer audio,
or changes to `app/services/gemini_pipeline.py` or
`app/services/voice_pipeline.py`.

## Precommitted Treatments

### Control

- Provider: Gemini Developer API.
- Model: `gemini-3.1-flash-live-preview`.
- Endpoint: `google.ai.generativelanguage.v1beta.GenerativeService`.
- Authentication: `GEMINI_API_KEY`; the key must never be printed or persisted.
- Thinking: `thinkingLevel=minimal`, as required by Gemini 3.1.

### Treatment

- Provider: Gemini Enterprise Agent Platform through the regional Vertex AI
  endpoint.
- Model: `gemini-live-2.5-flash-native-audio`.
- Location: `us-central1`.
- API: `google.cloud.aiplatform.v1.LlmBidiService`.
- Authentication: Application Default Credentials with the Cloud Platform
  scope; bearer tokens must never be printed or persisted.
- Thinking configuration: omitted because this GA Live model does not expose
  the Gemini 3.1 `thinkingLevel` contract.

The Vertex setup model is the full resource
`projects/<project>/locations/us-central1/publishers/google/models/gemini-live-2.5-flash-native-audio`.
The project identifier is supplied at runtime and excluded from reports.

## Shared Configuration

Both providers use:

- audio responses and the `Puck` voice;
- `maxOutputTokens=120`, `temperature=0.4`, and the same one-sentence
  receptionist instruction;
- input and output transcription enabled to match the candidate behavior, but
  neither transcript stream is read into a report or persisted;
- automatic VAD with high start sensitivity, high end sensitivity, 100 ms
  prefix padding, and 500 ms silence duration;
- `START_OF_ACTIVITY_INTERRUPTS` and `TURN_INCLUDES_ONLY_ACTIVITY`;
- an automatic arm and a ground-truth manual `activityStart`/`activityEnd` arm;
- no session resumption, context cache request, tools, grounding, search,
  request-response logging, or provider payload logging.

Provider wire differences are limited to the authenticated endpoint, full
Vertex model resource, supported thinking configuration, and the documented
realtime audio envelope (`audio` for the Developer API and `mediaChunks` for
Vertex).

## Corpus And Privacy

Only `tests/fixtures/voice_vad/fleurs_turn_replay_manifest.json` is allowed.
It contains pinned public FLEURS English US and Latin American Spanish audio,
with no transcript text, customer audio, phone number, request ID, or tenant
data. Reports contain only counts, latency aggregates, bounded error classes,
model/provider labels, seed, and corpus hashes.

Session resumption must be absent from serialized setup messages. The
treatment may proceed with public fixtures if the project-level in-memory cache
or abuse-monitoring exception cannot be confirmed, but that uncertainty remains
a hard blocker for customer data and promotion. Grounding is forbidden because
it is incompatible with zero data retention.

## Run Matrix And Spend Bound

1. Run unit and protocol tests with no network access.
2. Perform read-only checks for ADC, API enablement, project cache configuration,
   and available IAM access. Do not mutate project settings.
3. Run a one-trial, six-case smoke for both providers with seed 7.
4. Only if both smoke runs establish sessions and produce complete aggregate
   reports, run five trials per case for seeds 29 and 41 on both providers.

The hard ceiling is 264 provider attempts:

- 24 smoke attempts: 2 providers x 6 cases x 2 VAD arms;
- 240 qualification attempts: 2 providers x 2 seeds x 6 cases x 5 trials x
  2 VAD arms.

The run must stop on credential, IAM, API availability, setup, or serialization
failure. It must not retry a failed full treatment with changed thresholds or
configuration.

## Governing Gates

Each provider and seed must independently satisfy:

- 30 automatic and 30 manual attempts for a qualification run;
- 100% paired coverage, terminal completion, and latency coverage;
- zero provider errors;
- zero manual premature responses or interruption events;
- manual speech-end-to-first-audio p95 at or below 1,500 ms;
- manual speech-end-to-first-audio maximum at or below 2,500 ms.

The worse seed governs. A provider that fails any gate is not qualified. If
both pass, rank them by the lower worse-seed automatic speech-end-to-first-audio
p95, then maximum, while preserving zero errors and full terminal coverage. A
tie or mixed result is inconclusive and keeps the current live default and
rollback unchanged.

Even a winning offline provider remains blocked from staging until the full
telephone path passes greeting, barge-in, playout-clear acknowledgement, tool
latency, reconnect, multilingual, privacy, and revision-filtered release gates.

## Primary Sources

- Gemini 3.1 Flash Live Preview:
  <https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview>
- Gemini Developer Live capabilities:
  <https://ai.google.dev/gemini-api/docs/live-api/capabilities>
- Gemini Enterprise Live overview:
  <https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api>
- Enterprise WebSocket session protocol:
  <https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/start-manage-session>
- Vertex AI zero data retention:
  <https://docs.cloud.google.com/vertex-ai/generative-ai/docs/vertex-ai-zero-data-retention>
