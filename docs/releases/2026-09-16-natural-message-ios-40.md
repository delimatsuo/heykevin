# Natural message taking — backend rollout and iOS 1.3.1 (40)

## Release state

The owner approved staging verification, production rollout and a new internal
TestFlight build on September 16, 2026. Apple has processed **1.3.1 (40)** and
made it available to the existing internal **QA** group. Staging and production
verification passed. Production revision **`kevin-api-00271-q4j`** serves the
approved backend at 100% traffic, verified at **20:07:42Z**. The deployed source
includes the new message timing; its audible behavior still needs the owner's
physical-call check.

This approval does not include a public App Store submission or external beta
submission. Public **1.3.0 (39)** remains the released iOS baseline. No release
setting, feature flag, account, credential or telephony configuration was changed.
The owner subsequently confirmed build 40 was installed and reported the
confirmation-warning regression below. Full physical-call acceptance remains
open.

## Product change and evidence boundary

The owner's build 39 call demonstrated working notification actions and streamed
conversation text, but unclear buttons and an abrupt Take a message response.
The [accepted plan](../superpowers/plans/2026-09-16-natural-message-taking.md)
and [PR #253](https://github.com/delimatsuo/heykevin/pull/253) implement:

- Primary phone treatment for **Pick up** and a neutral message treatment for
  **Take a message** in app-owned controls. System notification actions retain
  their platform layout, with the existing localized titles and action icons.
- Finish Kevin's current speech before responding to an accepted Take a message
  instruction. Omit an availability offer that has not begun.
- If Kevin already offered to check availability, pause once for three seconds
  after the later of accepting the instruction and finishing current speech,
  then use the configured owner name in the unavailable/message response.
- Preserve exact-call/account ownership, caller continuation and interruption,
  retry and acknowledgement safety, and the existing unanswered 30-second wait.

ConversationRelay cannot selectively withdraw an availability offer already
submitted to its provider queue. Queue order and the silent clip are tested;
actual audible timing and that queued-offer edge require a physical call. Legacy
Voice retains its pre-existing fixed-English fallback. These limitations and
the deferred language-parity work are recorded in the plan and PRD.

No VoiceOver work or qualification is required for this owner-requested check.
Deferred archive, callback, text reply, follow-up and Dispatch work remains out
of scope.

## Frozen source and review

| Identity | Value |
|---|---|
| Implementation PR / branch HEAD | #253 / `90ce4b1fe6590ebd7bf0683f6b11a612df28f6f0` |
| Merged implementation / backend candidate | `eee7d42682bf4222ebee59f52e92082f5f9e13cb` |
| Implementation tree | `212dc50ede72c24d7b904d1d40cbd6c0739260d7` |
| Packaged source | `0c3eaacbdf5cbd765105b2bf68cf5294e1b8891a` |
| Packaged source tree | `a3f4b3f2c123710f2aecfa327dbef6f4167afef6` |
| Packaged iOS tree | `7035350f3ca9d4bc1aff2082aaaacfd14c6c6634` |
| App source tree (`ios/Kevin`) | `76639f808f4ecf2f130eba704eba1476cef50c4b` |
| Release branch | `codex/message-release-build40` |
| Release worktree | `<clone>/.worktrees/message-release-build40` |

The packaged commit changes only `ios/project.yml` and the generated Xcode
project's version/build values to **1.3.1 (40)**. Its app source tree equals the
merged, reviewed implementation. This release record is a documentation-only
follow-up and is not part of the uploaded binary.

Implementation verification: **492 focused backend tests**, behavioral mutation
checks, **22 iOS CallActionTests and three targeted native UI tests** passed.
Exact implementation-head [CI 35139244003](https://github.com/delimatsuo/heykevin/actions/runs/35139244003)
passed all nine required jobs and **8,005 backend tests**. Final independent
source review and automatic Codex review were clean before PR #253 merged.

Exact packaged-source [CI 35140895777](https://github.com/delimatsuo/heykevin/actions/runs/35140895777)
also passed all nine required jobs and **8,005 backend tests**. Independent
metadata review passed, and automatic Codex review completed without findings.
The documentation follow-up at `f4a478914b8edf124f70291b2973b13f85db6b04`
passed all nine required jobs in
[CI 35143249598](https://github.com/delimatsuo/heykevin/actions/runs/35143249598)
and automatic Codex review without findings. Independent release-record review
also passed. [PR #254](https://github.com/delimatsuo/heykevin/pull/254) merged at
**19:55:42Z** as `16ace6ce36cfafcdffd3a12de514b160fe4cf831`, with the same tree
as that reviewed head and the same iOS tree as the uploaded package. No full
local suite was repeated for the version or documentation changes.

## Package and Apple delivery

All archive/export commands used the required `xcodebuild-external` wrapper and
one explicit Xcode project. Unrelated Xcode work was allowed to finish; no lock
was bypassed and no other project's process was killed. The final successful
wrapper result directories under `/Volumes/Extreme Pro/XcodeStorage/Results/` are:

- `kevin-message-40-archive-20260916T193935Z-33062`
- `kevin-message-40-export-20260916T194015Z-34786`

| Package identity | Verified value |
|---|---|
| App / bundle / team | `6761427495` / `com.kevin.callscreen` / `3FLG8W6B95` |
| Scheme / configuration / SDK | Kevin / Release / iPhoneOS 26.0, Xcode `17A400` |
| IPA size | 6,611,872 bytes |
| IPA SHA-256 | `794ff5da1f9849f179e20ef5f89d41e130ef731bcecbd26c252f3f24248616b1` |
| Archive / export / dSYM UUID | `1E202E9F-06FD-3A9B-85A3-A126C8756BE0` |
| Distribution profile | `639cf43f-e398-42cb-9927-3b610eb59e4c`, expires July 15, 2027 |
| Twilio Voice | `6.13.6 (179124)` |
| Backend binding | `https://kevin-api-752910912062.us-central1.run.app`, production |
| Delivery UUID / Apple build ID | `168de1a5-c501-42d5-93f1-8aeeba55b68c` |
| Prerelease version ID | `98a95d4b-5c43-439c-a56f-a7c7d5d73a99` |
| Internal QA group | `ce552be4-c362-4021-9a90-b77d2e910d15` |
| English test-notes ID | `2984b8e5-7302-48c5-888a-026b4930c58e` |

Master and independent package review passed: strict/deep signatures; matching
archive/export file-backed Mach-O sections (35); all 18 packaged files matching
ZIP/extraction; correct app identity and production backend; distribution
signing; production APNs, Time Sensitive and Apple Sign In entitlements;
`get-task-allow=false`; no Critical Alert entitlement; correct background modes
and purpose strings; no non-exempt encryption; Release fixture activation
disabled. All 272 Spanish and 272 Brazilian Portuguese compiled translations
match their source catalog, without duplicate catalog keys.

Apple validation passed at **19:41:45Z**. Exactly one upload succeeded at
**19:45:28Z**. Log SHA-256 values:

- Validation: `dcda0af292808dffcde25304a1ea9f693015ff7235669ef3987a79e97b3857e7`
- Upload: `36a6e59760818ddc3db15f2cc848298b1afa6d7b86fdd6551df420c48a95b2ed`

At **19:47:40Z**, Apple returned build `40`, prerelease `1.3.1`,
`processingState=VALID`, `internalBuildState=IN_BETA_TESTING`, unexpired, and
`usesNonExemptEncryption=false`. At **19:48:58Z**, the QA group's build
relationship explicitly included build 40; it remains an internal group with
access to all builds. English What to Test notes were written and read back.
After production verification, the notes were updated at **20:08:28Z** to remove
the instruction to await rollout confirmation; Apple still reported `VALID` and
`IN_BETA_TESTING`. The notes cover the button and message-timing phone checks.
External beta remains
`READY_FOR_BETA_SUBMISSION`; no external review or App Store version was submitted.

Temporary package files and raw logs were removed after successful delivery,
independent review and preservation of this durable text record. No long-term
binary archive was approved.

## Backend rollout

The backend candidate is exactly
`eee7d42682bf4222ebee59f52e92082f5f9e13cb`. Compared with the prior production
baseline, it changes the transition engines and durable consumer plus the pause
WAV, with no migration, dependency, Docker, workflow or default-configuration
change. Runtime safety flags and environment bindings are preserved.

**Staging — passed.** [Run 35140358345](https://github.com/delimatsuo/heykevin/actions/runs/35140358345)
was dispatched at **19:24:21Z**, completed at **19:31:38Z**, and passed all ten
applicable jobs. Revision **`kevin-api-staging-00171-sir`** receives 100% traffic.
Canonical health identifies the candidate SHA and staging environment. Anonymous
health/admin-page/CSS/JS smoke checks passed; authenticated admin checks were
deliberately omitted. Firestore/RTDB remain bound to `kevin-staging-491315`,
APNs/App Store remain sandbox, and promotion/shadow flags remain false.

The canonical URL
`https://kevin-api-staging-l63rergg7a-uc.a.run.app/static/audio/message-pause-3s.wav`
returned HTTP 200 and **48,044 bytes**, SHA-256
`59db6dfb709393b7aab8efcc2b395df4def43f1c976d8b157bb9009dbacdc0ec`.
This checks the actual hostname used by Relay; the existing general smoke script
does not cover this audio asset. Retrieval alone does not establish audible timing.

**Production — passed.** [Run 35141337468](https://github.com/delimatsuo/heykevin/actions/runs/35141337468)
was dispatched at **19:34:10Z** from main at the exact candidate SHA. All nine
validation jobs passed. The job initially waited for Deli under the standing
owner-only approval rule. The owner then explicitly instructed: "I approved it.
You can go ahead and click." This newer instruction delegated approval of this
existing deployment only. Codex submitted that approval through the existing
authenticated GitHub CLI. Review history records state `approved`, actor
`delimatsuo`, environment `production` (`13924929328`), and the comment:
"Deli explicitly approved this deployment and instructed Codex to perform this
approval in the current task." The production job started at **20:00:23Z**.
No environment protection setting was changed and no duplicate dispatch was
created. The standing rule in `AGENTS.md` was not edited.

The workflow completed successfully at **20:06:43Z**, with all ten applicable
jobs passing. At **20:07:42Z**, Cloud Run reported revision
**`kevin-api-00271-q4j`** ready and receiving **100% traffic**. Both the configured
canonical URL `https://kevin-api-752910912062.us-central1.run.app/health` and
Cloud Run's alternate URL `https://kevin-api-l63rergg7a-uc.a.run.app/health`
returned `status=ok`, production environment, that revision and exact SHA
**`eee7d42682bf4222ebee59f52e92082f5f9e13cb`**. Anonymous health/admin-page/CSS/JS
smoke passed; authenticated admin checks were omitted.

The following runtime values match the prior production revision:

| Runtime field | Verified value |
|---|---|
| `ENVIRONMENT` / `APPSTORE_ENVIRONMENT` | `production` / `production` |
| `APNS_SANDBOX` | `false` |
| `CLOUD_RUN_URL` | `https://kevin-api-752910912062.us-central1.run.app` |
| `FIRESTORE_PROJECT_ID` | `kevin-491315` |
| `FIREBASE_DATABASE_URL` | `https://kevin-491315-rtdb.firebaseio.com` |
| `SUBSCRIPTION_PROMOTIONAL_OFFERS_ENABLED` | `false` |
| `RECEPTIONIST_OBSERVATION_SHADOW_ENABLED` | `false` |
| `LAPSED_NUMBER_RELEASE_ENABLED` | `true` |

Health continues to report Gemini staging safety controls false, model tools
true and automatic terminal actions true. The canonical production
`/static/audio/message-pause-3s.wav` returned HTTP 200, **48,044 bytes**, and the
same SHA-256 as staging:
`59db6dfb709393b7aab8efcc2b395df4def43f1c976d8b157bb9009dbacdc0ec`.

The prior production revision **`kevin-api-00270-l9s`**, SHA
**`7377c7ba402297625dec5de97a2250f50b1c8013`**, remains a rollback reference;
no rollback was performed. Direct Cloud Build listing was denied to the local
account. No permission change was requested: the successful workflow, Cloud Run
revision/traffic readback, runtime comparison and public health/static checks
provide the deployment evidence recorded here.

## Owner phone acceptance — open

The owner confirmed **Hey Kevin 1.3.1 (40)** was installed from TestFlight.
Production rollout has been verified.

During the September 16 call, the owner tapped **Take a message**, heard Kevin's
normal message response, and saw **"Outcome not confirmed. Check status before
choosing another action."** The supplied screen showed **Taking message** and
Dismiss, with no usable Check status control. This is a reported confirmation
regression, tracked by the [bounded repair plan](../superpowers/plans/2026-09-16-message-confirmation-state.md).
The report does not establish the exact three-second pause, absence of speech
interruption, or completion of the other scenarios below. Caller details and
the screenshot are intentionally excluded from repository evidence.

These focused button/message-transition checks supplement the remaining N1/N3
acceptance in the [PRD](../../kevin-prd.md#n1--finish-the-screening-notification-enhancement).
They do not replace its notification preview, banner navigation, old/ended-call
alert, urgent-preference persistence, notification-failure or other open rows.
Passing the five checks below does not close those separate requirements.

With Focus off and a second phone:

- [ ] Notification actions and app-owned buttons are clear; **Pick up** connects
      the intended call with audio in both directions.
- [ ] Tap **Take a message** while Kevin speaks. Current speech finishes, then
      Kevin naturally says the configured owner is unavailable and offers to
      take a message. An unstarted availability offer is omitted.
- [ ] After Kevin already offered to check availability, tap **Take a message**.
      There is one three-second pause after both the accepted tap and current
      speech finish, followed by the unavailable/message response.
- [ ] Continue as the caller. Kevin listens and takes the message without a
      repeated announcement or lost caller turn. Repeat/late taps do not affect
      another call, and hangup ends the pending transition.
- [ ] An unanswered call retains the existing 30-second wait with no extra
      three-second message-transition delay.

These rows remain open until owner results are recorded. Earlier build 39 call
observations, simulator tests, CI, Apple processing and anonymous provider checks
do not establish build 40 physical-call acceptance.

## Actions cost and routing

Repository and personal billing owner: **delimatsuo/heykevin**, **delimatsuo**.
The repository is public; required jobs use bounded standard Ubuntu runners and
PR cancellation. Each PR runs seven Python shards, Python quality and the stable
fail-closed **Test** aggregator. A deployment adds one applicable deploy job.
No hosted macOS job, paid runner migration, duplicate deployment or rerun was
started. Expected chargeable Actions cost is **$0**. September paid Actions
usage observed during approval preflight was **$70.313812188**, below the owner's
$80 normal portfolio stop threshold; this release uses no paid allocation.

Master owns architecture, release decisions, evidence and Git. The literal
version edit used `agy gemini-3.7-flash-low` headlessly: 28,347 input tokens,
374 output tokens, 28,721 total, zero retries. Master regenerated the Xcode
project. Independent staff review checked source, staging readiness and the
actual signed package. Later documentation follow-ups also require automatic
Codex review and exact-HEAD CI before merge.
