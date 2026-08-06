# Native control timing instrumentation boundary

**Status:** pre-implementation specification; not authorization to modify a live
route, enable telemetry, or run a connected bakeoff.

## Purpose

This document bounds the smallest future measurement slice for the native voice
control. Its sole purpose is to make a later, isolated bakeoff observable without
changing caller-facing behavior. It does not select an architecture, tune a model,
or authorize a deployment.

The current native path is production-shaped. It owns provider work, media
transport, callbacks, timers, and terminal behavior. Therefore this specification
does not permit a direct edit to `app/services/gemini_pipeline.py` or
`app/webhooks/media_stream.py`.

## Preconditions

All of the following must be complete before an implementation diff is proposed:

1. The Stage 2 ingress/authentication contract is implemented and independently
   reviewed for the new isolated route.
2. A sealed, nonproduction bakeoff manifest names the candidate arm, source SHA,
   revision, timing definitions, retention boundary, and expected cohort.
3. Staff engineering and security review the exact diff and its negative tests.
4. Configuration is default-off, environment-limited to the authenticated
   nonproduction bakeoff route, and cannot be enabled by caller-provided input.

Until then, `VoiceTelemetryProjector` and `VoiceLifecycle` remain offline-only
contracts. They are not import permission for an existing live route.

## Allowed passive facts

The later implementation may emit only these fixed operational facts. Each event
uses a sealed candidate/revision/manifest identity and an HMAC-derived session
reference. It carries no raw caller or provider payload.

| Fact | Authoritative source | Permitted meaning |
| --- | --- | --- |
| `caller_last_speech_sample` | Common corpus ground truth or the encrypted caller-side harness after the final intentional caller audio sample | Common lower-bound anchor; never inferred from a transcript fragment or replaced by server ingress arrival time. |
| `candidate_activity_end` | Candidate detector only, when that detector exposes an authoritative event | Diagnostic only; `unavailable` is explicit and does not replace the common anchor. |
| `provider_generation_started` / `provider_generation_complete` | Provider adapter lifecycle callback | Provider generation lifecycle, not a claim that callers heard audio. |
| `first_audio_generated` | Candidate audio generator output boundary | First generated candidate audio, not transport delivery. |
| `first_media_sent` / `final_media_sent` | Authenticated isolated transport write acknowledgement | Server-to-telephony transport boundary, not playback. |
| `mark_resolved` | Twilio transport mark resolution | `transport_resolved` only. It never proves that the caller heard audio. |
| `caller_playback_observed` | Encrypted caller-side PCMU harness | The only caller-side playout evidence. It establishes expected audio at the caller-side telephone stream, not human understanding or acceptance. |
| `playback_inferred` | Preregistered, cancellable deadline after `transport_resolved` | An explicit operational inference when caller-side observation is unavailable; it is never called playback evidence and cannot alone authorize closure. |
| `queue_cleared`, `interrupted`, `reconnected`, `failed` | Isolated transport or lifecycle controller | Explicit non-success terminal reason, using a closed error class only. |
| `configured_ceiling` / `usage_total` | Sealed configuration and candidate-owned counter | Bounded numeric experiment metadata only. |

At caller ingestion, the primary latency anchor binds immutably to its session,
input-turn, and epoch. Generation and semantic-act identifiers do not yet exist
and must not be guessed. Once created, they join to that anchor through a
validated immutable mapping; every later generation, transport, and playout fact
must carry the resulting complete binding. A fact with a mismatched binding,
invalid order, missing required predecessor, or unknown field is rejected rather
than repaired or guessed.

## Hard exclusions

The measurement slice must not:

- change prompts, model choice, VAD, response pacing, output ceiling, or response
  generation;
- add or enable tools, callbacks, timer scheduling, terminal actions, transfers,
  or hangups;
- add provider calls, callbacks, background tasks, retries, network writes, or
  route registration;
- retain or log transcripts, raw audio, media frames, caller identity, phone
  values, provider raw messages, headers, credentials, or exception text;
- infer caller speech end from transcript text or claim playback from generation,
  queueing, or a media-send event;
- modify any production or staging route.

## Future exact-diff review package

Before any source edit, the implementer must submit a small review package that
contains:

1. The precise changed files and imports, limited to a new isolated bakeoff
   adapter and its tests. Direct changes to the native live route remain blocked
   unless a separate winner-specific integration plan approves them.
2. A field-by-field mapping from every allowed fact above to its source and
   receipt semantics.
3. The immutable default-off configuration and the proof that only the
   authenticated bakeoff environment can enable it.
4. A retention proof showing that only allowlisted numeric/enumerated values and
   derived identifiers reach the aggregate evaluator.
5. Deterministic negative tests that compare the controller command stream with
   telemetry disabled and enabled. They must prove equal prompts, provider calls,
   media writes, callbacks, timers, tools, terminal decisions, and errors.
6. Tests rejecting unknown fields, raw-payload canaries, cross-session/epoch
   events, out-of-order facts, missing playback evidence, and unsupported receipt
   semantics.

The exact diff must pass staff and security review before it is written to the
isolated route. Passing this document review is not permission to test callers or
deploy any environment.
