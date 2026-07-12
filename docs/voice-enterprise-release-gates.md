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
| Interruption to Twilio clear completion | p95 <= 250 ms; max <= 500 ms |
| Twilio clear delivery failures | zero |
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
evidence, not release qualification. The next model/VAD comparison must use a
pinned dated control, identical paced audio, ground-truth boundaries, and at
least 30 randomized interleaved turns before any live control change.
[Gemini 3.1 migration guidance](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview)

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
| Reconnect simulation | Old Kevin output is absent; buffered caller audio replays in order before new live frames. |
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
