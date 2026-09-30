# Final measurement repair envelope

Fresh builder context. Base ba964d035325b8ba153a37e3ff2dc449130eeb60.
Work only in this measurement worktree. Read the current files and original
measurement plan plus review-repairs amendment for the pinned contract. All their
file allowlists and no-tests/builds/Git/provider/secret/plan-edit restrictions apply.
The previous builder's completion claims are not evidence. The current candidate
has real failures and missing tests; repair the actual files below.

## Required production fixes

1. scripts/summarize_acquisition_funnel.py must REQUIRE declared_onboarding_intent
   exactly personal/business/unknown. Current server creates unknown, but report
   rejects it. Require attribution_status key (null allowed for initial cohort).
   Guard enum scalar types before set membership; lists/dicts must yield ValueError
   without values. Lease fields must be paired; lease_attempt strict integer1..3
   equal attempts, expiry finite >=last_attempt_at and <=last_attempt_at+15.
2. app/services/acquisition.py: ONE shared strict cohort/budget/lease validator
   used by eligibility, reservation and both finalizers. schema_version exact int1;
   attempts REQUIRED int0..3; created_at/cohort finite 2001..2100 and equal;
   last_attempt_at numeric finite >=created_at when attempts>0. Non-null lease
   fields paired, lease_attempt exact int1..3 equal attempts, expiry as above.
   Reject corrupt state without writes/network. No bool equality/coercion.
   A stale finalizer returns public retryable/ineligible/terminal status, never
   stale_lease or null; do not disturb a newer lease. Terminal states cannot be
   overwritten. Valid third outstanding lease blocks duplicate exhaustion.
3. Call/payment milestone writes reject NaN/infinity/bool/string and pre-cohort
   observed timestamps. Do not coerce invalid input or manufacture observed time
   with max(). Every helper must preserve early disabled zero-DB and transaction
   enabled/active/existing fences. Validate known entitlement tier before write.
   Ignore invalid optional provider keyword IDs including 1234567890.
4. iOS AcquisitionCoordinator: add caller cancellation propagation to the captured
   owned task using withTaskCancellationHandler; Task.checkCancellation before
   creating work. Retain auth-generation replacement and fence old-task cleanup.
   Do not have duplicate same-context callers accidentally clear a newer flight.
5. AttributionResponseParser: exact keys status/retry_after_seconds only; status
   must be one of seven public backend statuses (unknown is NOT a valid response).
   Present delay must be a non-bool integer NSNumber5..15. Reject fractions, strings,
   huge values, out-of-range values and extra fields to fixed retryable(5).
   No clamping test expectations. Avoid .intValue until numeric range is checked.
   Retryable should have a valid delay; terminal should not contain delay. Keep
   HTTP401/403 ineligible and all non200 otherwise retryable. Remove trailing EOF
   whitespace in APIClient.swift. Do not edit its subscription-verification section.

## Actual local failures to fix in tests

All three new Python modules still fail collection when run alone because config
is imported before synthetic required settings. Copy the module-level dummy-only
os.environ.setdefault bootstrap from tests/unit/test_contractor_mode.py BEFORE
any app imports in each new file. No shell credential exports or .env.

Starlette TestClient is incompatible with installed httpx (`unexpected keyword
argument app`). Convert route tests to httpx.AsyncClient with ASGITransport and
a minimal FastAPI application mounting the actual router, no production startup.
Test actual verify_api_token with dummy admin setting and patched
app.db.contractors.get_contractor_by_api_token, clearing token cache per test.
Include contractor success and capture the authenticated contractor ID passed to
the route's actual imported function binding. Extra target IDs must be rejected.

test_summarize_acquisition_funnel_aggregates_synthetic_cohort uses 1726000000
(September2024) but asks for September2026. Generate epoch from timezone-aware
datetime(2026,9,10). The as_of test sets cohort_end later than as_of, violating its
own API contract; use a cohort ending before as_of with observations after it.

## Tests that must actually exist and execute production paths

Replace the transaction identity decorator with a lock-serialized wrapper around
the actual decorated transaction function. Add parametrized corrupted-budget/lease
cases. Control an in-flight network response with asyncio.Event: reserve attempt3,
send duplicate POST while response held, assert no exhaustion/additional exchange,
release success and assert recorded once. Cover expired lease and stale finalizer,
deleted/deactivated/disabled during response, and disabling between helper entry
and transaction. Disabled helpers must never get a DB client. Restore original
monkeypatch state per test.

Use httpx.MockTransport or a controlled streaming fake to check fixed URL/header,
follow_redirects=False, response closes and stops iterator after16KiB, deadline
returns retryable using a tiny patched HTTP_DEADLINE_SECONDS, and log messages
never contain synthetic exception/token/PII sentinels. Test multibyte8192 boundary.

Execute real create_contractor with fake collection.add to round-trip its unknown
intent map through reducer. Test intent stripping/immutability and real response
redaction/protected PATCH behavior. Execute direct subscription update and active
notification paths with the existing subscription test patterns and a recording
measurement mock: accepted update -> once; failed/rejected update -> zero;
measurement exception must not break entitlement. Execute Twilio evidence helpers
with throttle already satisfied to prove measurement precedes it and exceptions
do not prevent original last-seen update. Execute purge's real tombstone builder
or existing purge function with its fake pattern to prove the map is removed.

Reducer: parameterize malformed IDs, unknown/list enums, NaN/negative/pre-cohort
timestamps, unknown intent, malformed out-of-cohort records, invalid lease pairs;
assert ValueError without sentinels. Test a paid restore signed before account
creation but observed later, preserving as_of; attribution afteras_of staysunknown.
Keep price/free-trial/Sandbox/missing/zero/revoked and upgrade first-tier tests.

Swift: add held-await tests (bounded entry expectation, release before taskjoin)
for cancellation at eligibility/token/POST/sleep, account replacement and signout,
sameID/token generation rotation, same-context concurrent start and oldtaskcleanup.
Tests invoke production coordinator/parser and assert exact request counts and
captured bearer tokens. A removed guard must fail assertions without hanging.

Return JSON listing only changes actually made. Do not claim tests passed; parent
executes all verification and independent review. Do not omit tests while claiming
comprehensive coverage. If unable to complete a requested part, explicitly name it.
