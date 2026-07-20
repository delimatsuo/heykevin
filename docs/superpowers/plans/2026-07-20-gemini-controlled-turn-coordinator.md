# Gemini Controlled Turn Coordinator Plan

## Decision

The current native-audio Gemini Live path is not an acceptable control boundary for receptionist turns. It can begin sending audio before output transcription is complete, so the application cannot reliably enforce one question, complete safety guidance, or wait-before-hangup semantics.

Implement a default-off, staging-only controlled path:

`Deepgram final caller turn -> Gemini structured observation -> IntakeState -> DialoguePlanner -> server-rendered short turn -> semantic validation -> ElevenLabs TTS -> Twilio response_end receipt`

Normal intake questions, safety guidance, presence checks, and closing copy are
rendered by the application from the planned action. Gemini may return only
typed semantic facts and bounded answer categories; it never authors caller-heard
speech. This keeps AI-based language understanding and scope classification
while removing the model's authority over question count, hangup, and safety
wording.

This remains a Gemini architecture. The existing Claude/ElevenLabs pipeline is not a fallback or migration target. Production and non-allowlisted calls continue using their current configured engine.

## Authority and rollout boundary

- Environment: staging only.
- Cohort: SHA-256 contractor label allowlist only; never log raw contractor IDs.
- Model: exact stable `gemini-3.1-flash-lite`, never a moving `latest` alias.
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
| `playing` | validated text handed to TTS | `awaiting_reply` for any non-closing turn or `close_pending` for a closing turn after a valid `response_end` receipt; `listening` on caller barge-in or failed delivery |
| `awaiting_reply` | a non-closing turn's `response_end` mark resolved `played` | `listening` on caller activity; a played question enters `reprompting` on its first timeout, while a non-question statement proceeds to a receipt-gated silence close |
| `reprompting` | one deterministic presence check is being spoken | `awaiting_presence` only after its `response_end` mark resolves `played`; `listening` on caller activity |
| `awaiting_presence` | the presence check was fully played and the original answer slot is suspended | `resolving_presence` on caller activity or `playing` for silence close on timeout |
| `resolving_presence` | Gemini classifies the caller reply against the most recent presence check | `replaying_question` for acknowledgements, unclear replies, provider failure, or an unsupported sensitive answer; `generating` only for a typed substantive answer with required explicit evidence; `listening` with the suspended context preserved when newer caller activity cancels a stale classification |
| `replaying_question` | the caller acknowledged presence | `awaiting_reply` only after the exact original server-owned question's `response_end` mark resolves `played`; `listening` on caller barge-in or failed delivery |
| `owner_message_pending` | the owner requests message-taking | `playing` for one server-owned message question, replacing any previous question and its answer authority |
| `close_pending` | a closing turn was fully played | `ended` after the application invokes call completion |
| `ended` | call completion accepted | none |

`cleared`, `stale`, `timeout`, unknown, duplicate, wrong-turn, and first-media marks never establish audible completion, start a no-input timer, commit an asked slot, or authorize hangup.

## Structured model contracts

The observation response contains only the fields accepted by `CallerObservation`. Unknown fields and invalid enum values fail validation.

The same observation response may include only a `direct_answer_kind` enum.
Gemini never returns text for speech. The application renders the localized
answer and appends the server-owned question for the one planned slot, if any.
There is no second serial model call or model repair call; invalid or incomplete
answer classification selects the deterministic complete fallback immediately.

The model cannot select contractor IDs, phone numbers, tool arguments, side
effects, speech text, or direct hangup. The application validates the complete
server-rendered candidate before TTS. There is no serial repair call; invalid,
partial, or token-limited output selects a deterministic complete fallback and
is never spoken.

## Semantic gates

- Ordinary turns: at most one interrogative clause, at most 16 words, and one planned question slot.
- Closing turns: no question and no `expects_input`.
- Question turns: exactly one question and the server-planned slot.
- Safety turns: exact server-owned hazard templates, exempt from the ordinary
  word budget and never model-authored.
- Asked slots are committed only after the matching `response_end=played` receipt.
- Closing is committed and the call is ended only after the matching `response_end=played` receipt.
- `cleared`, `stale`, timeout, TTS errors, and mark-send errors recover to
  listening without committing a question or closing the call.
- A callback confirmation is accepted only as the answer to a confirmation
  question whose matching response-end receipt was played.
- English and Spanish presence checks and silence closes are deterministic in
  the detected caller language for the initial cohort.
- An affirmative answer to a presence check cannot satisfy the original intake
  slot. The original slot is structurally suspended before semantic
  classification. Bare affirmatives answer the latest presence check; only an
  explicit substantive answer can restore the suspended slot. All other results
  replay the exact original question, then start its answer timer only after that
  replay is confirmed played.
- An owner message-taking command is a typed lifecycle transition. It clears any
  previous question authority and asks one localized, server-owned message
  question whose slot is committed only after its own played receipt.

## Research basis

- Google documents structured output as a way to constrain the syntax of model
  output, while still requiring application-side semantic validation:
  <https://ai.google.dev/gemini-api/docs/structured-output>.
- Google's model catalog describes `gemini-3.1-flash-lite` as a stable,
  low-latency model for simple extraction and confirms structured-output
  support; the previous staging credential returned 404 for the old controlled
  model: <https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite>.
  Payload-safe current-account probes returned a median 844 ms for this exact
  model versus 1,399 ms for `gemini-3.5-flash`. The final schema correctly
  distinguished six synthetic English/Spanish presence acknowledgements and
  explicit callback answers in 814-1,119 ms, with valid schemas in every case.
- Google documents `thinkingLevel: minimal` for latency-sensitive Gemini 3
  work: <https://ai.google.dev/gemini-api/docs/generate-content/thinking>.
- Twilio defines a returned `mark` as confirmation that prior media was played,
  which is the lifecycle authority used here:
  <https://www.twilio.com/docs/voice/media-streams/websocket-messages>.
- LiveKit distinguishes controllable cascaded STT-LLM-TTS pipelines from
  speech-to-speech models and exposes turn-handling controls explicitly:
  <https://docs.livekit.io/agents/models/pipelines/> and
  <https://docs.livekit.io/agents/logic/turns/>.
- Vapi exposes endpointing, interruption, and backchannel controls as distinct
  pipeline concerns rather than leaving the language model to infer transport
  state: <https://docs.vapi.ai/customization/voice-pipeline-configuration>.
- Retell separates its real-time LLM websocket from telephony and requires
  explicit response and end-call events:
  <https://docs.retellai.com/api-references/llm-websocket>.
- Bland models controlled conversation progress as pathways with explicit nodes
  and transitions: <https://docs.bland.ai/tutorials/pathways>.
- ElevenLabs documents zero-retention mode for eligible text-to-speech requests,
  which is a hard activation gate for this cohort:
  <https://elevenlabs.io/docs/eleven-api/resources/zero-retention-mode>.

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
