# Jobber native management — pinned source slice

Base: `8cc4a01e85e5c3e0c741037722c18aa7aba04735`, tree
`514fabb54905c68de0dfd556b97e304eea3252e7`.

Master owns architecture, audit, tests, and Git. Builder: headless
`agy`, `gemini-3.7-flash-high`; this is standard Swift/backend implementation
after independent provider-contract and source-architecture analysis (2/2
returned). Builder never mutates Git or reads dotenv/credentials. No cloud,
provider, deployment, flag, email, paid work or upload is included.

## Behavior and invariants

Complete the native management portion of J5: authenticated iPhone controls
show truthful server status, capture availability, operation feedback, and
local deletion versus provider revocation. Public setup/manage pages provide
a fixed navigation handoff only. This does not complete authenticated browser
management, direct Marketplace installation, provider disconnect reception,
provider acceptance, or Marketplace readiness.

1. Add a Foundation-only Jobber management policy/model with strict response
   parsing and an account-bound single-operation fence. Status requires exact
   JSON booleans `connected` and `lead_capture_enabled`; reject numeric/string
   substitutes and the inconsistent disconnected/capture-enabled combination.
   `connected=false` means **Connection not ready**, since server status also
   includes quarantine/incomplete states. Never infer provider revocation from
   this value. `connected=true` shows Connected, with Lead capture enabled/off.
   No client control may enable the protected capture flag.
2. A disconnect is locally acknowledged only on HTTP 200, explicit
   `status=disconnected`, matching contractor_id, `provider=jobber`, and
   credential_deletion.status in executed/partial_reconciled/legacy_reconciled.
   Only nested provider_revocation.status=provider_confirmed establishes
   provider confirmation. provider_rejected/transport_error_unknown/
   not_attempted_unavailable_token are locally disconnected with an explicit
   unconfirmed-provider notice and instructions to check Jobber's Apps page.
   Missing/unknown revocation status must never become provider confirmation.
   Missing/mismatched local identity/deletion ACK fails parsing, preserves the
   last UI status, and offers status refresh. Never render raw server bodies or
   error objects. A foreground status read must not erase a revocation warning.
3. All native Jobber requests use an explicit captured CallAuthContext bearer
   and contractor ID, not mutable shared authentication. Add dedicated typed
   APIClient methods. Use maxRetries:0 for connect and disconnect; one tap
   yields at most one request. All methods validate HTTP status before parsing.
   Keep generic Calendar methods unchanged. Authorize URL must be HTTPS,
   exact api.getjobber.com, exact /api/oauth/authorize, without user/password,
   port or fragment. Do not print URL, code, state, token, body or Error.
4. Model operation leases include UUID and complete captured auth. Ignore
   stale status/mutation/browser-open completions after another operation or
   account/session-generation change. Reset all status/notice/busy state on
   auth change. Refuse overlapping operations. Show busy state and disable
   duplicate actions. Unknown initial status offers Refresh rather than
   advertising a cached connection. Refresh failure preserves last known
   status with a fixed visible message. After successful browser opening,
   prompt return to Kevin and refresh. Use the existing foreground coalesced
   refresh path, plus a manual Refresh. No automatic reconnect/disconnect.
5. Use the exact navigation URL `heykevin://integrations/jobber`. Reject any
   variant with user info, port, query (even empty), fragment, extra path,
   percent escapes, different scheme/host, or trailing slash. Routing only
   selects the Kevin tab and scrolls to Jobber. Require ready valid auth,
   business mode, and no active/pending connected-call presentation. Never
   sign in, create/switch account, change mode, provision, or start OAuth from
   the link. Unauthenticated links may be ignored; static pages explain sign
   in then reopen. Clear pending scroll on auth changes. Reuse the existing
   navigation/SettingsHost pattern, preserving Calendar routes and sheets.
6. Register only this custom scheme in XcodeGen and Info.plist. Add the fixed
   link to Jobber setup, add GET /api/integrations/jobber/manage as a static
   safe handoff page (same security headers/no datastore/provider work), and
   add a fixed return link on the Jobber success page. Manage ignores every
   query input and never displays account data. No-state callback behavior
   remains unchanged and discards unsolicited code. No new browser session,
   cookies, code exchange, identity index, flags or webhook receiver.

## Strict builder allowlist

- ios/Kevin/Models/JobberManagement.swift (new)
- ios/Kevin/Models/FrontendNavigation.swift
- ios/Kevin/Services/APIClient.swift
- ios/Kevin/Views/SettingsView.swift
- ios/Kevin/Views/ContentView.swift
- ios/KevinTests/JobberManagementTests.swift (new)
- ios/KevinTests/FrontendNavigationTests.swift
- ios/project.yml
- ios/Kevin/Info.plist
- app/api/integrations.py (static HTML/routes only)
- tests/unit/test_jobber_management_handoff.py (new)

Do not edit this brief. Stop if a requirement needs any other runtime file.
Master regenerates Xcode project after builder returns. No unrelated cleanup.

## Acceptance and independent verification

- Strict parsing: true/false, numeric/string/null/missing rejection,
  inconsistent capture state, non-200, malformed JSON, matching/wrong tenant,
  each known revocation outcome and absent/unknown outcomes.
- Single-operation fence, stale refresh after disconnect, double tap,
  A-to-B and A-to-B-to-A generation changes, browser-open failure, timeout,
  status-failure preservation and provider-warning preservation.
- Captured request identity/auth, exactly one connect/disconnect on HTTP 500,
  invalid authorize destinations, fixed errors without sensitive values.
- Exact-link happy path plus adversarial URL cases, invalid/not-ready auth,
  Personal mode, active/pending call; no mutation triggered by navigation.
- Static setup/manage/success fixed URLs, privacy headers, supplied hostile
  query values absent from responses, no DB/provider/auth side effects.
- Focused Python tests, real unsigned iOS build/tests through the required
  wrapper, mutation-effective guard probes, independent fresh-context diff
  review. Public PR requires separate exact-head zero-cost CI preflight.

Primary provider sources: [OAuth](https://developer.getjobber.com/docs/building_your_app/app_authorization/),
[Manage App](https://developer.getjobber.com/docs/publishing_your_app/manage_app_button/),
[webhooks](https://developer.getjobber.com/docs/using_jobbers_api/setting_up_webhooks/).
None documents sufficient grant ordering for a destructive delayed webhook.

## Independent audit repair contract

The first actual unsigned build failed because public declarations exposed
internal CallAuthContext. Keep all new declarations internal, import Combine
explicitly for ObservableObject/Published (Foundation/Combine, no UIKit in the
model). Focused Python checks passed 56; independent review found five valid
issues. Repair these within the original allowlist, including tests:

- Inject a live `currentAuthProvider: @MainActor () -> CallAuthContext?` into
  JobberManagementModel. Production default returns nil unless AppState is
  ready, onboarded, valid and in business mode. Every request must compare
  supplied auth to this live provider before starting AND on each completion.
  Tests inject mutable equivalent session context. Never assign currentAuth
  from an obsolete request. A genuine changed live auth resets bound state;
  stale supplied auth is rejected without request/state/busy changes.
- Finish/cleanup only the matching UUID lease, even when a result is stale.
  An overlapping different-auth call cannot invalidate another operation or
  leave busy stuck. Live admission/completion checks supplement host onChange.
- If a foreground/manual status refresh arrives during an operation, retain
  one coalesced account-and-generation-bound follow-up status read. Drain it
  once after the owning operation; drop it on auth/readiness/business change.
  Never queue or automatically repeat a mutation; no failure retry loop.
- Browser opener is asynchronous and returns Bool. Show return-to-Kevin prompt
  only after true. False/throw/timeout keeps a fixed error with no success
  prompt. Check live auth/lease before open and after completion; stale
  completion never changes state or clears a newer busy lease. UIKit opener
  runs on MainActor and checks current ready context before opening.
- Consolidate view actions through existing connect/disconnect helpers and a
  refresh helper, rather than copying tasks into every button. Fence any
  legacy AppState.jobberConnected writes with live ready auth. Mode/session
  changes cannot admit old profile-load plans or mutate new-session UI.
- Require explicit session readiness, active call and blocking-presentation
  inputs for native route admission. ContentView passes current ready,
  onboarded, paywall/WhatsNew and CallManager state. Navigation is ignored if
  any safety gate blocks; existing call and sheet priorities remain intact.
- If nested provider_revocation.status and top-level revocation_status are
  both present but disagree, never confirm provider revocation; reject the
  inconsistent response. Prefer computed isProviderConfirmed from the parsed
  enum rather than an independently supplied Boolean.
- Shared OAuth success markup must add Jobber's return link only for Jobber.
  Preserve Calendar's original behavior. Setup/manage directions refer to the
  current Kevin tab, then Integrations, then Jobber.
- Add URLProtocol tests of the actual APIClient methods: captured contractor
  and token travel together; HTTP500 connect/disconnect issue exactly one
  request; malformed/non-200 responses do not become successful state. Add
  live-auth changed without onChange, stale request admission, A-B-A generation,
  overlapping auth, browser false/late completion, deferred foreground read,
  not-ready/Personal/active-call/paywall route, and Calendar success regression.

No implementation or test widening outside the original allowlist. Master
owns verification and Git. Do not make unrelated code public to fix visibility.

## Final repair contract from executed checks and re-review

The actual focused Python run has 94 passes and two outdated Settings-copy
assertions. The actual simulator test build fails because MockURLProtocol
collides with another test file. Repair only the following remaining issues:

- JobberDisconnectParser must derive revocationStatus ONLY from nested
  provider_revocation.status. A top-level revocation_status is a consistency
  check when both fields exist; it has no confirmation authority by itself.
  Top-level-only provider_confirmed and missing/malformed nested status must
  remain unconfirmed. Preserve rejection when both strings disagree. Test
  top-level-only confirmation and malformed/missing nested status explicitly.
- handleAuthChange treats its argument only as a notification. Read the live
  currentAuthProvider, normalize invalid to nil, and reset only when that live
  value differs from currentBoundAuth. Never fall back to the supplied stale
  context. Duplicate or delayed notifications must preserve a valid current
  busy lease. Test delayed A notification while live B operation is busy,
  duplicate B notification, and nil live readiness.
- Browser opening needs a bounded failure path: allow async throwing opener,
  with a production default timeout of 10 seconds, injectable short duration
  for tests. False, throw, cancellation and timeout all use the same fixed
  connect error and clear only their own lease. Timeout must actually return
  without waiting indefinitely for a non-cooperative opener; avoid a task
  group that waits for the blocked losing child. Cancel/finish race handles
  idempotently on MainActor, ignore late opener completion, and retain live
  auth/lease fencing. Test throw, timeout and late completion after a newer
  operation starts. No second connect request or opener invocation.
- Rename the new test-local MockURLProtocol and all its uses to private
  JobberManagementURLProtocol. Do not touch other test classes.
- Make testModelDeferredStatusRefreshDrainedAfterOwningOperation's callback
  one-shot before it requests the nested refresh; its current callback
  recursively schedules a fresh request on every drain.
- Narrow allowlist extension: tests/unit/test_jobber_pkce.py, ONLY the two
  obsolete assertions `assert "Settings" in body` at the setup/no-state
  callback HTML tests. Assert the truthful Kevin-tab instructions instead,
  preserving every other no-DB/privacy/state/security assertion. Do not add
  incorrect UI wording to production HTML to satisfy stale tests.

All other original constraints hold. Master owns test/build/Git operations.

## Browser cancellation completion repair

Executed focused Python: 96 passed. Actual-source Swift harness: baseline
passed and all seven Boolean/confirmation/navigation/live-auth/lease/busy
guard mutants failed as intended. Xcode wrapper refused safely because a
different guarded job holds the persistent lock; no test ran in this attempt.

Read-only re-review finds one real remaining defect: canceling the enclosing
connect task does not cancel its unstructured opener/deadline tasks or resolve
its continuation. If the opener then returns true before the deadline, the
cancelled task displays the success prompt. Only edit JobberManagement.swift
and JobberManagementTests.swift for this repair:

- Resolve the browser race on parent cancellation, with exactly one completion
  and the same fixed connect error. Check cancellation before starting the
  browser and before accepting success. Never clear a newer lease. Preserve
  timeout, false, throw, live identity and UUID checks.
- A small thread-safe single-resolution helper containing the continuation
  and pending result is acceptable: cancellation can happen before the
  continuation is registered. withTaskCancellationHandler must handle that
  race, and cleanup must cancel loser handles. Do not use a structured task
  group that waits indefinitely for an unresponsive opener. The helper must
  not retain an active model/view after parent completion.
- Add a deterministic controlled-continuation test: await browser entry,
  cancel parent connect task, verify bounded fixed-error completion, then
  resume the suspended opener true and prove no success/state mutation.
- Replace the purported non-cooperative timeout fixture with a checked
  continuation that ignores cancellation until explicitly resumed by the
  test. Resume it after timeout to clean up; prove timeout completed first.
- Hold a newer operation genuinely busy on a second controlled continuation
  while the old opener resumes. Assert the newer UUID and isBusy stay intact,
  then finish/await both to leave no pending test tasks. The existing late
  completion test currently finishes the newer refresh before resuming old
  work and therefore does not prove this property.

No other file changes, Git operations, test/build execution, network or
credential access. Tier remains gemini-3.7-flash-high for cancellation races.

## Final audit coverage and capture repair

The fresh independent diff audit found two final P2 issues. The cancellation
resolver itself passes source audit. Only edit SettingsView.swift and
JobberManagementTests.swift for these bounded changes:

- In performJobberConnect, the injected browser opener must explicitly capture
  `[appState]` plus its already-local auth value, so a stalled opener does not
  implicitly retain the entire SettingsHost and its Jobber model after timeout.
  No other UI behavior changes.
- Add an independent elapsed bound to the controlled parent-cancellation
  regression (e.g. completion less than 2 seconds versus production default
  10-second timeout), so removing cancellation handling cannot just wait for
  timeout and still pass the test.
- For cancellation, true non-cooperative timeout and late-result/newer-busy
  tests, explicitly signal/await completion of the old opener after resuming
  its checked continuation. Replace each 20ms sleep with that deterministic
  signal. Keep the newer status operation suspended until assertions on its
  UUID/busy state pass, and then resume/await it for cleanup. Ensure guard
  failure cleanup also resumes and awaits controlled tasks.

Preserve all parser, lease, API, navigation and localization behavior. No
model changes, unrelated test cleanup, tests/builds, Git or network access.

## PR 289 valid Codex navigation finding

Exact-head CI at 7f5a6f0 passed and configured Codex review completed. Its P2
finding is valid: ContentView's hasBlockingPresentation omits the existing
root historical/live-detail/unavailable-notification sheet, so a Jobber link
can change tab/scroll state behind that active sheet.

Only edit ContentView.swift, FrontendNavigation.swift and
FrontendNavigationTests.swift for this repair:

- Pass `frontendNav.presentedSheet != nil` in ContentView's existing blocking
  presentation expression, preserving paywall/WhatsNew/call inputs.
- FrontendNavigation.handleDeepLink also rejects its own non-nil
  presentedSheet, even when a caller omits or falsely passes the blocking
  input. Preserve the active sheet, selected tab, scroll flags and other
  navigation state on rejection. Do not alter any account/call-sheet behavior.
- Add production-model regression coverage for existing historical detail,
  live-call detail and unavailable-notification sheets. With an otherwise
  ready/onboarded/business/valid session and canonical Jobber link, each must
  reject without changing navigation or sheet state. Keep the happy path.

No tests/builds or Git operations by builder; master verifies and publishes.
Tier gemini-3.7-flash-high for bounded navigation implementation and tests.
