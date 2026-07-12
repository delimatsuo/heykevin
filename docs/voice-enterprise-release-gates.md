# Voice Receptionist Enterprise Release Gates

## Current Decision

**Blocked for production.** PR #79 is a draft reliability candidate. Local tests
pass, but the exact SHA has not completed the staging certification below. A
production deploy also requires explicit owner authorization; merge or staging
success is not authorization.

PR #79 is based directly on `main`. It must not acquire PR #76 Jobber customer
memory or live PR #77 controller wiring. The observed 0.83-second Jobber timeout
is not part of the reliability release and must not return on this branch.

## Automated Voice Gates

Export only `voice_timing` log events and pipe them directly to the evaluator:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="kevin-api-staging" AND (textPayload:"voice_timing" OR jsonPayload.message:"voice_timing")' \
  --project kevin-491315 \
  --freshness=2h \
  --format=json \
  --account=deli@ellaexecutivesearch.com \
  | python scripts/evaluate_voice_release.py -
```

The evaluator prints aggregate JSON only and exits nonzero when any gate fails.
Do not save or paste the raw Cloud Logging export.

| Gate | Requirement |
|---|---:|
| Calls with first audio | at least 10 |
| Caller response turns | at least 30 |
| Deliberate barge-ins | at least 5 |
| Gemini connection to first audio | 100% of sampled attempts |
| Deterministic greeting coverage | 100% of calls with first audio |
| Greeting length | at most 24 words |
| First audio from Twilio media start | p95 <= 2,500 ms; max <= 3,500 ms |
| Response first audio after caller transcript | p95 <= 1,500 ms; max <= 2,500 ms |
| Generated response audio | p95 <= 6,000 ms; max <= 8,000 ms |
| Interruption to Twilio clear completion | p95 <= 250 ms; max <= 500 ms |
| Twilio clear delivery failures | zero |
| Inbound audio forwarding errors | zero |
| Outbound audio delivery errors | zero |
| Outbound audio backlog overflows | zero |
| Reconnect result coverage | every reconnect attempt has a result |
| Reconnect failures | zero |

These are release thresholds, not aspirations. A small or incomplete sample is
a failure, not a waiver.

## Staging Call Matrix

Use the exact PR SHA reported by `/health`. Complete at least ten calls and 30
caller turns across this matrix:

| Scenario | Required evidence |
|---|---|
| Business-hours greeting | Exact AI/transcription disclosure; no extra model-written preamble. |
| After-hours greeting | Disclosure and closed status in at most 24 words. |
| Personal greeting | Disclosure identifies Kevin as the owner's AI assistant. |
| Fast caller start | Caller speech is captured; Kevin does not inject a silence prompt. |
| Five deliberate interruptions | Every Twilio clear is acknowledged and in budget; no stale words or transcript side effects survive. |
| Short answers and corrections | Kevin accepts corrections and does not repeat answered questions. |
| Background noise and pauses | No false hangup, duplicate prompt, or sustained talk-over. |
| Tool-free intake | No Jobber/CRM lookup or controller import appears on the PR #79 path. |
| Reconnect simulation | Old queued audio and partial Kevin text are absent after recovery. |
| Oversized response simulation | Queued audio stays bounded; stale output clears; one short retry is requested. |
| Normal hangup | Final audio drains before call completion; post-call processing runs once. |

For each call, record only the scenario, pass/fail outcome, deployed SHA, and
aggregate timing report. Do not record caller phone numbers or transcript text
in the release artifact.

## Required Automated Verification

- Full unit suite passes on Python 3.12.
- Ruff and `git diff --check` pass.
- Credential-shaped and full-phone-shaped additions scan clean.
- `app/services/gemini_pipeline.py` and `app/services/voice_pipeline.py` do not
  import PR #77 controller modules until a separate feature-flagged live-wiring
  plan is approved.
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

1. Merge PR #79 only after review and CI; do not close stacked PR #78 until the
   standalone SHA has passed staging.
2. Deploy the exact candidate SHA to staging through the protected staging
   workflow.
3. Capture the current staging revision before testing.
4. Run the full staging matrix and evaluator twice in separate call windows.
5. Rehearse `.github/workflows/rollback.yml` against staging and verify
   `/health` reports the previous SHA.
6. Keep PR #76 and PR #77 out of this release decision.
7. Production remains blocked until all gates pass and the owner explicitly
   authorizes a production release window.

Immediate rollback triggers are first-audio p95 regression, missed caller
speech, stale post-interruption audio, duplicate side effects, sensitive log
content, transcript encryption failure, or any cross-tenant behavior.
