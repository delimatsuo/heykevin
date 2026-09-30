# Final test isolation repairs

Master-owned brief. Same base ba964d035325b8ba153a37e3ff2dc449130eeb60 and
measurement worktree as the prior plan. Production review found no remaining
P1/P2 issue. Parent qualification: 14 Swift tests passed; Python run was stopped
after 65 passes and two failures because an unmocked notification lookup reached
Firestore credential refresh. Fix tests only; do not alter production code.

Builder: headless agy gemini-3.7-flash-high, because async fixture boundaries and
held-flight sequencing require behavioral implementation. One writer. No Git
mutations, tests/builds, provider/network calls, .env/credentials, other worktrees,
dependencies, or raw Xcode. Parent owns verification. Allowed writes ONLY:

- tests/unit/test_acquisition_api.py
- tests/unit/test_acquisition_measurement.py
- tests/unit/test_acquisition_wiring.py
- ios/KevinTests/AcquisitionCoordinatorTests.swift

Required repairs:

1. Route POST test must patch app.api.acquisition.process_apple_ads_attribution,
   the router's directly imported binding, not the service module binding.
   Preserve authenticated contractor ID and token assertions, and no extra call
   on rejected extra contractor_id. Do not exercise real Apple transport.
2. In each new Python test module, add an autouse test-only outbound tripwire:
   patch httpx.AsyncHTTPTransport.handle_async_request to raise on real transport
   (ASGITransport stays usable); patch requests.sessions.Session.request to
   raise; patch google.cloud.firestore.Client.__init__ to raise. This prevents
   credential discovery and real HTTP/Firestore while test-local fakes still
   override the actual entry bindings. If existing unit tests in these modules
   depend on unmocked external behavior, replace it with explicit fakes rather
   than weakening the tripwire. Do not mutate suite-wide conftest or production.
3. _PurgeFakeCollection.list_documents must follow the actual fake in
   tests/unit/test_purge.py: enumerate direct children INCLUDING phantom parents
   that have only descendants. Assert _purge_sync's real result contract:
   positive purged_at, deleted contacts count=1, persisted tombstone purged_at
   equals result, no acquisition map/PII, contact removed. Do not assert a
   nonexistent purged key. Keep executing the real _purge_sync.
4. test_handle_appstore_notification_measurement_hooks exercises EXPIRED and
   currently calls real push_notification.get_device_token. Patch that exact
   binding with async None, and patch send_regular_push with a fake that raises
   if unexpectedly called; preserve real handler execution and zero measurement
   for expiration. Other accepted/rejected/failed update assertions must remain.
5. Strengthen Swift generation-rotation test to change ONLY generation while ID
   and bearer remain identical. Add one bounded held-flight regression: hold old
   operation, replace account and start a new flight held in its own operation,
   release/join old flight while replacement stays pending, call start again for
   replacement and assert it shares the existing flight instead of issuing a
   second operation. Release all gates and join every task. No unbounded spin or
   await on unreleased continuation; honor existing entry timeout conventions.

Return strict JSON with changed_files, exact repairs, remaining_gaps. Do not
claim tests passed; only parent may run them. Do not read or edit this brief.

## Parent qualification follow-up: deterministic concurrency fixtures

The 436-test focused suite passed. Sandbox, FREE_TRIAL and enablement mutations
were detected. Two test sequencing repairs remain, with no production change:

- In test_duplicate_request_while_attempt3_in_flight_blocks_exhaustion, pin
  acquisition.time.time to a mutable clock initialized to the local now value.
  Immediately before the duplicate lease call, advance the fake clock by 6.0
  seconds. This is after 5-second spacing but before the 15-second lease expiry,
  so removing the active-lease guard must fail this test. Use
  asyncio.wait_for(network_entered.wait(), timeout=1) for bounded entry and
  finally release the network gate and cancel/join task1 on assertion failure.
- In testHeldFlightRegressionOldCompletionDoesNotCancelReplacementFlightAndConcurrentStartSharesFlight,
  add a third-start entry expectation, fulfill it immediately before taskB2
  awaits evaluateAndRecord, and await its fulfillment before releasing releaseOpB.
  In testSameContextConcurrentStartSharesSingleFlight, use the same entry
  expectation pattern for task2 before releasing releaseToken. This guarantees
  the intended overlapping start executes while the existing flight is held.
  Keep the post/token count assertions and bounded timeouts.

These are mechanically pinned test edits: use gemini-3.7-flash-medium. Only write
tests/unit/test_acquisition_api.py and ios/KevinTests/AcquisitionCoordinatorTests.swift.
No test execution, source edits, Git mutation, or provider access.
