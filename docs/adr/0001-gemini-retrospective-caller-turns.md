# ADR 0001: Gemini Retrospective Caller Turns

- Status: Proposed, pending Gate 0B empirical evidence
- Date: 2026-07-14
- Decision owners: Hey Kevin voice and controller maintainers
- Scope: Offline caller-turn assembly qualification only

## Context

Gemini Live input transcription is delivered independently from other server
messages and does not carry a provider finality marker. Model output, generation
completion, turn completion, interruption, tool, reconnect, and transcription
events may arrive in orders that make an opportunistic transcript flush incomplete.

The receptionist controller accepts only typed, provider-neutral observations. It
must not parse raw transcripts, depend on Gemini message shapes, or infer that a
caller turn is final merely because model output started.

## Proposed Decision

Use two offline ownership boundaries:

```text
Gemini raw server message
  -> GeminiTurnEventAdapter
  -> CallerTurnEvent
  -> CallerTurnAssembler
  -> RetrospectiveCallerTurn
```

The Gemini adapter performs bounded shape decoding only. It neither retains nor
reports raw messages. The assembler is provider-neutral and applies an explicit,
bounded quiescence policy after a terminal candidate. Reconnect, connection close,
pipeline stop, cancellation, and resource exhaustion produce explicit non-complete
statuses.

Every accepted turn is labeled `retrospective_complete`; it is never described as
provider-final. A complete-looking turn may be used by a future asynchronous
extractor only after Gate 0B demonstrates that the selected policy meets the
pre-registered provider-ordering thresholds.

## Current Evidence

Gate 0A synthetic permutations exercise transcript fragments around model output,
generation completion, turn completion, interruption, tool activity, cancellation,
connection close, reconnect, pipeline stop, duplicate events, consecutive turns,
invalid input, and resource bounds. The offline evaluator records fixture and code
identity and emits aggregate, payload-free results.

This evidence demonstrates deterministic behavior for authored permutations only.
No Gemini provider request, real call, staging call, or production call is part of
Gate 0A. Therefore:

- `turn_assembly_validated` remains false;
- provider ordering viability remains undecided;
- no final quiescence policy has been selected;
- no runtime or shadow plan is authorized.

## Gate 0B Decision Rule

A later, separately approved run may finalize this ADR only when its immutable
pre-registration names the exact model, API version, endpoint, non-production
project, dedicated credential reference, manifest digest, setup digest, source SHA,
attempt cap, wall-clock cap, timeout, and cost cap.

Retrospective assembly receives a go decision only if the sealed holdout meets every
threshold in the approved qualification plan, including at least 99 percent exact
activity-to-turn assignment, zero cross-turn contamination, zero duplicate
application, correct lifecycle classification, no accepted turn changed by an
out-of-policy late fragment, per-language parity, and fail-closed teardown and
resource behavior.

If no bounded policy passes, the decision is no-go and extraction work stops.

## Consequences

This surface cannot control the response Gemini is already generating. It cannot,
by itself, prevent a same-turn repeated question, choose tools before response
generation, or prove caller-experience improvement.

Future pre-response control candidates require a separate design and evidence set.
They may include provider-native activity controls, a pre-response text or tool
gate, or another bounded turn-taking surface, but none is selected by this ADR.

The following remain prohibited until a later reviewed plan explicitly authorizes
them:

- importing these modules from `gemini_pipeline.py` or `voice_pipeline.py`;
- extracting semantic observations or mutating `IntakeState`;
- using real caller audio, transcripts, identifiers, or credentials;
- staging, production, deployment, feature flags, or release claims.

## References

- `docs/superpowers/plans/2026-07-14-typed-receptionist-observation-qualification.md`
- Gemini Live API: <https://ai.google.dev/api/live>
- Gemini Live capabilities: <https://ai.google.dev/gemini-api/docs/live-api/capabilities>
- Gemini Live tools: <https://ai.google.dev/gemini-api/docs/live-api/tools>
