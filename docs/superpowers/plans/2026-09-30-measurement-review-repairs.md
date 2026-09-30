# Measurement review repairs

Master amendment to the acquisition measurement plan, same base and write envelope.
Keep the feature default off. The first candidate failed collection of all three
new Python modules; no tests passed. Independent review found the following real
defects. Implement these bounded repairs; do not run tests/builds, mutate Git,
read secrets, invoke providers or edit plans. Use the prescribed headless builder.

## Configuration, account and transaction fences

Use one canonical `apple_ads_expected_org_id` setting, default 0, validated as a
strict positive signed-64-bit integer and not a known development placeholder.
Remove the redundant `apple_ads_org_id` alias. Cohort creation and all collection
helpers use the same eligibility function. Before obtaining a database client,
every collection helper returns when disabled. In every transaction, recheck
configuration and active=True, existing document and strict schema_version=1.
This applies to leases, attribution completion/exhaustion, call observations and
payment observations. Never recreate deleted or deactivated accounts. Retain
failure isolation for telephony and subscription flows. Log fixed categories or
exception class only, never exception text, raw token, payload or traceback.

Validate stored attempt state strictly: integer 0..3 (bool is invalid); positive
finite last_attempt_at required when attempts>0; no coercion/reset of corrupt
state. Invalid budget state is ineligible without a write or external request.

## Explicit outstanding attribution lease

Fix the race where a duplicate request exhausts the third attempt in flight.
Use `lease_attempt` (integer 1..3 equal to current attempts) and
`lease_expires_at` (timestamp). Reserve them transactionally with the increment
and last_attempt_at. Lease lifetime 15 seconds; total HTTP exchange deadline 8
seconds. An unexpired lease returns retryable with a bounded 5..15 second delay,
without consuming another attempt or marking exhausted. Require valid spacing
metadata and five seconds since last reservation. After a crashed lease expires,
the next request may reserve remaining budget, or mark exhausted if three were
used. Reject inconsistent lease fields as ineligible, fail closed.

Both successful terminal finalization and failed-attempt finalization receive
the captured lease_attempt. They mutate only if that same lease still owns the
account, collection remains enabled and the account is active. On failure clear
the lease fields; mark retryable if attempts<3, exhausted if attempts==3. On
success record the one terminal attribution and clear lease fields. A stale
completion cannot clear a newer lease or replace any terminal result. Do not let
a third concurrent duplicate suppress a successful third response. Refactor the
old unscoped mark_attribution_exhausted helper as necessary within the service.

Stream the hardcoded Apple POST using httpx client.stream; stop reading at 16KiB,
close the response, disable redirects, keep connect/read 3s/5s limits, and wrap
the entire exchange in asyncio.timeout(8). Do not eagerly download before checking
length. Reject malformed payloads safely; invalid optional IDs/enums must never
enter storage. Bound the incoming token by 1..8192 UTF-8 bytes with an explicit
validator, not just a character limit. No raw error logging, including unexpected
exceptions. Clear/finalize leases safely on timeout/transport/parser failures.

## Verified purchase facts

`offerType=1` is introductory, not proof of a free trial. Only explicit
offerDiscountType=FREE_TRIAL with strict integer price=0 qualifies as a trial.
A positive-price introductory offer can qualify as paid; explicit FREE_TRIAL
never does. Preserve Production, known product and revocation gates. Validate
purchaseDate as a strict positive integer in milliseconds, representable from
2001-01-01 through current server time plus five minutes. No bool/float/string.

Record `first_positive_price_purchase_observed_at` and
`first_storekit_trial_observed_at` using the server observation time. Separately
store `first_positive_price_purchase_signed_at` and
`first_storekit_trial_signed_at` from the signed transaction timestamp. Preserve
the first observation, original tier and declared intent across upgrades.
Verified entitlement remains separately named. Ensure all dates recorded as
milestones are finite and at least the account cohort timestamp; a signed purchase
may legitimately precede account creation on restore. No raw identifiers/prices.

## Strict offline reducer

Validate every field, not merely key names. Required core fields: strict schema1,
created_at, cohort (equal to created_at), declared intent enum, strict attempts,
attribution_status enum. Match the actual server-created schema. All timestamp
values must be finite numeric scalars (never bool) within 2001..2100. Observation
timestamps and last_attempt_at must be >=created_at. Signed timestamps may precede
creation, but cannot exceed their observation time plus 300 seconds. Require
paired observation/tier/signed-date fields; tier enum personal/business/businessPro.
Require paired valid lease metadata when present, with lease_attempt==attempts.
Validate every optional ID as a strict positive signed-64-bit integer excluding
development placeholders; enums must match the service allowlist.

Validate attribution combinations: recorded requires attribution=True,
source=apple_ads, recorded timestamp and org/campaign/adgroup IDs; unattributed
requires attribution=False, source=unattributed and recorded timestamp with no ad
IDs/enums; pending/retryable/exhausted/unknown states must not contain terminal
attribution facts. Do not emit any raw input string outside validated enums.
Reject malformed records even outside the selected cohort, with value-free
errors; never silently drop an invalid record then claim complete=True. Add the
two signed timestamp fields and lease fields to the strict allowlist.

An attribution observed after as_of belongs to source=unknown for that report;
do not expose campaign IDs or increment attribution_recorded until its observed
timestamp is in range. All other milestones use their observation timestamps for
as_of, not signed purchase timestamps. Keep [start,end), <=90 days, <=5000 records,
no network, no PII, aggregate output and deletion limitations.

## Swift cancellation and strict parser

Replace the global busy guard with a task owned by its captured full auth context.
Same-context concurrent starts share/drop the existing flight; a different context
cancels the prior task and starts immediately. Signout cancels too. Old task cleanup
must not clear the replacement task. Preserve async public methods for deterministic
tests. Check Task cancellation plus full context after every suspension (eligibility,
token, POST, sleep); propagate sleep cancellation instead of try? swallowing it.
Caller cancellation should cancel owned work via a cancellation handler. Bound token
UTF-8 bytes. Clamp no untrusted delay: strict parser accepts only integer 5..15.
Only HTTP200 with exact known status schema is terminal/success. HTTP401/403 is
ineligible; other statuses/malformed bodies are retryable. No boolean/fractional/
huge NSNumber coercion. Extract a pure parser used by production APIClient so real
response-contract tests can invoke it. Keep at most three POST attempts per flight.

## Required meaningful tests

Bootstrap synthetic required settings before imports using existing test patterns
(no .env or real credentials). Fix route tests to patch actual bindings or use
dependency overrides, proper request.state.contractor_id, and production field
names. Admin GET returns eligible:false and POST403; unauthenticated401. Tests
must execute the real router/helper, not a mocked function bearing the same name.

Add serialized transactional in-memory Firestore fakes that execute the real
transaction functions: durable budget+spacing, malformed counters, all account
deletion/deactivation/disable races, first-writer idempotency, expired leases,
third-attempt success while duplicate arrives, stale finalizer rejection. Use
held network gates for concurrency; no external services. Test streaming size
stop/close, redirects disabled, fixed URL/content-type, deadline, HTTP retry paths,
safe logs with synthetic token/PII sentinels. Test byte limits with multibyte input.

Exercise cohort initialization/intent stripping and immutability, response redaction,
PROTECTED_FIELDS and existing purge removal. Execute both accepted direct verification
and accepted server-notification hooks, rejected payload/failed update no-hook paths,
inbound/forwarded observation before throttle and measurement failure isolation.
Use existing production fixtures where appropriate rather than string membership.

Reducer tests: exact synthetic 2026 counts, all price/trial variants, signed versus
observed time, as_of attribution, wrong/negative/future dates, arbitrary/PII ID
values, bool IDs, unknown enums, contradictory fields, over-limit input, invalid
out-of-cohort records. Assert errors and CLI output do not echo input sentinels.

Swift tests: disabled no-token, duplicate starts, cancellation, account replacement
while each await is suspended, same-ID/token generation rotation, signout, retry
budget/spacing and strict HTTP/NSNumber parsing. Held continuations need bounded
expectations and cleanup so guard mutations fail rather than hang.

Return strict JSON with actual changed files, tests added and risks. No passed-test
claims. The parent will run local suites, mutation probes and independent review.
