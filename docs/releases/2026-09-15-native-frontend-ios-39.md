# Native frontend release — 1.3.0 (39)

**Public App Store release:** Apple reports `READY_FOR_DISTRIBUTION` / legacy `READY_FOR_SALE`,
verified September 16, 2026 at `00:20:10Z` (September 15 America/New_York). Version `1.3.0`,
build `39` is live on the App Store. Owner physical iPhone acceptance remains open.

Following internal TestFlight delivery, the owner requested submitting the existing build ("submit the last build"), and submission was completed. Current public 1.3.0 (39) includes the native Calls + Kevin frontend, preserved call actions and Settings, bounded history, and the screening notification enhancements.

## Public App Store release

Authenticated App Store Connect GETs completed September 16, 2026 at
`00:20:10Z` (September 15 America/New_York). Resources below are under
`https://api.appstoreconnect.apple.com/v1/`. A direct public US lookup during the
same session independently confirmed the public version and release date.

- **App Store Version:** `GET appStoreVersions/32977adc-f519-451a-a839-49976ee3bea7?include=build` HTTP 200: version `1.3.0`, `appVersionState=READY_FOR_DISTRIBUTION`, legacy `appStoreState=READY_FOR_SALE`, `releaseType=AFTER_APPROVAL`, linked build ID `7ebb5e58-9016-498b-b43e-75ec3383786e`, version `39`, `VALID`/unexpired. App ID `6761427495`, bundle `com.kevin.callscreen`.
- **Review Submission:** `GET reviewSubmissions/6697cbd8-48ab-4c63-8539-199683d98d6d?include=items,appStoreVersionForReview`: state `COMPLETE`, item state `APPROVED`, submittedDate `2026-09-15T04:10:43.283Z`, linked to version ID `32977adc-f519-451a-a839-49976ee3bea7`.
- **Build Status:** `GET builds/7ebb5e58-9016-498b-b43e-75ec3383786e?include=preReleaseVersion,buildBetaDetail`: version `39`, prerelease `1.3.0`, `VALID`/unexpired, internal state `IN_BETA_TESTING`.
- **Public Lookup:** Direct HTTP GET `https://itunes.apple.com/lookup?id=6761427495&country=us` returned version `1.3.0`, `currentVersionReleaseDate=2026-09-15T21:35:24Z`. Public lookup alone does not expose the build number; authenticated relationship provides build `39`. This does not claim all international storefronts or installed phone versions are confirmed.
- **Release provenance:** The owner requested submission of the existing build ("submit the last build"), and submission was completed. At submission `releaseType` was `MANUAL`; latest readback shows `AFTER_APPROVAL`. The actor and exact time of the transition are unknown. Read-only inventory refresh did not mutate settings; no further release action is needed and do not resubmit. Future releases retain separate authorization.

## Product decision

The owner approved translating the reviewed HTML into native SwiftUI and
providing another internal TestFlight build with bounded history on September 14,
2026. This is the
[N3 scope](../../kevin-prd.md): Calls + Kevin navigation, preserved call actions
and Settings, and bounded history. Build 38 contains the earlier notification
enhancement; the new design is delivered in this release.

Calls shows 20 records initially, then **Show 20 more**, up to the existing
100 most recent calls within 90 days. Search and All/Unread/Spam filters examine
the entire bounded snapshot before pagination. Returning from details preserves
the query, filter and expansion. Counts explain the available history.

This retrieval cap does not delete the 101st stored call or change the 90-day
retention sweep. No archive, longer retention, follow-up workflow or additional
caller communication is introduced. The [plan](../superpowers/plans/2026-09-14-native-frontend.md)
records official Apple Phone, Google Voice and Quo research and the independent
expert panel. Twenty is Kevin's display choice, not a competitor's stated cap.

## Frozen source and review

- Unit/UI verification source: `39be39a57246676e32d19124e9d7608596c296df`.
- Unit/UI iOS tree: `a05ff41581f04b15c0c4ebf90bae9f99a457bce4`.
- Production application tree at that verification (`ios/Kevin`):
  `4159c6db0d261cf13ccf7f5d01b2fb8cdff239fe`.
- Base main: `f72fe0a5ef8dc618e51c90767dc8f9e04834ef7d`.
- Branch/worktree: `codex/frontend-native`, `<clone>/.worktrees/frontend-native`.
- Bundle/team: `com.kevin.callscreen` / `3FLG8W6B95`.
- Scheme/configuration: `Kevin` / `Release`; version/build `1.3.0 (39)`.
- The independent Calls reviewer approved production source `fae4835...`,
  including retained appointment receipts and Account-sheet dismissal ordering.
  The Settings reviewer approved `947b65d...`, closing deletion-flight
  ownership. Its Settings production files did not change afterward.
- A final independent review of committed objects at `39be39a...` approved the
  three changed test files with no findings and verified the production app tree
  remained identical to the reviewed `fae4835...` tree. Workers did not grade
  their own work.

## Local verification

The full restored native unit suite passed: **269 tests, zero failures**, on the
isolated iPhone 16 / iOS 26 simulator. Result:
`kevin-native-unit6-20260915T032050Z-15532/result.xcresult`.
All local Xcode work used the required XcodeStorage wrapper.

Targeted mutation probes detected removal of history request/auth checks,
historical presentation auth, active-call origin, poll ownership, fresh settings
drafts, hydration ownership, the 100-call cap, search-before-pagination,
appointment-receipt generation, Account presentation queuing, and deletion
ownership/duplicate/callback guards. Production sources were restored byte for
byte after each run. Result bundles:

- `kevin-native-mutant-a-20260915T031156Z-76063/result.xcresult`
- `kevin-native-mutant-b-20260915T031817Z-150/result.xcresult`
- `kevin-native-mutant-receipt-20260915T031957Z-10234/result.xcresult`

The receipt mutation was also run alone to avoid attributing the effect of a
removed request guard to the receipt-generation check. Initial audit assertions
were strengthened to observe the retained error banner and actual unsaved draft,
baseline and AppState values. A connected-call test remained green when the
separate detail-sheet queue was mutated; it exercises a different branch and is
not counted as evidence for that queue. Mutant assertion failures are expected
negative controls, not failures of the restored candidate.

Fixture UI verification passed: **9 tests, zero failures**, including all four
history expansions through the 100-call cap and search for record 35. Result:
`kevin-native-ui3-20260915T032127Z-20382/result.xcresult` (232 seconds of tests).
It exercises the actual native root, not the marketing screenshot frame.
Earlier test failures exposed ambiguous
foreground/background selectors and inefficient scrolling; test repairs preserve
the actual interaction and disabled-action assertions.

Visual inspection covered Personal/Business, Calls, Kevin, Account, empty/error,
live caller, small iPhone, dark mode and the largest accessibility text setting.
Compact headers and live-caller wrapping were repaired, and adaptive cobalt and
forest action colors were verified. Screenshots do not establish VoiceOver or
physical-call acceptance. Fictional fixture actions suppress external effects.

The 25 focused backend history tenant-isolation and appointment checks passed.
Backend behavior is unchanged; the calls endpoint change is documentation only.
The inherited database-exception-to-empty-list backend contract remains a known
limit: the client can show transport/HTTP/decoding failures, but cannot identify
that server-side ambiguity.

## CI and delivery

All nine required CI jobs passed for `f4156db1535db14a531fc842abdc31ab8d61b936`
in [run 34925143050](https://github.com/delimatsuo/heykevin/actions/runs/34925143050)
on [PR #248](https://github.com/delimatsuo/heykevin/pull/248), using 412 runner
seconds. Both deploy jobs were skipped. Automatic Codex review reported exhausted
review quota; Cursor could not start without usage-based pricing. Neither was
retried, and independent source review supplies the review evidence.

The first signed package passed Apple validation but was **not uploaded**.
Independent package inspection found duplicate comment-only catalog entries for
`Mark All Read` and `Robocall, Kevin hung up.` that caused their Spanish and
Portuguese translations to be omitted from the compiled tables. The entries were
merged while preserving comments and all translation values. Duplicate-aware
parsing verifies 424 unique source keys and unchanged translations. Swift and
test sources are unchanged by this correction; the replacement package must
verify the compiled language tables. The initial IPA SHA-256
`ca483fed23f639bdce096bc9b1de648771df190c839b2e2114fb027f8ef83087`
is withdrawn. Its exact scratch directory was cleaned.

The corrected source `4bf090acf74ac476b1b31d6a5a44db4fffc16c5b` passed all nine
required jobs in [run 34926028253](https://github.com/delimatsuo/heykevin/actions/runs/34926028253),
using 401 runner seconds; deployment jobs were skipped. Independent replacement
package review approved with no findings and closed the localization P2.

The September 15 preflight
bound public repository and billing owner to `delimatsuo/heykevin` and
`delimatsuo`. Required check `Test` remains strict and fail closed, backed by
seven Python shards and quality (nine standard Ubuntu jobs including Test).
Timeouts are 15 minutes for shard/quality jobs and 5 for Test; PR concurrency
cancels superseded work. Deployment jobs do not run on PRs, and main pushes
do not deploy.

The comparable run used 356 runner seconds (ten rounded minutes), approximately
$0.06 gross at the reference rate and **$0 chargeable** under
[GitHub's public-repository policy](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
At preflight no repository runs were active, three caches used 175,106,709 bytes,
and no Actions artifacts were retained. September account paid Actions usage
was $64.135058; its simple month-end projection exceeded the $80 normal ceiling.
This candidate introduces no paid Actions work or hosted macOS jobs.

At `2026-09-15T03:21:53Z`, authenticated read-only Apple inventory confirmed
build 39 was unused, build 38 remained VALID / IN_BETA_TESTING, and existing
internal QA group `ce552be4-c362-4021-9a90-b77d2e910d15` automatically receives
builds. This is preparation evidence, not an upload claim.

## Signed package and upload

- Packaged source: `4bf090acf74ac476b1b31d6a5a44db4fffc16c5b`; source tree
  `0d64bf4d53601b59f5c1e4769a21e8d79e36d250`; iOS tree
  `dcf196ba5136527e7490d02ad7c3ff9c781a63a1`; production app tree
  `a2e505258c5cd1182b5214b4b0c01e23f4fca0f9`.
- IPA: **6,598,270 bytes**, SHA-256
  `4be09e45a2fb10b00183653660ee934eae609ba0e05a1c171d1f9e6284d46b87`.
  Independent inspection recomputed the hash and checked all 18 packaged app
  files against the ZIP, extraction and manifest. All 35 file-backed Mach-O
  sections match archive/export, with executable/dSYM UUID
  `7EF1A8CF-F93A-3B24-B5E4-A3C52277D7A4`.
- Both apps pass deep strict signature verification. Automatic signing creates
  a development-signed archive, then export signs the same compiled code with
  Apple Distribution: Travel Advisory LLC (`3FLG8W6B95`). The exported app and
  profile have production APNs, Time Sensitive and Apple sign-in, with
  `get-task-allow=false` and no Critical Alerts. Distribution profile
  `639cf43f-e398-42cb-9927-3b610eb59e4c` expires July 15, 2027.
- Archive and export agree on version `1.3.0`, build `39`, the expected bundle,
  production environment/backend, remote-notification/VoIP background modes
  and permission declarations. TwilioVoice is `6.13.6 (179124)`; build SDK is
  iPhoneOS 26.0, Xcode `17A400`. Screenshot/native-review activation is disabled
  in Release; this does not claim every test-related metadata byte is absent.
- English source fallback and all **272** compiled Spanish and Portuguese
  translations match the catalog. Both previously missing labels are present.
- Wrapper archive result: `kevin-native-39-archive-r2-20260915T034127Z-2874`;
  export result: `kevin-native-39-export-r2-20260915T034158Z-6779`.
- Apple validation passed at `2026-09-15T03:42:39Z` using altool
  `26.0.18 (170018)`. Independent validation-log SHA-256:
  `4c136a5b87425df020391adcd8c3fcad811238a42c83bde37676683d0f85e0b0`.
- One upload succeeded without errors at `2026-09-15T03:47:26Z`; delivery UUID
  `7ebb5e58-9016-498b-b43e-75ec3383786e`.
- At `2026-09-15T03:51:07Z`, authenticated Apple readback bound that exact build
  ID to app `6761427495`, version `1.3.0`, build `39`, VALID processing,
  IN_BETA_TESTING and unexpired status. The build is present in existing internal
  QA group `ce552be4-c362-4021-9a90-b77d2e910d15`. English What to Test was
  published and read back under localization `05b1350a-040b-4ca7-82e4-3587ba3b8cb5`.
  Existing external groups and public distribution were not changed.

Durable evidence is recorded here. Archives, IPAs, extracts and raw delivery
logs use the configured ephemeral scratch directory with exact-directory
cleanup; XcodeStorage manages its own cache/result bundles.

## Partial physical-device frontend checks — September 15, 2026

The session began at `2026-09-16T00:50:46Z` (September 15 America/New_York).
These observations came from the owner's physical **iPhone 16 Pro Max** through
iPhone Mirroring, not a simulator. Read-only device metadata reported iOS
**26.6.2 (23G90)** and installed bundle `com.kevin.callscreen`, version
**1.3.0 (39)**. TestFlight's app detail independently displayed version 1.3.0,
build 39, and the app was opened with its **Open** button. After the owner
re-enabled Mirroring, navigation and read-only permission checks continued at
approximately `01:17–01:30Z` on September 16 (September 15 America/New_York).

| Check | Observed result |
|---|---|
| Initial navigation and history | **Pass:** the app opened on Calls and displayed “Showing 20 of 100 calls.” |
| History expansion and cap | **Pass:** the initial 20-to-40 expansion was observed. In the continuation, starting at 20, four successive Show 20 more activations reached the header “Showing the 100 most recent calls available in this history.” The final footer retained the 100-recent-calls/90-day scope and no longer offered Show 20 more. |
| Tab and Account navigation | **Pass:** Kevin opened, Account Settings opened from Kevin and dismissed, and returning to Calls retained the 40-call expansion. In the continuation, Account Settings also opened from Calls; dismissal preserved the 100-call expansion. |
| Search beyond the first page | **Pass:** searching for an already fetched call outside the initial 20 produced one matching result. The identifying query and caller content are omitted from this record. |
| Details and retained state | **Pass in separate checks:** dismissing an already-read search result preserved the query, All filter and one-result view. Later, opening and closing an already-read call from the unqueried 100-call view preserved that expansion. A non-default filter after Details was not exercised. |
| Empty states | **Pass:** Unread displayed its distinct zero-unread state; Spam displayed “No spam calls.” The fictional query `kevin-acceptance-no-match` displayed “No matching calls” with Clear search. |
| Final app state | Calls was restored to All, 20 of 100, with no search query after the checks and again after returning from iOS Settings. |

The continuation inspected the existing iOS Settings values without changing them:

| Setting | Observed state |
|---|---|
| Microphone | Enabled for Kevin. |
| Notifications | Allow Notifications and Time Sensitive Notifications enabled. Lock Screen, Notification Center and Banners selected; banner style Temporary; Sounds and Badges enabled. |
| Notification previews | When Unlocked (Default). |
| Notification prioritization and summaries | Prioritize Notifications and Summarize Notifications enabled. These settings do not establish delivered-notification wording or behavior. |

No regression was demonstrated in these checks. No new call or message was
initiated and no saved account preference or OS permission was changed.
Customer identities, phone numbers, transcripts and call-history screenshots
are not included in the durable record.

**Still open:** a non-default filter retained after Details (both Unread and
Spam were empty, so no matching detail was available); live-call access from
Calls, Kevin and Account; the N1 phone sequence below; and
the active Focus state. Focus Settings was viewed, but its sharing configuration
does not establish which Focus, if any, was active. The owner reported that a
second owner-controlled phone was unavailable. These partial observations do
not complete N1 or N3 acceptance.


## Remaining owner acceptance

**Owner scope update — September 16, 2026:** VoiceOver qualification is deferred,
outside current N1/N3 acceptance, and is not a release blocker. No physical
VoiceOver pass is claimed. Resume accessibility work only if the owner explicitly
requests it; see the [PRD deferral](../../kevin-prd.md#deferred-ideas-and-superseded-plans).

Continue with the installed 1.3.0 (39) build. Finish the open frontend checks
above. With an owner-controlled fictional test call, check access to the live
transcript and Pick up / Take a message from both tabs and Account. Complete the
remaining [N1 device sequence](2026-09-14-notification-ios-38.md#owner-iphone-test-sequence)
using build 39, including urgent fallback, stale notifications and real two-way audio.

Public App Store release 1.3.0 (39) is completed. Partial physical frontend
observations are recorded above; the remaining physical iPhone call and frontend
checks stay open. Offline owner-phone testing of unavailable delivery does not prove
or equal a forced APNs-provider error. This session introduced no backend
deployment, runtime flag change, or real customer call.


## Routing evidence

Master model: Codex master; exact runtime model identifier unavailable.
Builder model/tier: gemini-3.7-flash-high for pinned Swift/state implementation;
gemini-3.7-flash-medium for mechanical test selectors, configuration and copy.
Routing reason: Master owned architecture, pinned contracts, independent audit,
source restoration, Git, execution and release evidence. Builders only edited
allowlisted files through headless agy.
agy transport=headless: true
Input tokens: 6514580
Output tokens: 1037897
Total tokens: 7552477
Retries: Two empty/partial builder results required bounded recovery; 22 total
builder invocations include planned implementation and audit-fix phases.
Audit defects found: Seven source groups required repair: producing-account
ownership, observer lifecycle, Settings draft/save ownership, retained appointment
receipts, Account presentation order, deletion-flight ownership and duplicate
localization entries. Test/visual
harness corrections are described above.
Audit disposition: Source and final test-delta reviews approved; all 269 native
unit tests, 9 UI tests and targeted mutation checks passed. Corrected-source CI,
replacement package review and Apple validation passed; upload succeeded.
Internal QA availability and What to Test readback are verified. Public App Store
release 1.3.0 (39) is completed and READY_FOR_DISTRIBUTION. Remaining owner
physical-device acceptance stays open; VoiceOver qualification is deferred.
