# Gemini Controlled Turn Coordinator Plan

## Decision

The current native-audio Gemini Live path is not an acceptable control boundary for receptionist turns. It can begin sending audio before output transcription is complete, so the application cannot reliably enforce one question, complete safety guidance, or wait-before-hangup semantics.

Implement a default-off, staging-only controlled path:

`Deepgram final caller turn -> Gemini structured observation -> IntakeState -> DialoguePlanner -> server-rendered short turn -> semantic validation -> ElevenLabs TTS -> Twilio response_end receipt`

Normal intake questions, safety guidance, presence checks, and closing copy are
rendered by the application from the planned action. Gemini may realize only a
bounded direct answer; its complete candidate still passes the same semantic
gate before TTS. This keeps AI-based language understanding and scope
classification while removing the model's authority over question count,
hangup, and safety wording.

This remains a Gemini architecture. The existing Claude/ElevenLabs pipeline is not a fallback or migration target. Production and non-allowlisted calls continue using their current configured engine.

## Authority and rollout boundary

- Environment: staging only.
- Cohort: SHA-256 contractor label allowlist only; never log raw contractor IDs.
- Model: exact stable `gemini-2.5-flash`, never a moving `latest` alias.
- Default: disabled.
- Mode: Business only; Personal mode is excluded even when the account retains
  a Business subscription entitlement.
- Side effects and model-selected tool calls: disabled in the first controlled cohort.
- Rollback: remove the allowlisted label or disable the flag; no account `voice_engine` mutation.
- TTS privacy: activation also requires verified ElevenLabs zero-retention
  eligibility and sends `enable_logging=false`; otherwise routing stays off.

## Turn lifecycle

The application owns these states:

| State | Entry | Only valid exits |
|---|---|---|
| `listening` | caller may speak | `generating` |
| `generating` | committed caller turn | `playing`, `listening` on safe failure |
| `playing` | validated text handed to TTS | `awaiting_reply`, `close_pending`, or `listening` after a valid `response_end` receipt; `listening` on caller barge-in |
| `awaiting_reply` | a question's `response_end` mark resolved `played` | `listening` on caller activity, `reprompting` on first timeout, `playing` for silence close on second timeout |
| `reprompting` | one deterministic presence check is being spoken | `awaiting_reply` only after its `response_end` mark resolves `played`; `listening` on caller activity |
| `close_pending` | a closing turn was fully played | `ended` after the application invokes call completion |
| `ended` | call completion accepted | none |

`cleared`, `stale`, `timeout`, unknown, duplicate, wrong-turn, and first-media marks never establish audible completion, start a no-input timer, commit an asked slot, or authorize hangup.

## Structured model contracts

The observation response contains only the fields accepted by `CallerObservation`. Unknown fields and invalid enum values fail validation.

The optional direct-answer response contains:

- `action`: exact server-planned `ActionName` value.
- `expects_input`: exact `NextAction.question_required` value.
- `asked_slot`: empty or the single allowlisted planned slot.
- `spoken_text`: the complete text that may be sent to TTS.
- `safety_complete`: required for safety guidance.

The model cannot select contractor IDs, phone numbers, tool arguments, side effects, or direct hangup. The application validates the complete candidate before TTS. One bounded repair is allowed; a second invalid result selects a deterministic complete fallback. Partial or token-limited output is never spoken. Normal intake and safety turns do not require this second model call.

## Semantic gates

- Ordinary turns: at most one interrogative clause, at most 16 words, and one planned question slot.
- Closing turns: no question and no `expects_input`.
- Question turns: exactly one question and the server-planned slot.
- Safety turns: exempt from the ordinary word budget, must be marked complete, and must include immediate leave/avoid-danger and emergency/gas-utility direction for the detected hazard.
- Asked slots are committed only after the matching `response_end=played` receipt.
- Closing is committed and the call is ended only after the matching `response_end=played` receipt.
- `cleared`, `stale`, timeout, TTS errors, and mark-send errors recover to
  listening without committing a question or closing the call.
- A callback confirmation is accepted only as the answer to a confirmation
  question whose matching response-end receipt was played.
- English and Spanish presence checks and silence closes are deterministic in
  the detected caller language for the initial cohort.

## Payload-safe telemetry

Only fixed event names and typed scalar fields are logged: call label, caller/response turn, lifecycle state, model stage, attempt, provider milliseconds, validation reason enum, word/question counts, playback phase/status, audio duration, and deploy SHA. Caller/model text, full phone numbers, contractor IDs, tokens, and free-form error payloads are prohibited.

## Qualification gates

Before caller testing:

1. Red/green unit tests for lifecycle receipts, caller activity, one reprompt, and receipt-gated closing.
2. Structured-output tests for unknown fields, invalid enums, token-limited/partial output, one-question enforcement, ordinary duration budget, question-plus-goodbye rejection, and safety completeness.
3. Routing tests proving default-off, staging-only, exact-model, hash-allowlist behavior and no `voice_engine` mutation.
4. Existing receptionist replay tests, focused voice tests, full unit suite, Ruff, diff integrity, and payload-safe added-line secret scan.
5. Verify ElevenLabs zero-retention eligibility using synthetic text only.
6. Independent staff review of the exact candidate SHA.
7. Exact-SHA staging health verification, then the matched caller script. Production remains out of scope.

Caller acceptance requires zero ordinary multi-question turns, ordinary audio p95 at or below four seconds and max at or below five seconds, no later-turn degradation, no hangup while an answer is pending, fully audible reprompt/closing, complete safety guidance, revision-scoped timing evidence, and caller feedback.
