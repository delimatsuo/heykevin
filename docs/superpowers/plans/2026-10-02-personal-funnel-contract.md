# Personal activation measurement implementation contract

Base HEAD `fa353fc996f0a0003ec3179d3a17077518c59c07`, tree
`aea0082ef9ed569bfa395d34c429a06be09fd889`. This extends existing measurement;
it does not change calling, admission, payment verification or trial policy.

## Builder envelope

Allowed edits: `app/db/contractors.py`, `app/services/acquisition.py`,
`app/services/post_call_handoff.py`, `scripts/summarize_acquisition_funnel.py`,
`tests/unit/test_acquisition_measurement.py`,
`tests/unit/test_acquisition_wiring.py`, `tests/unit/test_post_call_handoff.py`,
and new focused `tests/unit/test_screening_measurement.py`.
No Git mutations, tests, provider access, credentials, dotenv reads or unrelated
edits. The master independently tests and reviews the actual diff.

## Immutable country snapshot

On eligible new-account measurement-map creation, set
`account_country_at_signup` to the already normalized `effective_country`.
Never overwrite it on later profile edits and never backfill legacy accounts
from mutable profile data. The existing protected map and redaction/deletion
rules continue to apply. No new map when collection is disabled.

The reducer accepts the optional exact country enum
`US, CA, BR, GB, DE, FR, IT, ES, PT`; missing/null means legacy `unknown`.
Reject other types/values with generic errors. This country is account setup
country, not residence, number location, storefront or ad targeting geography.

## Conservative conversation observation

Add `first_screening_conversation_observed_at`, a first-write-only timestamp
inside the protected map. It means only that a qualifying durable transcript
contains a Caller line followed by a Kevin line; generated text does not prove
the caller heard intelligible audio, a useful answer, summary quality or SMS
delivery. Do not name it successful screening. Some fallback/takeover/legacy
paths may be missed; missing evidence is not proof of failure.

Hook the shared `run_post_call_handoff` after `finish_handoff` returns true.
Require `call_record` in `result.completed_effects` and not in
`result.failed_effects`; partial SMS/push outcomes may still qualify. Use the
already loaded durable `call_record`, never caller-supplied inline transcript.
Retain existing handoff/call/inline tenant consistency guards. Require exact
nonempty string tenant identity matching that durable record. A scheduler error
must not change the successful handoff result.

The synchronous, default-off scheduler qualifies only:

- Exact stored `call_status == "completed"` and `route_taken == "ai_screening"`.
- Durable decrypted transcript is a nonempty string, at most 64 KiB UTF-8.
  Parse exact case-sensitive line prefixes `Caller:` and `Kevin:`; each must
  have non-whitespace text, with a Kevin line after a Caller line. A greeting
  followed only by a Caller line does not qualify. Embedded speaker-looking
  newlines are a limitation of existing unstructured transcripts.
- `timestamp` and `ended_at` are finite numeric non-booleans within existing
  timestamp bounds and `timestamp <= ended_at <= observed_at`.
- `observed_at` is finite numeric non-boolean, within existing timestamp
  bounds, and not in the future relative to the scheduler's current clock.

After qualification, enqueue only contractor ID, call start and observation
time. Do not retain transcript, call SID or the whole record in background
tasks. Bound pending tasks to 32, handle absent event loop, consume exceptions,
and discard completed/cancelled tasks like the payment scheduler. No extra DB
read or task when disabled. Do not refactor the payment scheduler.

The async transactional writer checks collection enabled before obtaining a DB
client and again inside the transaction; valid nonempty contractor ID; valid
times; active account; valid eligible existing map; call start >= map creation;
and absent prior milestone. Persist only the observation timestamp. It must
not create a measurement map, overwrite an observation, change entitlement or
log personal data. Keep legacy map schema 1 compatible.

## Aggregate reporting

Allow and validate the new optional milestone (finite timestamp >= creation),
count observations at/before as-of, and group by account country and Apple
`conversion_type` in addition to existing intent/source/campaign/ad-group and
optional keyword. Legacy country and missing conversion type are `unknown`.
Only use conversion type when attribution was already observed by as-of;
future attribution remains source/conversion `unknown`.

Keep existing counters for compatibility. Add
`screening_conversation_observed` to groups and totals. All milestones are
independent counts of supplied records, not a sequential funnel. Do not add
rates, reorder server trial timing, infer first-ever payment, current paying
subscribers, renewals, net revenue or profitability.

Preserve `complete: true` compatibility but add explicit
`input_validation_complete: true`, `source_population_complete: null`,
`input_uniqueness_verified: false`, and `input_record_count` to the report.
Explain that complete only means all supplied records were processed; the
offline reducer cannot certify export completeness or unique accounts, and
deletions/missing/old-build records affect counts. Positive-price observations
may be historical restored purchases. Document observation time as processing
time. Keep strict validation of ALL records before filtering and all existing
record/window bounds and sanitized errors. No identifiers/phones/content added.

## Required independent probes

Cover country snapshot creation, legacy unknowns, malformed country values,
country/conversion/keyword grouping and future as-of attribution; disabled
zero-DB behavior; exact route/status; greeting-only vs Caller-then-Kevin;
missing/malformed/overlong durable transcript vs convincing inline content;
all timestamp order/type bounds; prior-cohort call rejection; immutable first
write; inactive/malformed map; persisted successful call-record effect vs
finish/save failure; partial delivery allowed; tenant mismatch; scheduler
capacity/exception/cancellation cleanup; held writer cannot delay handoff.

The master will run focused regressions and mutation probes against route,
tenant, pre-cohort and report as-of guards. The builder must not self-grade.
