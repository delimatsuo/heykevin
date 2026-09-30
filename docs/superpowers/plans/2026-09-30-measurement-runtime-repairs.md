# Runtime qualification repair

Same measurement base/worktree/envelope. Keep all prior restrictions. Parent ran
the focused suite: 396 passed, 3 failed, 22 setup errors. Do not claim completion
until actual named missing tests exist. No tests/builds/Git/provider/secret calls.

Fix these concrete errors:

- Define MAX_TIMESTAMP_S=4102444800.0 beside MIN_TIMESTAMP_S in acquisition.py.
- Task is a Swift struct: replace activeTask === task with a captured UUID flight
  ID, assign/reset alongside activeTask and require matching ID for cleanup.
- tests/unit/test_acquisition_api.py uses nonexistent _TOKEN_CACHE and
  _CONTRACTOR_TOKEN_MAP. Actual auth module has only _token_cache. Clear that and
  set dummy api_bearer_token in the autouse fixture so unauthenticated path is401.
  Fix synchronous get_contractor mock to an async function/AsyncMock. Rename
  test_app to route_app to remove pytest collection warning. Test actual contractor
  POST, captured contractor identity and rejection of an extra contractor_id.
- create_contractor round-trip test phone +15555550123 fails real normalization.
  Use reserved +14155550123 or omit owner_phone; don't weaken production validation.
- In subscription.py DID_RENEW/SUBSCRIBED branch capture update_contractor's bool
  and invoke record_payment_measurement ONLY if result is True. Keep existing
  notification return/processing semantics otherwise; don't redesign billing.

Add these still-missing essential behavior tests to allowlisted new test files:

1. Actual update_subscription_from_transaction and handle_appstore_notification
   measurement hooks: accepted -> once; false update/rejected payload -> zero;
   thrown update -> zero; measurement throws -> existing successful entitlement
   result still succeeds. Reuse fixture patterns from test_subscription_verification
   and test_subscription_security. Mock boundaries but EXECUTE the actual functions.
2. Parameterize held-network finalization test for deactivated, deleted and
   disabled (it currently covers deactivated only). Add disable after entry but
   before transaction using a fake transactional wrapper toggle. Assert no write.
3. Streaming fake asserts exact hardcoded URL, POST, text/plain, redirect=False,
   configured timeout; yield 16KiB then one byte then a sentinel chunk that must
   not be requested; assert stream closes and sentinel is never consumed/logged.
   Keep tiny-deadline test. Test HTTP404/500 and invalid JSON as bounded retries.
4. Actual record_payment_measurement twice: first Personal then BusinessPro,
   immutable intent and original first-tier/date remain Personal; Sandbox/free
   trial/missing price cannot increment paid. Use valid full fake cohort schema.
5. Swift held-await account replacement, signout and same ID/token generation
   rotation. Assert replacement flight starts while old operation is held, old
   completion cannot clear replacement ID, requests use captured bearer and stale
   work stops. Add cancellation during token and sleep to existing eligibility/
   POST coverage. Use bounded entry waits and release all held gates before joins.
6. Purge test currently reconstructs a comprehension instead of executing purge.
   Use fake patterns in tests/unit/test_purge.py and call actual _purge_sync with
   a deactivated/deletion-requested record containing measurement; assert removed.

No changes outside the prior allowlist. Return actual edits and missing parts in
strict JSON. Parent owns lint, tests, mutation probes, independent review and Git.
