# Semantic Turn Control Qualification Contract

**Status:** Offline planning approved; detector implementation, live shadow,
manual activity control, and production are blocked. Evidence base:
`937e6918ce47dd3148ec075dd10d6b9d1f1e6608`.

## Decision

No detector is approved. The current Gemini automatic VAD remains authoritative
in staging. WebRTC may produce diagnostic candidates only; no candidate may
clear assistant audio or send Gemini `activityStart`/`activityEnd`. PR #76 and
production remain outside this work.

Every candidate review declares `input_mode` as `audio_waveform` or
`transcript_events`. Audio candidates consume only bounded waveform state.
Transcript candidates require a separate privacy-approved text-fixture contract
and must additionally satisfy the STT latency clocks below; corpus v3 contains
only numeric provider-event timing and cannot by itself qualify a text model.

The first offline candidate was Pipecat Smart Turn v3.2 CPU. It is audio-native,
local ONNX, BSD-2-Clause, and lists 23 supported languages. The exact evaluation
inputs were:

- source commit `4786657e242dfe77dd138699ac564ee074a2a543`
- model revision `f766f81d3cfdf7737ac64aad813d91bbfd56bf93`
- `smart-turn-v3.2-cpu.onnx`, 8,679,182 bytes
- model SHA-256 `2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f`
- NumPy `2.5.1`, ONNX Runtime `1.27.0`, Transformers `5.13.1`
- 16 kHz mono normalized waveform, last eight seconds, Whisper feature
  extraction, sequential one-thread ONNX execution, completion threshold `>0.5`

The disposable aggregate-only probe accepted all six valid final candidates
but also accepted all nine premature natural-pause candidates. Inference was
18.395 ms p95/max. It fails correctness and is rejected. The model and packages
must not enter `pyproject.toml`, the application dependency graph, or the Cloud
Run image.

LiveKit Turn Detector is not a candidate because its model license restricts
use to LiveKit Agents. TEN Turn Detection is not a candidate because it requires
transcript processing and a 7B-class text model, supports only English/Chinese,
and carries additional restrictions.

VideoSDK NAMO v1 Multilingual was also rejected. The disposable probe pinned
source commit `75ddd0e858ef3fc28a65dbc48bb74d7e9b462f3c`, model revision
`59245aac1f0a0f170277ebecc32ddaea418083b4`, and quantized-model SHA-256
`67be31dac7c49abd43a277a105222c9031a0e22ac37f0e22dab395f68b318fc9`.
On the two clean public fixtures, only three of five pause candidates had any
transcript available at the 300 ms decision point. NAMO rejected zero of one
premature candidate with text and accepted zero of two valid final candidates;
inference was 27.590 ms p95/max. Transcript availability and correctness both
fail. A future candidate needs a new review.

One Deepgram Flux Multilingual configuration was evaluated as a hosted,
audio-waveform candidate and rejected from advancement. The aggregate-only
probe used `flux-general-multi`, 16 kHz mono linear PCM, the
provider-recommended 80 ms stream chunks with trailing-edge pacing, no language
hints, `eot_timeout_ms=5000`, and model-improvement opt-out. It replayed the two
clean public English and Spanish fixtures as baselines and with deterministic
internal pauses of 500, 800, and 1,200 ms, for eight scenarios per threshold.
Provider transcripts, words, request IDs, error payloads, audio, and
attempt-level records were discarded; reports retained fixed language/scenario
buckets and numeric or boolean aggregates only.

The exact disposable probe identities were:

- harness SHA-256 `6a05c112ba5f465c24b694a0bf74fb3a9fbada3d3a8b4e44de1f7e5bcb0bd44f`
- replay helper SHA-256 `321e11fce4c1b8318b4eb53e41271517c9a85ae0e188e6a4eb2d40c9a17975af`
- audio helper SHA-256 `b918ed588fe1e0804e1bbc82fb2c24bde2bbd5b76cb1ee74bb24c0d755adddef`
- dependency-manifest SHA-256 `d5d095795b83d5c5667921633faa714abcb0a10d0e480351afc41f23a4a1c6a8`
- source-manifest SHA-256 `ae84a0428697119bc794e70d661bfda94e3cbc7fc5f396283b72e29995790e5c`
- source-corpus SHA-256 `b0c23e5a964427d7e51193913c760e39143d957a0660e9fe8ae3ad47ea515636`
- rendered probe-corpus SHA-256 `ce64de260818e99140d239ba86eff7654df83ff39426d85c0d77310410a56c0e`
- Python `3.12.13`, WebSockets `15.0.1`, schedule seed `73`

Deepgram exposes only the mutable model alias; no immutable revision or model
digest was reported. Each threshold received one seeded eight-scenario
feasibility pass:

| EOT threshold | Decisions | Premature ends | Errors | Language matches | Decision p95/max | Result |
|---:|---:|---:|---:|---:|---:|---|
| 0.70 | 8/8 | 3 | 0 | 8/8 | 1,153/1,153 ms | reject |
| 0.80 | 8/8 | 2 | 0 | 8/8 | 1,203/1,203 ms | reject |
| 0.85 | 8/8 | 0 | 0 | 8/8 | 1,452/1,452 ms | reject |

At `0.70`, premature decisions occurred in the 800 and 1,200 ms pause
buckets. At `0.80`, both 1,200 ms pauses ended prematurely. Threshold `0.85`
eliminated premature ends in this pass but increased latency. Clean baseline
maximum latency was 1,053, 1,076, and 1,189 ms respectively, so even the
no-pause cases fail the 500 ms p95 and 800 ms maximum contract. The harness
buffers each 80 ms chunk and sends it at the trailing edge of its nominal
interval. The true-speech-end clock therefore includes capture buffering and
does not give the provider an artificial look-ahead.

No tested threshold passed both safety and latency, so this exact configuration
does not advance to the full offline corpus. `eot_timeout_ms` was fixed at 5,000
and was not swept, and the hosted alias is mutable, so this evidence does not
reject every possible Flux configuration or future revision. Either would be a
new candidate review. Hosted network egress, the incomplete development corpus,
unavailable holdout, and unreported model revision independently block
qualification; no runtime dependency, shadow path, or live wiring is approved.

Primary references:

- https://github.com/pipecat-ai/smart-turn
- https://huggingface.co/pipecat-ai/smart-turn-v3
- https://docs.pipecat.ai/api-reference/server/utilities/turn-detection/smart-turn-overview
- https://huggingface.co/livekit/turn-detector/blob/main/LICENSE
- https://github.com/TEN-framework/ten-turn-detection
- https://github.com/videosdk-live/NAMO-Turn-Detector-v1
- https://huggingface.co/videosdk-live/Namo-Turn-Detector-v1-Multilingual
- https://developers.deepgram.com/docs/flux/quickstart
- https://developers.deepgram.com/docs/flux/configuration
- https://developers.deepgram.com/docs/flux/language-prompting
- https://developers.deepgram.com/reference/speech-to-text/listen-flux

## Three Modes

1. **Candidate generation:** offline or diagnostic observations only. No caller
   effect and no runtime dependency.
2. **Passive shadow:** a structurally separate component may score hypothetical
   actions but cannot access Gemini send/clear callbacks. Entry requires all
   offline gates, privacy/resource isolation, exact owner eligibility, and a
   rehearsed rollback that automatically restores prior traffic.
3. **Active control:** may send activity signals or authorize a clear only after
   two passing shadow cohorts, separate speech-start qualification, and a tested
   in-call automatic-VAD fallback. This mode requires a new approval.

## Independent Gates

All reports include exact code SHA, model/package digests, corpus SHA, scenario
bucket, language bucket, and aggregate counts. No call ID, text, audio, provider
payload, or text-length proxy is allowed.

| Gate | Requirement |
|---|---:|
| Speech-start false clears | 0 |
| Missed labeled interruptions | 0 |
| Interruption to Twilio clear | p95 <=250 ms; max <=500 ms |
| Premature semantic ends | 0 |
| Missing semantic ends / detector errors | 0 |
| Semantic decision coverage | 100% |
| Pause candidate to detector decision | p95 <=100 ms; max <=200 ms |
| True speech end to qualifying STT receipt (transcript input only) | p95 <=300 ms; max <=500 ms |
| Qualifying STT receipt to detector decision (transcript input only) | p95 <=100 ms; max <=200 ms |
| True speech end to accepted decision | p95 <=500 ms; max <=800 ms |
| Decision to Gemini activity-message WebSocket send completion | p95 <=50 ms; max <=100 ms |
| Gemini signal to first model audio | p95 <=1,000 ms; max <=1,500 ms |
| First model audio to Twilio acceptance | p95 <=100 ms; max <=250 ms |
| True speech end to Twilio acceptance | p95 <=1,500 ms; max <=2,500 ms |

Detector quality and provider latency are reported separately even when the
end-to-end gate fails. Random seeds change schedule order; they do not create
independent evidence for a deterministic detector.

Candidate generation must pass semantic-detector gates and any hypothetical
speech-start authorization gates. Passive shadow may corroborate those results
but cannot satisfy gates that require a Gemini signal, audio clear, model audio,
or Twilio acceptance. Those gates remain N/A, never passed or waived, until
separately approved active-control testing and are mandatory before production.

## Corpus V3

The machine contract is
[`schemas/voice-turn-corpus-v3.schema.json`](schemas/voice-turn-corpus-v3.schema.json).
It stores licensed/public audio references and numeric event labels only. It has
no transcript field.

The future loader must additionally enforce invariants that JSON Schema cannot
fully express: path confinement with no symlink escape, concatenated 64-digit
source checksums, unique IDs, valid cross-references, monotonic event order,
track bounds within source and scenario duration, ordered end windows, disjoint
development/holdout speakers, and a semantic corpus fingerprint over every
source, transform, event, and expected action.

Before candidate approval, the development set must include at least six
languages, five independent speakers per language, and 40 scenarios per
language. A sealed holdout must add 20 scenarios per language from disjoint
speakers. Required buckets are natural pauses, short answers, long turns over
eight seconds, corrections, late corrections, code-switching, fast starts,
background noise, speakerphone echo, assistant/caller double-talk, deliberate
barge-in, frame fragmentation, reconnect, delayed/duplicated/missing provider
events, detector timeout, and hangup. A production language claim is limited to
languages that pass; "all languages" remains unqualified.

Development and holdout are separate artifacts. Before opening a holdout, an
independent reviewer records the sealed artifact SHA-256 while the candidate
code, model, threshold, preprocessing, and configuration are frozen at exact
hashes. Implementers do not inspect holdout scenarios before that freeze. The
holdout is opened once, produces aggregate output only, and cannot be reused
after any tuning; a changed candidate requires a new disjoint holdout and a new
precommitted hash.

## State And Failure Contract

- A pause detector may enqueue one coalescing candidate per call. It never ends
  a turn by itself.
- At most eight seconds of 16 kHz mono PCM is retained in a 256 KB call-scoped
  ring buffer. No transcript is required by an audio-native candidate.
- At most one inference may be in flight per call. A global concurrency cap is
  established by an offline CPU saturation benchmark. The hard decision timeout
  is 200 ms.
- New caller speech invalidates an outstanding end decision. Late decisions,
  duplicate signals, old epochs, mismatched candidate IDs, and post-hangup work
  are discarded.
- A speech-start candidate cannot clear audio without a separately qualified
  corroborating decision. Echo, noise, and double-talk must resolve to no clear.
- Shadow overflow, timeout, model error, reconnect, or integrity failure disables
  shadow for that call without delaying direct Gemini forwarding.
- Active-control failure may reconnect with automatic VAD and replay audio only
  when the buffer contains the complete current uncommitted caller turn. Old
  session output is discarded. The retained turn is erased immediately after
  replay or failure. If the complete turn is unavailable, ask the caller to
  repeat or route to the existing deterministic safe fallback. Partial-turn
  replay must never be treated as complete. Every branch proves exactly one
  terminal outcome with no stale response or indefinite detector wait.

## Privacy, Eligibility, And Recovery

Passive or active live work requires all of the following:

- `ENVIRONMENT == "staging"`
- server-controlled default-false flag
- exact authenticated contractor ID from verified call state
- exact experiment version
- missing or mismatched values deny eligibility
- production startup rejects an enabled experiment

State is memory-only, tenant-scoped, bounded, and erased on hangup, timeout, and
exception. Reconnect erases old-session state except the explicitly bounded
complete uncommitted turn allowed by the fallback contract; that turn is erased
immediately after replay or failure. Model loading and inference have no network
egress or remote code. Metrics use a fixed allowlist and contain no identifiers,
audio, transcript, provider messages, token values, or utterance-shape proxies.

Before passive shadow, rollback must automatically restore and verify the exact
prior traffic split when post-switch health validation fails. Deployment
rollback does not replace the in-call fallback.

## Authorized Sequence

1. Contract and corpus-v3 schema.
2. Review and pin one candidate that passes disposable feasibility.
3. Narrow stacked offline-only PR: fixtures, evaluator, model-fetch verifier,
   and aggregate reports; no live imports or runtime dependencies.
4. Independent development and sealed-holdout gates.
5. Rollback auto-restore implementation and staging rehearsal.
6. Owner-only passive shadow after a separate approval.
7. Owner-only active treatment after two passing shadow cohorts and a separate
   approval.
8. Production only after all enterprise release gates, legal review, and
   explicit owner authorization.
