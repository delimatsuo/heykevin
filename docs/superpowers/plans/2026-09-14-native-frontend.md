# Native Calls + Kevin frontend and bounded history

> **Completion Note (2026-09-15):** This plan was implemented, verified, packaged, and published to the App Store as version **1.3.0 (39)** (`READY_FOR_DISTRIBUTION`). See the canonical [build 39 release record](../../releases/2026-09-15-native-frontend-ios-39.md). Physical iPhone and VoiceOver acceptance remain open.

Owner authorization: September 14, 2026, after reviewing the HTML, “ok, proceed
with the enhancements. Make sure you remember that history needs to have a
limit or the list will be too long. How similar products manage history?
Search online and decide how to implement.” This follows the explicit proposal
to translate the concept into SwiftUI and provide another TestFlight UI build.
The native design is approved; physical-call acceptance and public App Store
release remain separate. The original notification build 38 remains testable.

Baseline: `f72fe0a5ef8dc618e51c90767dc8f9e04834ef7d`, tree
`6009229ce26c94081d4462dca5613249de88707a`. Worktree:
`<clone>/.worktrees/frontend-native`, branch `codex/frontend-native`.
Design reference: [reviewed HTML](../../frontend-concept/index.html), SHA-256
`942c45e608987d8d539382c851b39860b1c0734e5d67bea92636d32a6a47cc21`, and
[panel rationale](../../frontend-concept/PANEL-REVIEW.md).

## Research and decision

Reviewed official sources on September 14, 2026:

| Product | Documented approach | Lesson for Kevin |
|---|---|---|
| [Apple Phone](https://support.apple.com/guide/iphone/view-and-delete-the-call-history-iph21d1e1f56/ios) | Call-type filters and per-call details; deletion is explicit. The cited guide does not establish a numeric history cap. | Keep familiar filters and detail navigation. |
| [Google Voice search](https://support.google.com/voice/answer/146756?hl=en) and [archive](https://support.google.com/voice/answer/143935?hl=en) | Search by person, number and date; archiving hides items without deleting them. | Separate finding and displaying calls from retention or deletion. |
| [Quo, formerly OpenPhone](https://www.quo.com/product/call-analytics/call-logs) | Call status/date filters, searchable conversation context; its product page describes indefinite call-log retention. | Keep useful context findable without copying its storage policy. |

Decision: initially show **20 calls**, then **Show 20 more**, up to the existing
**100 most recent calls within 90 days** returned by Kevin's API. Twenty is our
presentation choice, not a number attributed to a competitor. The 100-call cap
is a retrieval/display boundary, not deletion of the 101st stored call. This
slice does not change the existing 90-day retention sweep.

Fetch the existing bounded history once per load. Search and filter that entire
snapshot before applying the visible-row limit. Do not add backend pagination,
archive states, longer retention, a follow-up product, or deletion controls.

## Plan panel

Three independent fresh-context reviews used Codex / gpt-6-astra / high:
Staff Engineer, Security/Privacy Engineer and Product UI/UX Engineer. Three of
three initial reviews and three of three debate responses were reduced in code.
All three support C1-C7 below; no unresolved P0/P1 objections remain once the
conditions are pinned. The reviewers do not implement or grade their own work.

Panel decision: **approve with the pinned implementation and verification
conditions below**. The initial review found unguarded history read-ID seeding,
mutable-account detail actions, view-owned live polling, unsupported prototype
facts, and an inherited backend error ambiguity. These determine the scope.

## Pinned builder contract

Master owns this plan, interpretation, Git, generated project, verification,
release evidence and all remote actions. One headless `agy` builder uses
`gemini-3.7-flash-high` because this is substantive SwiftUI/state implementation.
No other writer operates in the worktree while the builder runs. No Git
mutations, builds, tests, provider access, signing, uploads or network browsing
by the builder. The parent validates the actual artifact afterward.

Allowed implementation files:

- `ios/Kevin/Views/ContentView.swift`
- `ios/Kevin/Views/CallHistoryView.swift`
- `ios/Kevin/Views/SettingsView.swift`
- `ios/Kevin/Views/WhatsNewSheet.swift` (routing only if necessary)
- `ios/Kevin/Theme/Theme.swift`
- `ios/Kevin/Models/AppState.swift` (navigation/read-state/auth observation only)
- `ios/Kevin/Models/Call.swift` (truthful display projection only)
- `ios/Kevin/Services/APIClient.swift` (history decoding and captured-auth
  history/read/appointment requests only)
- `ios/Kevin/Services/CallActionCoordinator.swift` (auth-change notification,
  equatable/hashable contexts or navigation adaptation only; preserve action
  protocol, CallKit ownership and reconciliation)
- New `ios/Kevin/Models/CallHistoryModel.swift`
- New `ios/Kevin/Models/FrontendNavigation.swift`
- New `ios/Kevin/Services/LiveCallObserver.swift`
- New `ios/Kevin/Views/ActiveCallCard.swift`
- `ios/Kevin/Debug/AppStoreScreenshotFixtures.swift`
- New tests `ios/KevinTests/CallHistoryTests.swift`,
  `ios/KevinTests/FrontendNavigationTests.swift`,
  `ios/KevinTests/LiveCallObserverTests.swift`
- New `ios/KevinUITests/FrontendUITests.swift` for actual-root fixture interaction;
  the parent owns adding the UI-test target and scheme to project.yml.
- `ios/Kevin/Localizable.xcstrings` (new interface copy in en/es/pt-BR).

Inspect the actual localization path before editing. If it differs, use the
existing catalog under `ios/Kevin` and report that path. Stop on a required
out-of-scope architecture change. Do not edit backend, workflows, project.yml,
entitlements, credentials, signing settings, CallManager, external assets or
other documentation. Do not read any `.env` or secret file.

### C1 — History state and API ownership

Create an actual production `@MainActor` observable history owner used by Calls,
with injected fetch/current-auth/clock dependencies for deterministic tests.
One request revision and immutable `CallAuthContext` identify each load. Every
success/error/loading cleanup/read hydration/badge/reauth effect checks BOTH
the latest revision AND current auth including generation. A→B→A is a new
lifetime even if the credentials compare equal. Captured stale failures cannot
clear a newer request's spinner or show Session Expired for a newer account.

`getCallHistory` must return records without mutating AppState. Extract a
production-used pure response decoder, validate HTTP 2xx and the calls-array
envelope, and throw typed sanitized errors for 401/403/5xx/malformed 200. Use
`retryRequest(..., signalReauth: false)` for captured-auth history and apply
reauth only in the guarded current history commit. Do not turn malformed data
into a believable empty account. Preserve existing valid field defaults and
appointment fields; records require a real nonempty stable `call_sid` and valid
finite timestamp, never an ID synthesized from phone content.

On auth invalidation clear retained rows, query/filter/expansion, selected detail,
notification target, read metadata and unread badges. Auth observation must
include contractor changes, logout and credential rotation, not just appearances.
Use the existing epoch; a sanitized notification on epoch advance can trigger
main-actor invalidation after the credential setter completes. Each action still
checks auth directly so a delayed notification cannot grant stale authority.

Keep local read IDs bounded to the current fetched snapshot and scoped by
contractor, with synchronous captured-key persistence (never deferred writes
against a later mutable account). Do not load the old unscoped key into a new
account. Server read flags seed only a successfully committed owned snapshot.
No persistent transcripts, call snapshots or queries. `isCallUnread` respects
server read flags as well as current-account local read IDs. Fixture paths never
persist. Preserve existing server mark-read behavior, using captured credentials.

### C2 — Navigation, live observation and immutable presentations

Calls and Kevin are the only visible tabs. Calls is the initial destination.
Preserve the existing internal `.live`, `.recents`, `.settings` commands using
an explicit production-used navigation adapter (a `.kevin` case may be added).
An internal `.live` request opens the exact active call; `.recents` opens Calls;
legacy `.settings` from the calendar announcement must still lead to the needed
Kevin integration control. Explicit account Settings opens from a labeled
profile/gear control, not a third tab. Do not silently drop old route commands.

One root-owned `LiveCallObserver` polls the active call using the existing
getCallAction contract every two seconds while foreground and active. It allows
one in-flight request, captures auth and lifecycle scope, and invalidates
completion/cleanup when stopped, backgrounded, replaced or authenticated anew.
Views do not create network polling timers. Tab/sheet transitions do not restart
or duplicate the observer. Keep active-call discovery on foreground; coordinate
it with the observer rather than running two active-status requests. An unknown
status preserves the call; a confirmed matching ended status clears only its
lease. Do not forcibly replace an explicitly opened historical detail.

Calls has a dark prominent active-call card. Kevin and account Settings retain
a compact, labeled return-to-call control. Every rendered live surface carries
immutable callID/auth/lifecycle identity. Validate it before exposing caller
content or initiating Pick up/Take a message; pass that captured ID/auth to the
existing coordinator. An old A surface must never initiate or show B. Loading,
connecting, requesting, taking-message, urgent and error states come from the
coordinator, not inferred success. The live detail remains backed by the actual
transcript and current action coordinator. Preserve the existing CallManager
full-screen call presentation and its dismissal lease; never duplicate a native
incoming or outgoing call. Resolve sheet/full-screen presentation deliberately
when a pickup connects. Passive view dismissal never disconnects the caller.

Historical details also carry a captured auth lease and call ID. An auth change
invalidates presentation before any action; mark-read and appointment requests
use captured credentials with pre-dispatch and post-await checks. Callback/text
links on a stale detail cannot operate. Reuse existing CallDetailView content and
appointment semantics: do not claim a text was sent without its existing server
receipt. Notification details resolve exact IDs from the full snapshot, outside
the current visible page and filter; unavailable records keep the honest state.

### C3 — Bounded, truthful journal

`CallHistoryModel` or a pure production-used policy owns constants pageSize=20,
maximumCount=100, retentionDays=90. Injectable clock; fixtures use fixture now.
Discard invalid/empty IDs, deduplicate by ID taking the newest timestamp (equal
timestamp duplicates keep first source occurrence), sort timestamp descending
then ID ascending, exclude timestamps older than90days, then cap100. Apply
case/diacritic-insensitive caller-name, phone and caller-excerpt search plus
All/Unread/Spam before `prefix(visibleLimit)`. Normalize phone digits so spaces,
parentheses and country-code formatting do not prevent matches. Spam includes
both spam and blocked outcomes. Existing message-based unread semantics remain.

Initial20, explicit Show20more, never beyond100. Preserve expansion/filter/query
when returning from detail; reset to20 on explicit query/filter change. Keep
expansion on refresh when possible, clamped to valid bounds. Rows group by
Today/Yesterday/localized calendar date and use stable IDs. Lazy rendering.

Required copy (localized equivalents): “Showing 20 of 73 calls”; “History
includes up to 100 recent calls from the last 90 days.” When capped, “Showing
the 100 most recent calls available in this history.” Do not claim no older
records exist. Distinct empty outcomes: No calls yet, No unread calls, No spam
calls, No matching calls; appropriate Clear search/Show all actions. Retained
rows stay visible on observable reload failures with an explicit Retry.
Mark all read is scoped to all fetched recent history, including hidden pages;
label/accessibility clarify that scope. Counts recompute when read state changes.

Field mapping: name/phone from CallRecord; snippet is an available Caller:
transcript excerpt (use the existing summaryLine selection as a starting point)
or honest outcome fallback. Do not claim it is a structured AI summary. No
invented historical duration, verified identity badge, readiness verification
or provider result. Keep existing detail transcript, appointment information
and legitimate actions. Rendering/expansion invokes no deletion API.

### C4 — Settings parity and single ownership

Extract or factor the existing Settings presentation around ONE state owner;
never instantiate two SettingsView owners to split sections. A sheet can render
account sections that close over the same state/methods as the Kevin page.
Do not duplicate profile/integration loaders or trigger writes during routing.

| Existing control | Native destination and preserved rule |
|---|---|
| Setup status / permissions / Kevin number | Kevin; retain honest unverified forwarding language and existing permission rules. |
| Screen all calls, previously in Recents | Kevin / How Kevin answers; captured-auth pending save, visible failure, revert to confirmed effective value, existing contacts bypass inversion. |
| Spam tone, urgent alerts, mode switch | Kevin; preserve urgent write fence and mode/plan separation. |
| Personal contact sync | Kevin; preserve contacts-upload consent, OS permission and status/errors. |
| Business hours, service/business fields | Kevin; Business-only existing conditions, errors and save behavior. |
| Knowledge editor/import/recording | Kevin; preserve all existing editor/permission/save flows. |
| Jobber and Google Calendar | Kevin; preserve connection flows and mode conditions. |
| Forwarding instructions, carrier toggle, activate/deactivate/clear, dial-in/PIN if present | Kevin; retain all existing platform/carrier conditions and confirmations. |
| Name, account country, regulatory street/city | Account Settings; regulatory-address correction stays available in Personal mode when the country requires it. |
| Plan, paywall, restore/manage subscription | Account Settings; reuse purchase flows and unchanged forced paywall. |
| Account deletion, confirmations/errors/cancellation | Account Settings; preserve every existing confirmation and pending-dismissal cancellation. |
| Feedback/support, privacy/terms, about/version, DEBUG-only diagnostics | Account Settings; preserve existing destinations and conditions. |

Inventory every current section/control while extracting; include anything less
prominent not named above. Preserve existing preference/write/load fences. The
new screening toggle must have actual pending/error/retry-or-revert behavior,
not the existing silent `try?` optimistic write. Keep Text reply disabled.

### C5 — Honest error evidence ceiling

The existing backend `get_calls_for_contractor` catches database exceptions and
returns an empty list. Without changing that contract, native cannot distinguish
this case from valid empty history. Record that inherited limitation. This slice
handles only observable transport, HTTP and decoding failures; do not claim a
transport fixture proves database-outage recovery. No backend behavior changes.

### C6 — Visual fidelity, accessibility and fixtures

Use the approved warm ivory canvas, ink text, fine dividers, restrained cobalt,
forest pickup and one dark live-call card. Use native system typography and
navigation, not a WKWebView wrapper. Adapt colors to dark mode with readable
contrast. New rows and controls use semantic fonts, wrapping names/snippets and
reflowing actions at accessibility sizes; minimum44pt targets. Expose selected
filters, unread state and live state to accessibility. Respect Reduce Motion in
decorative pulses and button scaling. Localize new strings/plurals/dates in the
existing en/es/pt-BR catalog without rewriting unrelated translations.

DEBUG-only screenshot fixtures must exercise the ACTUAL production ContentView,
history model and Settings presentation, not a duplicate screenshot UI. Retain
existing fixture scenarios and add bounded-history101, history-empty,
history-error, history-retained-error and Kevin/account review scenarios. The
fixture harness suppresses persistence, network, phone/message/calendar/provider
navigation, authentication, billing and deletion, including every newly reachable
Settings action. It may display a local fictional result explicitly as simulated.
Release/Staging configurations cannot activate fixtures. No screenshot harness
or review controls in production. No caller/query/token logging.

### C7 — Acceptance and evidence

Required real unit checks (builder writes tests; parent executes them):

1. Actual production decoder: valid empty, valid calls, malformed200, 401,403,5xx,
   missing stable ID and invalid timestamp; no AppState side effect.
2. Actual history store: response2 before response1, error1 after success2, auth
   A→B and A→B→A, credential rotation, pending spinner invalidation, retained
   retry, server-read hydration only on owned commit. No stale rows/badges/reauth.
3. Policy:0/19/20/21/99/100/101+, duplicates/equal timestamps, exactly90days,
   phone formatting, reason hit beyond20, query/filter reset, page cap and
   mark-all-read over hidden rows. Deterministic clock and fixtures.
4. Navigation/actions: legacy routes, old A while B active, stale presented
   detail after auth change, exactID outside page/filter, return preserving query
   and expansion, screen dismissal versus call ownership.
5. Actual root observer: one request across tab/sheet changes, background/auth/
   call replacement invalidation, late cleanup cannot stop B, unknown retains
   call, matching ended clears only owned lease.
6. Existing native suite, including all call-action and settings/country tests.

Parent mutation probes must fail when request/auth/presentation/poll guards or
history cap/search-before-pagination are removed. Static-only or helper-only
tests do not prove integration; production must call the tested paths.

Parent simulator checks: actual root in Personal/Business, 101 calls and search
beyond20, light/dark, small iPhone, large Dynamic Type, long labels, all empty/
error states, live-card navigation, Kevin/account parity, selected/read
accessibility values and focus after expansion/detail dismissal. Screenshots
alone cannot establish VoiceOver behavior or physical call acceptance.

After focused tests and visual review, fresh-context independent review of the
actual diff, then one coherent commit/push/PR. Bind repo/owner/billing entity,
exactHEAD, clean status, current workflow triggers, required Test, fan-out,
timeouts, caches/artifacts and zero chargeable public-Ubuntu Actions cost first.
No paid Actions allocation; do not retry remote review bots with exhausted quota.
Wait for exactHEAD CI and fix/re-review valid findings before reviewed-clean merge.

Next internal TestFlight package requires a fresh release envelope, available
build number, wrapper-managed archive/export, signed-package identity and
entitlement inspection, Apple validation/processing and QA availability. Preserve
all build38 notification capabilities. As planned, public App Store release required
separate owner approval; on September 15, 2026, the owner instructed submission of
the build, and public App Store release **1.3.0 (39)** is completed (`READY_FOR_DISTRIBUTION`).
Physical iPhone and VoiceOver qualification remain open. No backend deployment is
introduced by this slice.

## Builder output

Return JSON with `changed_files`, `implementation_notes`, `new_test_names`,
`known_limitations`, `out_of_scope_requests`, and `done`. Do not claim tests ran.
Stop if the pinned behavior cannot be implemented within the allowlist. Leave
all edits uncommitted. Parent inspects and executes acceptance independently.

## Native integration supplement

The Settings owner is a persistent `SettingsHost<Root: View>` around the two-tab
root. It keeps the existing state and helpers, supplies its assistant screen to
the root, and presents account sections in its own sheet. Both labeled Settings
buttons open that same sheet without changing the selected tab. Dismissing the
account sheet cancels pending deletion confirmation. Returning to the current
call from the account sheet captures the lease, dismisses, then validates and
opens it after dismissal. The legacy calendar announcement routes to Kevin.

The root controls one production `LiveCallObserver.shared` lifetime. Existing
AppDelegate/KevinApp calls to AppState.checkForActiveCall must delegate to this
same observer, with no duplicate polling/discovery implementation. Observer
request deduplication and auth/scope/lifecycle guards apply to every entry point.
No observer starts provider work in fixtures or outside the active scene.

Fixture auth is a DEBUG-only in-memory context returned before any token read;
never write a fixture credential into Keychain. AppState fixture persistence
guards may be completed where newly reachable seeded state needs them. Fixtures
must use the actual ContentView, injected history fetch/effects and fixture
clock. Preserve pure navigation/search/filter interactions while disabling all
external actions. Add stable accessibility identifiers for history search,
count, rows, expansion, tab navigation, account Settings and call actions so
the UI tests exercise production controls.

History review tightened malformed-record handling: an invalid required ID or
timestamp makes the response an error. It is never silently discarded into an
empty successful account. Historical urgency badges are omitted because this
schema has no authoritative urgency field; live urgency uses the coordinator.

The draft audit found that active-call state lacked its producing auth context.
Store origin auth on adoption and derive display/polling leases only from that
origin. Require producing auth at each setActiveCall boundary, clear the old
transcript when identity changes, and immediately reject retained state from
another auth lifetime even before notification-driven cleanup. The CallManager
allowlist is expanded only to pass its already captured auth into setActiveCall;
CallKit, transport and physical-call teardown remain unchanged. New isolated
AppState ownership tests are allowed. Connected-call UI must validate the
existing CallManager presentation lease without disconnecting on passive UI
dismissal.

The Settings audit also requires real load coalescing, dirty-draft preservation,
screening/spam/urgent preference fences, visible business-hours save failures,
reachable paywalls from Kevin and Account, and comprehensive fixture guards on
external sections/tasks and AppState persistence. New production-used Settings
load/save policy helpers and focused tests are allowed when needed; preserve
the single SettingsHost state owner.
