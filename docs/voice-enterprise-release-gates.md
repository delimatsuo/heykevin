# Voice Receptionist Enterprise Release Gates

## Current Decision

**Blocked for production.** The voice reliability stack is a draft candidate.
No exact SHA has completed two independent staging certification windows below.
A production deploy also requires explicit owner authorization; merge or staging
success is not authorization.

The voice-only candidate must not acquire PR #76 Jobber customer memory or live
controller wiring. Customer-memory behavior and controller integration remain
separate release decisions.

## Automated Voice Gates

Export only `voice_timing` log events and pipe them directly to the evaluator:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="kevin-api-staging" AND resource.labels.revision_name="<candidate-revision>" AND (textPayload:"voice_timing" OR jsonPayload.message:"voice_timing")' \
  --project kevin-491315 \
  --freshness=2h \
  --format=json \
  --account=deli@ellaexecutivesearch.com \
  | python scripts/evaluate_voice_release.py -
```

Replace `<candidate-revision>` with the revision reported by `/health` for the
exact candidate SHA. The evaluator prints aggregate JSON only and exits nonzero
when any gate fails. Do not save or paste the raw Cloud Logging export, and do
not run the query without the exact revision filter.

| Gate | Requirement |
|---|---:|
| Calls with first audio | at least 10 |
| Caller response turns | at least 30 |
| Deliberate barge-ins | at least 5 |
| Out-of-cohort timing events | zero; every event must have a connection marker in the export |
| Evidence integrity errors | zero missing metrics, invalid ordinals, unmatched terminals, or contradictory terminals |
| Gemini connection to first audio | 100% of sampled attempts |
| Deterministic greeting coverage | 100% of calls with first audio |
| Inbound media readiness coverage | 100% of Gemini connection attempts |
| Inbound audio forwarding coverage | 100% of Gemini connection attempts |
| Recognized caller speech coverage | 100% of Gemini connection attempts |
| Validated response-latency coverage | 100% of response turns use a calibrated caller speech-end timestamp |
| Non-interrupted response completion | 100% of unique non-interrupted `(call, turn)` starts have a completion |
| Response terminal coverage | 100% of response starts explicitly complete or are interrupted |
| Greeting length | at most 24 words |
| Inbound media ready from Twilio media start | p95 <= 2,000 ms; max <= 3,000 ms |
| First inbound audio forwarded | p95 <= 2,000 ms; max <= 3,000 ms |
| First audio from Twilio media start | p95 <= 2,500 ms; max <= 3,500 ms |
| Response first audio after validated caller speech end | p95 <= 1,500 ms; max <= 2,500 ms |
| Generated response audio | p95 <= 6,000 ms; max <= 8,000 ms |
| Interruption to Twilio clear WebSocket send completion | p95 <= 250 ms; max <= 500 ms |
| Twilio clear delivery failures | zero |
| Twilio clear mark coverage | 100% of successful clear sends have a returned mark |
| Twilio clear mark acknowledgement | p95 <= 250 ms; max <= 500 ms |
| Twilio mark send failures | zero |
| Twilio pending-mark evictions | zero |
| Inbound audio forwarding errors | zero |
| Inbound media buffer overflows | zero |
| Inbound reconnect audio buffer overflows | zero |
| Outbound audio delivery errors | zero |
| Outbound audio backlog overflows | zero |
| Reconnect result coverage | every reconnect attempt has a result |
| Reconnect failures | zero |

These are release thresholds, not aspirations. A small or incomplete sample is
a failure, not a waiver. Start the export window before the first canary call
and end it after the last call so every event belongs to a complete cohort.

`barge_in_clear.clear_ms` ends after the clear frame write and the ordered
marker write attempt. Local clear success is not a provider acknowledgement.
`twilio_clear_mark_sent` and the matching `twilio_playout_ack` ordinal prove
that the marker write completed and Twilio returned it after processing the
clear boundary. Missing, duplicate, orphaned, invalid, slow, or failed clear
markers block promotion. Twilio documents that returned marks represent played
or cleared buffered media:
<https://www.twilio.com/docs/voice/media-streams/websocket-messages>.

The native Gemini candidate requests context-window compression and session
resumption on every connection. It retains only the latest resumable handle in
memory, never logs or persists it, clears it when the call stops, and reacts to
`GoAway` with a bounded resume attempt. Failed resumption falls back to the
existing bounded transcript-context reconnect. See the official lifecycle
guidance: <https://ai.google.dev/gemini-api/docs/live-api/session-management>.
Gemini 3.1 greeting and corrective text instructions use `realtimeInput.text`;
the older `clientContent` contract remains available only for an explicit 2.5
rollback model. This follows Google's 3.1 migration guidance:
<https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview>.

Gemini input-transcription events arrive independently and have no guaranteed
ordering relative to model audio. `transcript_to_audio_ms` is diagnostic only
and cannot satisfy the response-latency gates. Until a prerecorded canary or a
calibrated shadow VAD supplies `speech_end_to_first_audio_ms`, the evaluator
must fail validated response-latency coverage.

The current candidate also runs a local WebRTC VAD over bounded 20 ms Twilio
audio frames. This path is shadow observability only: Gemini automatic activity
detection remains authoritative, no manual activity signals are sent, and a
classifier failure disables only shadow metrics. The pipeline preserves the
original Twilio ingress timestamp and emits payload-free activity boundaries,
overlap or association outcomes, and `speech_end_to_twilio_ms` only after the
first response audio chunk is accepted by the Twilio callback.

`shadow_response_start` has an independent ordinal created from the first model
audio part, so provider transcription ordering cannot hide a response from the
shadow denominator. The deterministic greeting is excluded. A timing outcome
can bind only to `last_ended_segment` and `last_ended_at`; active or newer caller
activity is reported as overlap instead of a completed endpoint.

The evaluator reports shadow delivery and total-outcome coverage, p95/max
timing, overlap, unassociated and missing outcomes, duplicate events,
contradictory outcomes, orphan outcomes, invalid ordinals, and classifier errors
under `diagnostics`. None of those fields satisfy
`validated_response_latency_coverage_rate`. Treat coverage and integrity as
calibration evidence; do not interpret a low-coverage timing percentile as
representative performance. Promotion requires a labeled prerecorded corpus to
establish speech-boundary bias and false positive/negative rates across quiet
speech, background noise, pauses, accents, languages, and Twilio frame
fragmentation before the shadow metric can be renamed or used as a gate.

The initial offline calibration fixture is the pinned, MIT-licensed upstream
`py-webrtcvad` sample under `tests/fixtures/voice_vad/`. After a Twilio mu-law
round trip, the current tracker ends 20 ms after the upstream mode-2 label but
starts 140 ms early. That fixture makes codec bias reproducible; one clean
English sample is not the multi-condition corpus required for promotion.

The default provider replay now uses a v2 multi-source manifest with pinned
FLEURS English US and Latin American Spanish test rows. Both fixtures are
resampled to 8 kHz PCM, manually labeled, trimmed to 500 ms before and after
speech, checksum-pinned, and replayed through the Twilio mu-law path. No
transcript field is stored. This is the first independent bilingual corpus
increment, not a claim of broad language or speaker qualification; its paired
results must be collected before it can affect a release decision.

Every replay report includes the raw manifest SHA-256 and a semantic corpus
SHA-256 over ordered case names, source hashes, speech boundaries, transforms,
and frame patterns. Archive both hashes with aggregate results; a report with a
missing or unexpected identity is invalid evidence even when its latency gates
pass. `scripts/build_fleurs_voice_fixtures.py` also reproduces the committed
fixture hashes from the pinned dataset revision and dependency versions.

Ingress also emits one `inbound_audio_stream_gap` event after a full second
without Twilio media and one `inbound_audio_stream_resumed` event if media
returns. These are payload-free diagnostics and do not send Gemini
`audioStreamEnd`. [Google documents `audioStreamEnd`](https://ai.google.dev/api/live)
as the automatic-VAD signal for a paused audio stream; using it is a separate
control experiment that requires repeated gap/endpoint correlation first.

At pipeline shutdown, `inbound_audio_forwarding_summary` reports the count of
ordinary live frames successfully sent to Gemini, an all-frame histogram-based
`p95_upper_bound_ms`, and the exact maximum from the original Twilio ingress
timestamp through WebSocket send completion. The fixed histogram bounds memory
for long calls and intentionally reports an upper bound instead of claiming
false millisecond precision. Reconnect-buffer replays do not retain a source
timestamp and are excluded; their chunks and duration remain separately
accounted by `inbound_reconnect_audio_replayed`. Forwarding summaries are
diagnostic until a cohort establishes a release threshold.

The latest controlled staging probes recorded no one-second ingress gaps or
transport errors, but reproduced a two-turn overlap and measured 7,469 ms from
the local completed speech endpoint to Twilio delivery in a single-turn
control. This rejects `audioStreamEnd` as the next experiment but does not, by
itself, assign all delay to server VAD. A provider-only three-trial synthetic
probe measured 1,993-3,806 ms on the current Gemini 2.5 `latest` alias and
1,519-1,547 ms on `gemini-3.1-flash-live-preview`. Google identifies 3.1 as the
low-latency migration target, but this small unpinned probe is discovery
evidence, not release qualification. A subsequent explicit-model, paired six-case
smoke used identical mu-law audio in automatic and ground-truth manual arms.
Gemini 2.5 measured 5,195 ms automatic and 3,753 ms manual at the small-sample
p95/max, so endpoint control alone still failed. Gemini 3.1 measured 1,936 ms
automatic and 1,342 ms manual with complete terminals and no manual
interruptions or premature responses. After correcting the latency anchor to
the actual send completion of the chunk containing labeled speech end, the
30-pair seed-29 run measured 2,094/2,309 ms automatic p95/max and
1,408/1,544 ms manual p95/max with complete coverage. Independent seed 41 did
not reproduce the pass: automatic measured 2,059/2,099 ms with one error and
96.67% terminal/latency coverage, while manual measured 1,593/1,809 ms and
missed the 1,500 ms p95 gate. The worse seed governs, so neither a model-only
3.1 treatment nor live manual endpointing is qualified. Those runs used six
transformations of one English source. The bilingual FLEURS replay below now
provides the governing provider evidence.
`gemini-3.1-flash-live-preview` is an explicit
non-`latest` model ID, but it is still a mutable preview rather than an
immutable dated release; all qualification evidence must therefore be rerun
immediately before any later staging decision.

Observation-only revision `kevin-api-staging-00076-nug` then repeated the fixed
two-turn and single-turn staging probes without changing the Gemini model or
VAD. Across both calls, receipt-to-Gemini-send p95 was bounded at 5 ms, exact
maximum was 155 ms, and there were no one-second ingress gaps, reconnects, or
audio errors. The two-turn probe delivered both responses without overlap at
1,845 ms and 2,349 ms from local speech end, while the single-turn control took
5,271 ms. Prior unchanged-behavior probes did overlap. The large cross-call
variance and negligible forwarding lag rule out the application ingress queue
as the dominant cause; the voice candidate remains blocked on provider/model
turn reliability and calibrated endpoint control.
[Gemini 3.1 migration guidance](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview)

The pinned bilingual FLEURS replay then ran against Gemini 3.1 with manifest
SHA-256 `ae84a0428697119bc794e70d661bfda94e3cbc7fc5f396283b72e29995790e5c`
and semantic corpus SHA-256
`b0c23e5a964427d7e51193913c760e39143d957a0660e9fe8ae3ad47ea515636`.
Seed 29 passed all gates: both arms completed 30/30 attempts with no errors,
premature responses, or manual interruptions; manual speech-end p95/max was
1,409/1,435 ms and activity-end p95/max was 898/915 ms. Seed 41 did not
reproduce the pass: the automatic arm had one provider timeout and 96.67%
completion, while manual speech-end p95/max was 1,503/1,553 ms. Manual
activity-end p95/max was 983/1,035 ms with complete coverage and no manual
errors, premature responses, or interruptions. The worse seed governs. This
stronger corpus narrows the manual-endpoint uncertainty but still does not
qualify a live model or activity-control change.

The deterministic local mode-2 WebRTC tracker also fails the same corpus as a
control source. It preserved full start/final-boundary coverage with start
absolute error p95/max of 40/40 ms, final-end absolute error p95/max of 80/80
ms, and a 300 ms confirmation delay. However, zero of six transformed cases
remained one segment: the tracker emitted three pre-roll false starts and nine
premature end events across natural within-turn pauses. A bounded diagnostic
grid across WebRTC modes 0-3, 3-15 speech-confirmation frames, and 300-1,500 ms
end silence found no setting that passed even both clean sources within the
150 ms boundary and 500 ms confirmation gates. Therefore the shadow tracker
must not send Gemini manual activity signals.

A one-trial provider-only probe of the legacy Deepgram `speech_final` settings
was also unsuitable for direct control. The English clean fixture produced one
final about 6.4 seconds before the labeled endpoint and no post-end final; the
Spanish clean fixture produced two post-end finals. No transcript content was
printed or stored. This is discovery evidence, not a calibrated Deepgram
benchmark, but it blocks wiring `speech_final` into Gemini as the next shortcut.

`gemini_usage_snapshot` contains cumulative numeric counters for one Gemini
session. It is payload-free, may be duplicated by the provider, and must not be
attributed to an individual response turn. The 120-token output limit is an
experiment to reduce long responses; it is not a duration guarantee and does
not waive the generated-audio or audible-completeness gates.

## Staging Call Matrix

Use the exact PR SHA reported by `/health`. Complete at least ten calls and 30
caller turns across this matrix:

| Scenario | Required evidence |
|---|---|
| Business-hours greeting | Exact AI/transcription disclosure; no extra model-written preamble. |
| After-hours greeting | Disclosure and closed status in at most 24 words. |
| Personal greeting | Disclosure identifies Kevin as the owner's AI assistant. |
| Fast caller start | Caller speech is captured in order during authentication/provider startup; logs contain first-inbound-forwarded and first-caller-transcript markers; Kevin can be interrupted during the greeting. |
| Five deliberate interruptions | Every Twilio clear has a unique ordinal, is acknowledged and in budget, and any started response has an interrupted terminal marker; no stale words or transcript side effects survive. |
| Short answers and corrections | Kevin accepts corrections and does not repeat answered questions. |
| Background noise and pauses | No false hangup, duplicate prompt, or sustained talk-over. |
| Shadow VAD calibration | Compare labeled caller speech boundaries with shadow delivery, overlap, unassociated, and error diagnostics; do not alter Gemini activity control. |
| Tool-free intake | No Jobber/CRM lookup or controller import appears on the voice-only candidate path. |
| Reconnect simulation | Provider handle resumes without duplicate transcript context; `GoAway` reconnects; failed resumption cold-falls back; old Kevin output is absent; buffered caller audio replays in order before new live frames. |
| Oversized response simulation | Queued audio stays bounded; stale output clears; one short retry is requested. |
| Bounded normal responses | Generated audio stays within budget and no response ends mid-sentence. |
| Inbound startup overflow simulation | Stream closes, no caller audio is logged, and the release evaluator fails. |
| Normal hangup | Final audio drains before call completion; post-call processing runs once. |

For each call, record only the scenario, pass/fail outcome, deployed SHA, and
aggregate timing report. Do not record caller phone numbers or transcript text
in the release artifact.

## Required Automated Verification

- Full unit suite passes on Python 3.12.
- Ruff and `git diff --check` pass.
- The configured Gemini model is an explicit non-`latest` model ID.
- Credential-shaped and full-phone-shaped additions scan clean.
- `app/services/gemini_pipeline.py` and `app/services/voice_pipeline.py` do not
  import controller modules in the voice-only release candidate.
- Controller replay must pass multi-turn correction, interruption, repeated
  question, private-memory, tool-timeout, duplicate-side-effect, reconnect, and
  multilingual fixtures before live wiring.
- GitHub CI passes for the exact candidate SHA.

## Privacy And Compliance Gates

- The default greeting discloses that Kevin is an AI assistant and that the
  call may be transcribed and summarized. Counsel must approve the wording and
  jurisdiction policy; code coverage is not legal approval.
- Logs contain no transcript text, audio payload, full phone, customer record,
  OAuth code, bearer token, integration token, or provider API key.
- Production must have a valid `TRANSCRIPT_ENCRYPTION_KEY` and must fail closed
  if it is absent or invalid. The current plaintext fallback in
  `app/db/calls.py` remains a production blocker until separately hardened and
  configured.
- Cloud Run secret delivery and access must be reviewed against
  `docs/security/phase0-release-readiness.md`; service metadata must never be
  copied into release notes or tickets.
- Tenant-isolation, retention, deletion, and export tests remain release gates
  for stored calls and derived job records.

## Rollout And Rollback

1. Review and land the reliability stack in dependency order only after exact
   head CI and staging certification.
2. Deploy the exact candidate SHA to staging through the protected staging
   workflow.
3. Capture the current staging revision before testing.
4. Run the full staging matrix and evaluator twice in separate call windows.
5. Rehearse `.github/workflows/rollback.yml` against staging and verify
   `/health` reports the previous SHA.
6. Keep PR #76 customer memory and controller integration out of this release
   decision.
7. Production remains blocked until all gates pass and the owner explicitly
   authorizes a production release window.

Immediate rollback triggers are inbound-media-ready, first-inbound-forwarded,
or first-audio p95 regression; missing caller-transcript or response-completion
coverage; inbound media overflow; missed caller speech; stale post-interruption
audio; duplicate side effects; sensitive log content; transcript encryption
failure; or any cross-tenant behavior.
