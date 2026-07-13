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
and carries additional restrictions. A future candidate needs a new review.

Primary references:

- https://github.com/pipecat-ai/smart-turn
- https://huggingface.co/pipecat-ai/smart-turn-v3
- https://docs.pipecat.ai/api-reference/server/utilities/turn-detection/smart-turn-overview
- https://huggingface.co/livekit/turn-detector/blob/main/LICENSE
- https://github.com/TEN-framework/ten-turn-detection

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
