# Purchase completion review repairs

This master amendment supersedes conflicting limits in the original purchase
envelope. Keep its base SHA and all safety constraints. Builder remains pinned
`gemini-3.7-flash-high`; do not execute tests/builds or mutate Git. The first
candidate builds and 45 selected tests pass, but independent review found gaps.

Allowlisted writes: the original five implementation files, plus
`ios/Kevin/Services/SubscriptionManager.swift`,
`ios/Kevin/Services/SubscriptionVerification.swift`,
`ios/KevinTests/SubscriptionVerificationTests.swift`.
Do not edit generated project or either plan. New helpers may live in the existing
new PaywallFlowCoordinator.swift or SubscriptionVerification.swift files.

## Required fixes

1. Close the restore account recapture gap. Production restore currently awaits
   AppStore.sync, then captures whichever account is current. Capture a full
   verification context before sync and pass it into subsequent entitlement
   verification. Include session generation in SubscriptionVerificationContext,
   equality/current-context checks and cache namespace. Preserve all existing
   ownership, retry, parser and transaction-finishing rules. Purchase, restore,
   launch and transaction-update paths must capture/use that full context. Reject
   stale context before network/profile/finish effects; don't recapture the current
   account after sync. Existing APIClient receives the same explicit ID/token.
   Avoid broad receipt redesign. Existing test construction may use generation=0
   default, but production must supply currentAuthContext().generation and compare
   it explicitly.
2. Extract a tiny testable @MainActor restore-sync operation/helper used by the
   production restore method: captured context, current-context predicate,
   injected async sync, injected async verify(context). It must not invoke verify
   if sync suspended across an account or generation change. Test this actual
   helper with held sync continuations; same ID/token but newer generation is a
   different authorization session. Test normal sync->verify ordering, sync error,
   stale context before start, stale after sync and stale after verification.
3. Correct Business activation false-PATCH feedback: verified purchase succeeded
   but setup could not finish; tell the user to use Restore Purchases to retry.
   Do not incorrectly state they need to buy a Business subscription. Actual
   failed entitlement retains its distinct wrong-tier message.
4. Make Business activation itself testable: extract a small production helper
   with captured auth, current-auth, entitlement, injected server PATCH and commit
   closures. It checks validity/context/entitlement before PATCH and context again
   before local commit. False PATCH/throw returns visible activation failure.
   OnboardingView uses it, passing captured bearer credentials and fixed body
   values, with local Business field/step updates only in commit. Add real helper
   tests for success, false/throw, wrong entitlement, invalid auth and account
   rotation while PATCH is suspended. No need to mount the entire private view.
5. BusinessActivation purpose with no activation handler fails closed with visible
   error. Default allPlans may complete without one. Configure should not replace
   an active operation's purpose/handlers while busy; snapshot handlers/purpose or
   reject busy reconfiguration. Test both boundaries.
6. The alternate doneStep must not offer Personal when selectedMode=business.
   Pass businessActivation purpose and awaited activation handler in that case,
   but successful final-step activation must finish onboarding rather than send
   the user back through contacts/forwarding. Reuse the Business helper with a
   caller-selected continuation, preserving the initial Business sheet's contacts
   continuation. For selectedMode=personal keep all plans and Personal preference.
7. Fix the concurrency tests to explicitly signal entry into held activation,
   count purchase and restore invocations, and exercise purchase-vs-restore both
   ways. Do not create an infinite wait when the guard is mutated away: use bounded
   expectations and open the gate before awaiting all spawned tasks on failure.
   Avoid busy-spinning forever on while !isBusy. Add tests for a false purchase or
   restore ensuring activation closure call count remains zero (current fixtures
   define counters they never actually increment).

Return strict JSON describing changed files and tests. Do not claim verification.
