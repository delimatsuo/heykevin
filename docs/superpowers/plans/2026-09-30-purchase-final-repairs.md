# Final purchase repair amendment

Same base, original safety constraints and builder role. Do not run tests/builds,
mutate Git, read secrets/providers, edit plans or touch other worktrees. Permit
the existing purchase implementation/test files plus APIClient.swift (only
verifySubscription's captured-context 401 effect). Do not touch attribution APIs.

Independent review confirms previous implementation repairs but found:

1. SubscriptionVerification.swift references AppStore.sync without importing
   StoreKit. Add the required import.
2. APIClient.verifySubscription still sets global needsReauth on a stale 401.
   Fence this effect on the full captured ID/token/generation inside MainActor.
   Extract a tiny @MainActor response-effect helper in SubscriptionVerification.swift
   accepting statusCode, captured context, current auth and mark-needs-reauth closure.
   Use it from the actual APIClient 401 path and test current401, changed ID/token,
   same ID/token changed generation, and non401 no effect. Preserve response parser
   semantics. Also guard current context inside the deferred verification closure
   in SubscriptionManager before beginning APIClient.verifySubscription; the actor
   coordinator can suspend after the outer guard and before starting that closure.
3. All four concurrency tests await task2.value before gate.open, which hangs if
   the busy guard regresses. The AsyncGate has only one waiter slot and overwrites
   a prior waiter's continuation. Fix the gate to store/resume ALL exit waiters.
   Have task2 fulfill an XCTestExpectation on completion. Await fulfillment with
   timeout <=1 second, assert call counts, OPEN THE GATE, THEN await task1/task2.
   No path may await an operation that can still be held behind a closed gate.
   Keep bounded entry signaling and resume all continuations in cleanup. A guard
   mutation must produce a test failure, not a test hang or continuation leak.
4. Add actual held-continuation tests for restore-sync and Business PATCH: start
   task, wait bounded entry, assert verify/commit zero, externally change generation
   (also one account change), release gate, join and assert no verify/local commit.
   Existing tests changing generation synchronously inside mocks are insufficient
   proof of suspension behavior. Use the same safe gate pattern; never spin.

Return actual changed file/test list in JSON. Do not grade verification yourself.
