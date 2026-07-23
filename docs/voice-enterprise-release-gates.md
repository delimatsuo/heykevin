# Voice Receptionist Enterprise Release Gates

## Current Decision

**Blocked for production.** The voice reliability stack is a draft candidate.
Local tests pass, but its exact SHA has not completed the staging certification
below. A production deploy also requires explicit owner authorization; merge or
staging success is not authorization.

The voice-only candidate must not acquire PR #76 Jobber customer memory or live
controller wiring. Customer-memory behavior and controller integration remain
separate release decisions.

## Bakeoff evidence overlay

The following gates apply to any future voice-architecture bakeoff and do not
supersede the disclosure, transcript-encryption, tenant-isolation, retention,
deletion, export, or counsel-review blockers below. A bakeoff result is not staging
or production authorization.

- Measure two clocks: common caller-harness ground-truth last-speech sample to
  first caller-side playback evidence for selection, and candidate-detected
  activity-end to first media sent for endpointing diagnosis. A transcript fragment
  is neither clock.
- Keep generated, `transport_resolved`, caller-side `caller_playback_observed`,
  `playback_inferred`, partial, clear, interruption, semantic-act, and terminal
  evidence distinct. A Twilio mark, provider completion, output text, or dashboard
  event is never a caller-heard or semantic-completion claim.
- Require hard gates for complete thoughts, answer-before-follow-up, one question,
  silence/presence/closure sequencing, safety-content completeness, repair,
  interruption, reconnect, language/accessibility fallback, and zero premature
  terminal action.
- Require pre-media ingress authentication, a dedicated nonproduction identity,
  no production reachability, privacy-setting attestation, payload-safe logs,
  one-use multi-role approval, revision/source/configuration/manifest/evaluator
  digests, and caller-side evidence for every selectable arm.
- A runtime without caller-side observation may use only a preregistered,
  cancellable conservative playback inference; transport resolution alone cannot
  arm a silence timer or authorize closure.

## Automated Voice Gates

This legacy automated/staging matrix applies only to the existing production-shaped
candidate and is non-authorizing diagnostic history. It is inapplicable to bakeoff
selection: a bakeoff must use the isolated Task-4.7 application, caller harness,
and new evaluator, and may not deploy or call `kevin-api-staging`. Staging becomes
eligible only after a winner is selected, a separate winner-specific integration
plan passes review, and the owner explicitly authorizes staging in that session.

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
| Non-interrupted response completion | 100% of unique non-interrupted `(call, turn)` starts have a completion |
| Response terminal coverage | 100% of response starts explicitly complete or are interrupted |
| Greeting length | at most 24 words |
| Inbound media ready from Twilio media start | p95 <= 2,000 ms; max <= 3,000 ms |
| First inbound audio forwarded | p95 <= 2,000 ms; max <= 3,000 ms |
| First audio from Twilio media start | p95 <= 2,500 ms; max <= 3,500 ms |
| Response first audio after caller transcript | p95 <= 1,500 ms; max <= 2,500 ms |
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
| Tool-free intake | No Jobber/CRM lookup or controller import appears on the voice-only candidate path. |
| Reconnect simulation | Old Kevin output is absent; buffered caller audio replays in order before new live frames. |
| Oversized response simulation | Queued audio stays bounded; stale output clears; one short retry is requested. |
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
