# Transcript-first call flow — iOS 1.3.2 (42)

**Delivery complete:** Apple reports **VALID / IN_BETA_TESTING** for **1.3.2 (42)**,
with explicit availability in the existing internal QA group and exact English
What to Test readback, verified **2026-09-17T15:33:58Z**. Owner installation and
phone acceptance remain open. No public App Store submission was made.

**Later App Store submission, September 17 at 18:32:28 UTC:** at the owner's
“go ahead and upload” instruction, this existing build was submitted with six
updated screenshots. Apple reports **WAITING_FOR_REVIEW** and release remains
**AFTER_APPROVAL**. See the [App Store submission record](2026-09-17-app-store-ios-42.md).
This supersedes the internal-only authorization and no-submission status above
for this release. No binary was uploaded again; unperformed phone checks stay open.

## Authorization and compatibility

The owner said “ok, continue” after the source merge report named a new test
build and phone verification as the next step. This authorizes preparing,
signing and delivering the next internal candidate to the existing QA group.
It does not authorize another public submission or backend deployment.

The candidate contains the approved [transcript-first flow and appearance
correction](2026-09-17-transcript-first-implementation.md): notification body or
Read transcript enters the complete exact-call conversation; message-taking uses
the existing operation; pickup retains the available screening conversation as
Before you joined; reading position and system light/dark appearance are preserved.

Production still identifies `976202dfd418e0c4ca551a96e8075e5d5065b45b`, revision
`kevin-api-00272-8z4`, at the September 17 15:16 UTC health check. The new structured
screening reason is not deployed. Missing reason metadata uses the honest
“Finding out why they’re calling…” fallback. This candidate does not change
backend state, retention, recording or post-pickup transcription.

**Later backend rollout, September 17 at 17:36:33 UTC:** the owner separately
authorized deployment of the screening-reason field and owner SMS identity.
Production now reports `2b402796d1cbffaf59bcb6ecb63d661d9dd0ef12`, revision
`kevin-api-00273-f7g`, with 100% traffic. See the
[backend rollout record](2026-09-17-owner-sms-identity-backend.md). This supersedes
the undeployed-field limitation above and in the earlier TestFlight notes;
missing reason metadata still uses the fallback. Phone checks remain open,
and no replacement iOS package or TestFlight metadata update was made.

Apple reported the existing 1.3.1 version `READY_FOR_SALE` and its review
submission `COMPLETE` at 15:16 UTC. The public US lookup still returned 1.3.0 at
15:32:01 UTC. Those are separate observations; storefront propagation was not
established. Using 1.3.2 avoids the released 1.3.1 marketing version. Existing
release settings and screenshot sets are unchanged.

## Frozen source and checks

| Identity | Value |
|---|---|
| Merged implementation | `9640dd0d20306123c81767da125040402bdbb0d6` (PR #261) |
| Packaged source | `043b4c8e9cea09f95def904e98045abe901f94d5` |
| Packaged source tree | `d30923e93acabeaa98da02e83da135f6dbeeb093` |
| Packaged iOS tree | `7dc72a8660313def1efe8fc217e28664ce2b41fd` |
| Unchanged app source tree | `114959b814e87126e6e06a560064ae8f9dc43028` |
| Release branch / PR | `codex/transcript-build42` / [#262](https://github.com/delimatsuo/heykevin/pull/262) |

Only `ios/project.yml` and its generated Xcode project changed in the packaged
commit: one YAML and three project substitutions per field, marketing version
1.3.1 to 1.3.2 and build 41 to 42. Master and independent source review compared
the complete files deterministically. Application code, backend, tests, signing
entitlements and export settings are unchanged from reviewed main. XcodeGen
regeneration and `git diff --check` passed. No full local suite was repeated for
these metadata changes; implementation tests and mutation evidence remain in the
linked implementation record.

All nine package-head CI jobs passed in
[run 35239673366](https://github.com/delimatsuo/heykevin/actions/runs/35239673366).
Codex completed review on `043b4c8` at 15:22:23Z with no findings and a positive
completion reaction. The optional Cursor security review could not start because
its integration required an additional billing allowance; it is unavailable
evidence, not a security pass. No billing setting was changed.

The first delivery-documentation head `646bcbc` passed all nine CI jobs in run
`35241373903`. Codex found a valid documentation P2: active N1/roadmap acceptance
rows still directed installation of build 41 and retained its old review status.
The follow-up makes build 42 the explicit current phone-test target in those
rows, keeps build 41 as historical package evidence, and preserves every open
physical acceptance checkbox. It changes no application or package bytes.

## Signed package

Archive and export used the required `xcodebuild-external` wrapper with one
explicit Kevin project, Release configuration and generic iOS destination. The
first archive preflight correctly declined while another guarded job owned the
global lock. The successful invocation waited for that job; the lock was neither
removed nor bypassed.

Managed result IDs:

- `kevin-transcript-42-archive-20260917T152122Z-57973`
- `kevin-transcript-42-export-20260917T152309Z-58462`

| Package identity | Verified value |
|---|---|
| App / bundle / team | `6761427495` / `com.kevin.callscreen` / `3FLG8W6B95` |
| Version / build | **1.3.2 (42)** |
| IPA bytes | 6,650,770 |
| IPA SHA-256 | `165bf8406bb4901fb67033edc3ff39c1a67a427dd1669f22aee5db9b696e99ea` |
| App archive / export / dSYM UUID | `191ED730-A06B-3ECE-A3CE-57CFB04D9522` |
| Distribution profile | `639cf43f-e398-42cb-9927-3b610eb59e4c`, expires July 15, 2027 |
| SDK / Xcode | iPhoneOS 26.0 / `17A400` |
| Twilio Voice | `6.13.6 (179124)` |
| Backend binding | `https://kevin-api-752910912062.us-central1.run.app`, production |

Master verified strict/deep archive and export signatures, all 21 ZIP file bytes
against extraction, matching app/Twilio executable and dSYM identities, production
APNs, Time Sensitive and Apple Sign In entitlements, `get-task-allow=false`, no
Critical Alert entitlement, iPhone-only family, background modes, purpose strings
and no non-exempt encryption. None of the three screenshot/native-review
activation environment-key strings occurs in the exported executable.

Independent review approved the exact IPA above after reading the actual archive,
export, source and raw logs. All 35 app and 31 Twilio file-backed Mach-O sections
match between archive and export. Archive development signing is correctly
replaced by distribution signing in the exported app. All 272 Spanish and 272
Brazilian Portuguese compiled translations match the source catalog, with no
duplicate catalog keys. Release compile provenance points to the frozen worktree
and uses optimization without DEBUG/STAGING defines. No package findings remain.

## Apple delivery

Apple validation passed at **15:24:31Z**, followed by exactly one successful upload
at **15:27:51Z**. Delivery UUID: `a81a22cf-cf59-43c8-889e-3f0774df9bea`.

- Validation JSON SHA-256: `29944654bedf60421466e1fb7476f2d7b4f97b026a794970fe83402858f07e42`.
- Upload JSON SHA-256: `f9df1ba4b3a3dd86d430b5c0202c53ce9a07d70b1d849ad2593e7c06fb9e0ee4`.

Before upload, build 42 was absent and the internal QA group
`ce552be4-c362-4021-9a90-b77d2e910d15` was verified against app `6761427495` and
bundle `com.kevin.callscreen`. Its existing access-to-all-builds setting was
preserved. At **15:32:59Z**, Apple returned build 42 as `VALID`, unexpired,
`usesNonExemptEncryption=false`, and `IN_BETA_TESTING`, related to prerelease
version 1.3.2. At **15:33:58Z**, the existing QA group explicitly included this
build; no group-membership mutation was needed. English test notes were created
and read back exactly, covering the first notification test, message-taking,
pickup, scrolling, appearance and the undeployed reason-field limitation.

| Apple identity | Value |
|---|---|
| Build / delivery UUID | `a81a22cf-cf59-43c8-889e-3f0774df9bea` |
| Prerelease version | `ab8b7423-a761-48f9-8298-e4d0010ceaaa`, 1.3.2 |
| Internal QA group | `ce552be4-c362-4021-9a90-b77d2e910d15` |
| English test notes | `d5d05c16-3658-4950-bad5-429c015ab9ca` |
| External beta state | `READY_FOR_BETA_SUBMISSION`; no external review requested |

Temporary package binaries and raw logs are scoped to this delivery review and
removed afterward. This durable text record retains provenance; no long-term
binary archive is approved. Documentation after the packaged commit does not
change the iOS or backend trees and does not require another upload.

## Owner phone checks — open

- [ ] Install **1.3.2 (42)** from TestFlight and record the installed build.
- [ ] With Focus off and an owner-controlled second phone, tap a locked-screen
      notification body. After unlock, the full matching conversation opens
      directly. Repeat with the app already open; reading alone never answers.
- [ ] Test Read transcript when iOS exposes that action. System notification
      layout determines how many actions are visible.
- [ ] Tap Take a message. The full conversation remains visible and pending,
      confirmed or uncertain state stays truthful. Check status reconciles the
      existing operation. Kevin finishes current speech, uses the conditional
      three-second pause when already checking availability, and hears the
      caller’s subsequent reply without repeating an answered question.
- [ ] Pick up a separate test call. Available screening text remains under
      Before you joined; two-way audio, mute, speaker and end work. A later call
      must not display the previous call’s captured conversation.
- [ ] Check consistent light and dark appearance, live-call entry from Calls,
      Kevin and Account Settings, stable earlier-line reading and bottom following.

Unperformed N1/N3 rows remain open. VoiceOver qualification remains deferred by
the owner. Apple processing, simulator fixtures and source review do not prove
physical APNs, unlock, CallKit or audio behavior.

## Actions and routing

Public repository and personal billing owner: `delimatsuo/heykevin` and
`delimatsuo`. Before push, the exact clean candidate and unchanged workflow were
bound: seven Ubuntu shards, Python quality and required fail-closed Test; no
hosted macOS or deployment on this PR/main merge. Expected incremental chargeable
Actions cost is $0. Portfolio September paid usage observed before push was
$72.244022734; Hey Kevin usage was $0. No same-SHA rerun or new paid allocation.

Master model: gpt-6-astra
Builder model/tier: gemini-3.7-flash-low
Routing reason: exactly pinned mechanical version substitutions; master regenerated the project and independently verified the literal diff
agy transport=headless: true
Input tokens: 28799
Output tokens: 398
Total tokens: 29197
Retries: 0 builder retries
Audit defects found: 1 documentation issue — stale build 41 acceptance references; no source/package defects
Audit disposition: documentation references corrected; metadata source and actual signed package independently approved; Apple validation, processing and QA delivery verified; physical acceptance remains open
