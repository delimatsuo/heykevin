# Native frontend candidate — 1.3.0 (39)

The owner approved translating the reviewed HTML into native SwiftUI and
providing another internal TestFlight build on September 14, 2026. This is the
[N3 scope](../../kevin-prd.md): Calls + Kevin navigation, preserved call actions
and Settings, and bounded history. Build 38 contains the earlier notification
enhancement; the new design starts with this candidate.

## Product decision

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

- Verified source: `39be39a57246676e32d19124e9d7608596c296df`.
- iOS tree: `a05ff41581f04b15c0c4ebf90bae9f99a457bce4`.
- Production application tree (`ios/Kevin`):
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

Required CI and signed-package delivery are pending. The September 15 preflight
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

## Remaining owner acceptance

Install 1.3.0 (39) once delivery is confirmed. Check the Calls + Kevin design,
history/search/details and Settings on the owner's phone. With an owner-controlled
fictional test call, check access to the live transcript and Pick up / Take a
message from both tabs and Account. Complete the remaining
[N1 device sequence](2026-09-14-notification-ios-38.md#owner-iphone-test-sequence),
including urgent fallback, stale notifications and real two-way audio.

Physical-device acceptance and public App Store submission remain pending. This
candidate introduces no backend deployment, runtime flag change, real customer
call or external TestFlight distribution.

## Routing evidence

Master model: Codex master; exact runtime model identifier unavailable.
Builder model/tier: gemini-3.7-flash-high for pinned Swift/state implementation;
gemini-3.7-flash-medium for mechanical test selectors, configuration and copy.
Routing reason: Master owned architecture, pinned contracts, independent audit,
source restoration, Git, execution and release evidence. Builders only edited
allowlisted files through headless agy.
agy transport=headless: true
Input tokens: 6451571
Output tokens: 1035084
Total tokens: 7486655
Retries: Two empty/partial builder results required bounded recovery; 21 total
builder invocations include planned implementation and audit-fix phases.
Audit defects found: Six source groups required repair: producing-account
ownership, observer lifecycle, Settings draft/save ownership, retained appointment
receipts, Account presentation order and deletion-flight ownership. Test/visual
harness corrections are described above.
Audit disposition: Source and final test-delta reviews approved; all 269 native
unit tests, 9 UI tests and targeted mutation checks passed. CI and package
delivery remain pending.
