# Voice Architecture Provider Capabilities

Status: Stage 0 decision input only. Retrieved 2026-07-22. This matrix does not
authorize a provider connection, staging route, production route, caller-data use,
or a caller-experience claim. An `official_contract` documents a provider protocol
fact; it does not prove Kevin's end-to-end behavior.

## Terms and evidence tiers

`transport_resolved` is a provider or Twilio buffer receipt. It is never renamed
to played or heard. `caller_playback_observed` requires the planned encrypted,
caller-side PCMU harness. `playback_inferred` is only a preregistered conservative,
cancellable runtime deadline after transport resolution. It is not caller-heard
evidence and cannot alone authorize closure.

| Tier | Permitted claim |
| --- | --- |
| `offline_static` | Schema, source, configuration, or deterministic mock behavior only. |
| `bounded_connected_probe` | A preregistered synthetic, non-scoring protocol fact under one-use approval. |
| `sealed_selection` | Frozen, provider-connected technical evidence only. |
| `closed_loop_acceptance` | Consenting-participant caller experience only. |

## Pinned official sources

| Source | Retrieval | Pinned fact |
| --- | --- | --- |
| [Gemini Live capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities) | 2026-07-22 | VAD interruption cancels generation; output transcription and session facilities are documented. No universal Hey Kevin application permit is documented. |
| [Twilio Media Streams messages](https://www.twilio.com/docs/voice/media-streams/websocket-messages) | 2026-07-22 | `connected`, `start`, media, mark, and clear protocol; mark follows buffered audio completion or clear. |
| [Twilio webhook security](https://www.twilio.com/docs/usage/webhooks/webhooks-security) | 2026-07-22 | Validate the exact configured request URL, all parameters, and Twilio signature with the provider SDK; WebSocket signature header is lowercase. |
| [ConversationRelay onboarding](https://www.twilio.com/docs/voice/conversationrelay/onboarding) | 2026-07-22 | Initial WebSocket handshake includes a Twilio signature validated with the auth token and request URL. |
| [Twilio ConversationRelay messages](https://www.twilio.com/docs/voice/conversationrelay/websocket-messages) | 2026-07-22 | setup, prompt with `last`, interrupt, error, and streamed text tokens. |
| [Twilio ConversationRelay TwiML](https://www.twilio.com/docs/voice/twiml/connect/conversationrelay) | 2026-07-22 | `speechTimeout`, language behavior, and managed STT/TTS configuration. |
| [Deepgram endpointing](https://developers.deepgram.com/docs/endpointing) | 2026-07-22 | `speech_final` after a configured VAD pause. |
| [Deepgram endpointing/interim results](https://developers.deepgram.com/docs/understand-endpointing-interim-results) | 2026-07-22 | `is_final` segment finality is distinct from `speech_final` utterance endpointing. |
| [Deepgram Utterance End](https://developers.deepgram.com/docs/utterance-end) | 2026-07-22 | `UtteranceEnd` is a configured gap after finalized words, not a caller semantic guarantee. |

## Candidate capability matrix

## Candidate configuration registry

No model or managed dependency is selected by this document. Missing identity is an
intentional `not_selected` outcome: that arm is interface-stub-only and cannot
advance to an adapter, connected probe, or privacy review until a separate exact
registry revision names provider, product, model/API version, endpoint, dedicated
nonproduction account/project/subaccount/region, credential reference, and privacy
source. B1's text model and B2's managed STT/TTS are therefore `not_selected`;
A/C's Gemini model/API version is also `not_selected`. The existing Gemini, Deepgram
and ElevenLabs references describe possible surfaces only, not approval to use them.

| Candidate / assertion | Classification | Evidence tier required | Decision |
| --- | --- | --- | --- |
| A native Gemini: automatic VAD interruption and generation cancellation | `official_contract` | `bounded_connected_probe` for adapter mapping | Adapter may model interruption; it must clear Twilio playback separately and cannot infer caller audible stop. |
| A native Gemini: input finality / late fragments | `unavailable` | `bounded_connected_probe` | No adapter beyond an interface stub until the probe establishes ordering or eliminates A. |
| A native Gemini: universal app pre-response permit | `unavailable` | `bounded_connected_probe` | A remains a control unless manual-turn C proves zero audio-before-permit. |
| A/C native Gemini: provider generation/turn completion | `official_contract` | `offline_static` mapping plus probe | Telemetry only; neither signal proves semantic or caller playback completion. |
| A/C native Gemini: real-time response-correlated caller playback | `unavailable` | `bounded_connected_probe` with caller PCMU | Native output is not selectable until caller-side evidence and conservative runtime behavior pass. |
| A/C native Gemini: reconnect/resumption and epoch behavior | `official_contract` for session facilities; exact mapping `unavailable` | `bounded_connected_probe` | Freeze model/API version before probe; stale epochs must fail closed. |
| B1 Twilio Media Streams: authenticated HTTP/TwiML and WebSocket ingress | `official_contract` | `offline_static` then probe | Implement only in isolated bakeoff app. Pin configured canonical `https` and `wss` URLs; validate exact URL, all parameters, and signature with the provider SDK; forwarded host/protocol has authority only from a named trusted proxy. |
| B1 Media Streams: outbound media, mark, clear | `official_contract` | `offline_static` mapping plus caller-side probe | Mark is `transport_resolved`; clear produces transport evidence only. Neither is caller playback evidence. |
| B1 Deepgram: segment and pause endpointing | `official_contract` | `bounded_connected_probe` | `is_final`, `speech_final`, and `UtteranceEnd` are candidate input signals; application must test late fragments and pause behavior. |
| B1 Deepgram: pre-response app permit | `official_contract` at application boundary | `offline_static` | The coordinator may wait for its own authorized finality policy; provider endpointing does not itself authorize speech. |
| B1 ElevenLabs streaming TTS / exact audio-act binding | `offline_static` existing component only | `bounded_connected_probe` | No selection claim until exact act-to-audio and caller-side playout identity are demonstrated. |
| B2 ConversationRelay: final prompt and interruption | `official_contract` | `bounded_connected_probe` | `prompt.last` is a provider STT finality signal; interrupt is a managed TTS interruption fact, not a caller-heard completeness receipt. |
| B2 ConversationRelay: text token finality / managed playback receipt | `unavailable` | `bounded_connected_probe` with caller PCMU | Control-only until a real-time response-correlated normal-playback receipt is distinguishable from final-token, preemption, and post-call telemetry. |
| Every arm: partial, clear, delivery failure, caller activity, and audible stop | `unavailable` end-to-end | `bounded_connected_probe`, then `sealed_selection` | Only caller-side PCMU establishes `caller_playback_observed` and the interruption last-audible boundary. |
| Every arm: account/project/subaccount/region and privacy-setting attestation | `unavailable` until per-provider control-plane/API or signed evidence is pinned | `offline_static` then probe | Unknown retention, training/data-sharing, tracing, recording, cache/resumption, region, or account isolation is a no-go. |

## Ingress and identity requirements

For every HTTP/TwiML and WebSocket ingress, Task 2.1 must separately pin the
provider's canonical signature construction, configured external URL, allowlisted
nonproduction account/subaccount, and trusted proxy. Client-supplied forwarded host
or protocol is never authority. Before authentication, the only permitted work is
signature verification plus bounded auth-token-store operations; no media read,
provider construction, policy work, RTDB/Firestore business access, or callback
processing is permitted.

Each provider privacy field must record its source URL or control-plane observation,
provider/API version, evidence class, account/project/region identity, accountable
owner, expiry/recheck date, and residue-verification method. A dashboard or
post-call event is not a real-time receipt and not a privacy attestation.

## Selectability gate

All candidates remain non-selectable until the one-use approved probe proves their
required mappings in the isolated app. Any unavailable required capability leaves
the arm control-only or eliminates it; the shared lifecycle is never weakened to
keep an arm eligible. A successful capability probe does not select a winner,
authorize staging or production, or establish caller experience.
