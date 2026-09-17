# Message confirmation repair — iOS 1.3.1 (41)

**Delivery complete:** Apple reports **VALID / IN_BETA_TESTING** for build 41,
with explicit availability in the existing internal **QA** group, verified
September 16, 2026 at **22:07:24Z**. Owner installation and retest remain open.

**Later App Store submission:** At the owner's subsequent request, this same
build was submitted with six updated screenshots at
**2026-09-17T03:16:04.335Z**. Apple reports **WAITING_FOR_REVIEW**. See the
[submission record](2026-09-16-app-store-ios-41.md); no second binary upload was made.

## Scope and authorization

The owner reported that **Take a message** on TestFlight build 40 showed
"Outcome not confirmed" while Kevin's spoken message response was normal.
The [repair plan](../superpowers/plans/2026-09-16-message-confirmation-state.md)
records the bounded iOS fix and its verification. Caller details and the supplied
screenshot are excluded from repository evidence.

The owner explicitly approved packaging, signing and one upload of **1.3.1 (41)**
to the existing internal TestFlight **QA** group on September 16, 2026. This
release changes no backend code, deployment, flag, telephony setting or public
App Store release. Public **1.3.0 (39)** remains the observed release baseline.

## Behavior

A valid, exact message-request response now stays pending without the false
unknown-outcome warning. When live status reports Taking message, the app checks
the retained operation before treating it as acknowledged. Genuine uncertainty
keeps **Check status** available beside Dismiss on both live-call surfaces.
Account/call ownership, duplicate prevention, passive dismissal and explicit
pickup recovery remain intact.

The backend's natural speech transition was already deployed for build 40.
This iOS repair does not change it. Physical-call confirmation, speech completion,
the conditional three-second pause and the separate N1/N3 acceptance rows remain
open. VoiceOver qualification remains deferred by the owner.

## Frozen source and verification

| Identity | Value |
|---|---|
| Implementation PR | [#256](https://github.com/delimatsuo/heykevin/pull/256) |
| Merged implementation | `c39926cfe667b0e70c3437bc764a99cb011cae5a` |
| Implementation tree | `3deb9f67be04d5d5bf8bb77837b90c8e32e70290` |
| Packaged source | `3865883387e0a0ba555cd6fd187a6ab60c0caa28` |
| Packaged source tree | `b6a7ca06773aebedb0ec4133d4bbb38fd3ae8f91` |
| Packaged iOS tree | `573c5b0d20a46b907744d830a8b4d1dc66a112da` |
| App source tree | `d06a9398c0dcc1d5dc5c96ed7b5370be052350b0` |
| Backend app tree, unchanged from deployed source | `82650009750499b67c9c6d6ae16cebcdbac09539` |
| Release branch / PR | `codex/message-confirmation-build41` / [#257](https://github.com/delimatsuo/heykevin/pull/257) |

The packaged commit contains only one YAML and three generated Xcode project
build-number substitutions, 40 to 41. Marketing version stays 1.3.1. A deterministic
before/after audit, XcodeGen regeneration and `git diff --check` passed. App source,
tests, export settings and backend are unchanged from reviewed main.

The implementation evidence includes **46 iOS tests**, **three native UI smoke
tests**, **141 backend contract tests**, and **five detected mutations**. Restored
source passed the same 46 tests again. Independent review directly re-read the
restored 46-test and three-test UI result bundles and verified source identity.
The UI smoke tests cover navigation and fixture isolation; the new recovery
control's unknown-state behavior was reviewed in source, not physically tested.

Implementation [CI 35153864532](https://github.com/delimatsuo/heykevin/actions/runs/35153864532)
passed all nine required jobs on final source head
`5d014a037f4f9df5eafa51f4321485883337d0ce`; automatic Codex review completed
without findings. Package-head
[CI 35155281665](https://github.com/delimatsuo/heykevin/actions/runs/35155281665)
passed all nine required jobs on packaged source `3865883...`. Automatic Codex
review completed at **22:00:02Z**, with no findings and a positive completion
reaction. Full local suites were not repeated for metadata or documentation.

## Signed package

Archive and export used the required Xcode wrapper, explicit Kevin project,
Release configuration and generic iOS destination. Managed result directories:

- `kevin-message-41-archive-20260916T215603Z-26107`
- `kevin-message-41-export-20260916T215643Z-26684`

| Package identity | Verified value |
|---|---|
| App / bundle / team | `6761427495` / `com.kevin.callscreen` / `3FLG8W6B95` |
| Version / build | **1.3.1 (41)** |
| IPA size | 6,611,093 bytes |
| IPA SHA-256 | `611f5ea9e0d520ce218c6d2d6c8d17e5fef683c6648dfdb6c3e490d7491292e4` |
| Archive / export / dSYM UUID | `F60BC02F-C597-31CD-9B5A-677116164C88` |
| Distribution profile | `639cf43f-e398-42cb-9927-3b610eb59e4c`, expires July 15, 2027 |
| SDK / Xcode | iPhoneOS 26.0 / `17A400` |
| Twilio Voice | `6.13.6 (179124)` |
| Backend binding | `https://kevin-api-752910912062.us-central1.run.app`, production |

Master and independent package checks passed strict/deep signatures, all 35
file-backed app Mach-O sections
and all 31 Twilio file-backed sections matching between archive and export,
all 21 ZIP files matching extraction (including 18 app files),
production APNs, Time Sensitive and Apple Sign In entitlements,
`get-task-allow=false`, no Critical Alert entitlement, correct background modes
and purpose strings, and no non-exempt encryption. Release compile provenance
points to the frozen worktree with optimization and no DEBUG/STAGING defines;
fixture activation is DEBUG-only, and neither fixture activation environment-key
string occurs in the exported binary. App and Twilio dSYM identities match.
The 272 Spanish and 272 Brazilian Portuguese
compiled translations match the source catalog without duplicate catalog keys.

Apple validation passed at **21:58:28Z**. Validation log SHA-256:
`66f3c3e16168fe709206b0601fd8518f24b7e49fac910582e768b6528555a65e`.

## Apple delivery

Exactly one upload succeeded at **22:04:07Z**, delivery UUID
`d0326647-8465-4757-8679-58186383a390`, for the IPA hash above.
Upload log SHA-256:
`6998241c68fef7f98225685d3c9b75e55952841cfa6cf0a36c836f78a647ba82`.
At **22:07:24Z**, Apple returned build **41**, prerelease **1.3.1**,
`processingState=VALID`, `internalBuildState=IN_BETA_TESTING`, unexpired and
`usesNonExemptEncryption=false`. The existing internal QA group explicitly
included this build. English What to Test notes were written and read back at
**22:07:39Z**, covering the warning regression, speech timing, caller continuation
and pickup. At that delivery stage, no external beta or public App Store
submission was made. The later public submission is recorded above.

| Apple identity | Value |
|---|---|
| Build ID / delivery UUID | `d0326647-8465-4757-8679-58186383a390` |
| Prerelease version ID | `98a95d4b-5c43-439c-a56f-a7c7d5d73a99` |
| Internal QA group | `ce552be4-c362-4021-9a90-b77d2e910d15` |
| English test-notes ID | `d16fd5da-325f-4644-9bb5-703cd05bee84` |
| External beta state, unchanged | `READY_FOR_BETA_SUBMISSION` |

Temporary binaries and raw logs are retained only during release verification.
After independent evidence review, remove that exact temporary package directory;
this text record retains provenance. No long-term binary archive is approved.

## Owner retest — open

- [ ] Install **1.3.1 (41)** from TestFlight and record the installed build.
- [ ] With Focus off, call from an owner-controlled second phone and tap
      **Take a message**. Pending/accepted status is truthful; the reported false
      warning does not appear while Kevin takes the message normally.
- [ ] If confirmation is genuinely uncertain, **Check status** is available in
      the call card and transcript; it checks the existing action without a
      second request or an unintended pickup.
- [ ] Complete the [build 40 speech/pickup checklist](2026-09-16-natural-message-ios-40.md#owner-phone-acceptance--open)
      on build 41, including finishing speech, the conditional single three-second
      pause, caller continuation and the unchanged 30-second unanswered wait.

These checks supplement the [N1/N3 phone acceptance](../../kevin-prd.md#n1--finish-the-screening-notification-enhancement);
they do not close other unperformed scenarios. Successful Apple processing and
internal distribution do not prove owner installation or physical-call behavior.

## Actions and routing

Repository and personal billing owner: **delimatsuo/heykevin**, **delimatsuo**.
The public repository uses seven standard Ubuntu shards, Python quality and
stable fail-closed Test, with bounded timeouts and PR cancellation. Expected
chargeable cost is **$0**; no paid allocation, macOS hosted job or rerun is used.
September paid Actions usage observed at preflight was **$71.341073252**;
Hey Kevin net usage was $0. Main merge does not deploy.

Master owns architecture, evidence, audit and Git. The literal metadata edit used
headless `agy gemini-3.7-flash-low`: **33,906 input**, **584 output**,
**34,490 total tokens**, zero retries. Master regenerated and audited the project.
No metadata or package defects were found. Independent staff review approved
the source and actual signed package. Exact final-head CI and automatic Codex review must
complete before merging the release record.
