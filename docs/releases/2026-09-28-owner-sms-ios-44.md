# Owner SMS controls — iOS 1.3.3 (44)

**TestFlight delivery complete: 1.3.3 (44) is VALID / IN_BETA_TESTING and explicitly available to the existing internal QA group.** All timestamps are UTC. The owner requested “build and
upload,” authorizing this iOS package and its existing internal TestFlight
delivery. The production backend update remains pending. There was no public
App Store submission, external beta review, new group assignment, or real test
call/text in this release.

## Source and checks

[PR #273](https://github.com/delimatsuo/heykevin/pull/273) added the
[SMS controls and terminal-call summary guard](../superpowers/plans/2026-09-28-owner-sms-controls.md).
[PR #274](https://github.com/delimatsuo/heykevin/pull/274) changes only the build
number from 43 to 44: one YAML and three generated project settings. The app
version remains 1.3.3. Root and independent review verified complete-file
expected equality, XcodeGen regeneration, plist validation and diff cleanliness.

| Source binding | Value |
| --- | --- |
| SMS source merge | `175c98ae463f9855e3c7716bc5d03e4e6b73e994` |
| Packaged source | `6d53b3281d159c08f9f17519c8575830c270d321` |
| Packaged source tree | `4c2719f4c98d3fc06717c03e8c0af29358926a01` |
| Packaged iOS tree | `d9181e020ab202b73ff941001ba0c4886285f295` |
| Version metadata merge | `4ae3a8b896b82012ba62cecf59899987e3bcddb7` |

The metadata merge tree is identical to the packaged tree. The underlying SMS
source passed 350 iOS unit tests (21 SMS preference tests) before its merge.
No application source changed during packaging. All nine package-head CI jobs
passed, including 8,152 Python tests across seven extracted shard summaries, in
[run 36481173748](https://github.com/delimatsuo/heykevin/actions/runs/36481173748).
Codex review completed on `6d53b32` at `20:44:09Z` with no findings and a positive
reaction at `20:44:13Z`. The optional Cursor Security Agent was neutral and
unavailable; that does not establish a security pass.

## Signed package

The required guarded wrapper completed archive and export:

- `kevin-sms-44-archive-20260928T204201Z-54646`
- `kevin-sms-44-export-20260928T204237Z-55395`

| Package identity | Verified value |
| --- | --- |
| App / bundle / team | `6761427495` / `com.kevin.callscreen` / `3FLG8W6B95` |
| Version / build | **1.3.3 (44)** |
| IPA size | 6,872,690 bytes |
| IPA SHA-256 | `e7795fdf486dc5c1e93b487ab74844cef500d48818b221478ac895b9d830c1a9` |
| Archive / export / app dSYM UUID | `E0384268-4BF8-34C4-BDE5-C646355E4543` |
| SDK / Xcode | iPhoneOS 27.0 / `27A266a` |
| Distribution profile | `639cf43f-e398-42cb-9927-3b610eb59e4c`, expires July 15, 2027 |
| Backend binding | `https://kevin-api-752910912062.us-central1.run.app`, production |

Archive and export pass strict/deep code-signature validation. Automatic signing
created a development-signed archive; export correctly re-signed the upload IPA
with Apple Distribution. The exported app has production APNs, Apple Sign In,
Time Sensitive notifications and `get-task-allow=false`, with no Critical Alert
entitlement or development device list. Both have matching compiled code: all
37 app and 31 Twilio file-backed Mach-O sections match across archive/export.
App plist semantics and nonsigning resources match. The app and Twilio privacy
manifests, iPhone-only family, required purpose strings and background modes are
present.

The three actual Swift compile commands use Release optimization, bind this
worktree, and have no DEBUG/STAGING definitions. All 40 checked-in Swift inputs
byte-match the packaged commit; the only additional input is generated asset
symbols. The three screenshot/native-review activation markers are absent from
the exported executable. No archive compiler warnings were observed. Independent
signed-package review returned no findings.

## Apple delivery

Apple validation passed; its JSON SHA-256 is
`f46d5777dc1647586eb95ad1a4aaedcb6be0ce4260b734fa9ea1d2f253137b31`.
Exactly one upload succeeded at `20:48:54Z`, delivery UUID
`e3076a1f-6f9c-4053-a1cf-772625cf8ce7`.
Upload JSON SHA-256:
`1bbc9e2e2fd60368029543b151c7de1c38c65a770c3aac890483d2ba2caca73e`.

Authenticated readback at `2026-09-28T20:53:50Z` confirmed this exact delivery UUID is
build **44** for prerelease version **1.3.3**, with `VALID`, unexpired,
`usesNonExemptEncryption=false`, and `IN_BETA_TESTING`. The existing internal QA
group `ce552be4-c362-4021-9a90-b77d2e910d15` explicitly contains the build. Its
access-to-all-builds configuration was preserved; no group mutation was needed.
English What to Test notes `f45ddb27-4bee-4cb3-924c-261a0db9e675` were written and read back exactly,
including the pending-backend limitation and completed-call, app-off, STOP/START
and notification checks. Notes SHA-256: `94f207f18a982c5be49b5133aa998ad9ceb88027015bcd7170720529c4b082ab`.
External beta state is `READY_FOR_BETA_SUBMISSION`; no external review was
submitted. App Store version readback still lists **1.3.2** as the newest public
`READY_FOR_SALE` version.

## Backend and phone acceptance

Production health readback at `20:48:26Z` remained on
`kevin-api-00273-f7g`, source
`2b402796d1cbffaf59bcb6ecb63d661d9dd0ef12`.
The earlier notification-only deployment
[run 36458376853](https://github.com/delimatsuo/heykevin/actions/runs/36458376853)
was still waiting at the owner's production gate for source
`f45fa6295cfcd290f4c6df2532318f1eb79dfa57`. It predates the SMS changes.
No deployment was dispatched, approved, cancelled or replaced for this upload.
Only Deli may approve that environment gate under the current instructions.

The new app reads versioned SMS settings from the backend. The current old
backend cannot supply that contract, so the app shows the setting as unavailable
rather than inventing a confirmed value. Build 44 alone does not activate the
new SMS behavior. A release of the reviewed backend is needed first, followed
by owner-run completed-call, app-off, STOP and START checks. START must not undo
an app switch left off. Notification recovery and locked-phone delivery also
remain physical acceptance checks; source tests and Apple processing do not
prove them.

## Cost, routing and retention

The public repository and billing owner are `delimatsuo/heykevin` and
`delimatsuo`. The metadata PR used nine standard Ubuntu validation jobs;
feature/main pushes trigger no jobs. Required Test and conversation resolution
are preserved. Expected incremental paid Actions cost is $0, with no hosted
macOS jobs, workflow changes, artifact uploads or new paid allocation.
September paid Actions readback before publication was $79.21627084, with
reporting through `16:16:44Z`; billing can lag.

Task-owned binary artifacts and raw logs are held under TMPDIR only through
verification, upload and Apple readback, then removed by the exact-directory
cleanup finalizer. This text receipt retains the package identity and results;
no long-term binary archive is created.

Master model: gpt-6-astra
Builder model/tier: gemini-3.7-flash-low
Routing reason: pinned mechanical build-number substitution; root regenerated and audited the full project diff
agy transport=headless: true
Input tokens: 76619
Output tokens: 760
Total tokens: 77379
Retries: 0
Audit defects found: 0 source or package findings
Audit disposition: source, package and Apple upload verified; processing and internal QA access verified; production backend and physical acceptance remain open
