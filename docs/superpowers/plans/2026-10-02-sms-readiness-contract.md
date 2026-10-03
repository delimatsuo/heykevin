# SMS readiness audit implementation contract

Bound base: `fa353fc996f0a0003ec3179d3a17077518c59c07`.
This is an operational read-only tool, not a registration or consent writer.

## Builder envelope

Only add `scripts/sms_readiness_audit.py` and
`tests/unit/test_sms_readiness_audit.py`. No Git mutations, provider access,
credentials, dotenv reads, test execution or unrelated changes. The master owns
independent validation. Use the existing Python standard library plus the
repository's phone-normalization dependency when necessary; do not add packages.

## Input, readers and bindings

Provide a pure `summarize_sms_readiness(snapshot, *, as_of)` and an injectable
`collect_snapshot(firestore_client, twilio_client, *, expected_project,
expected_account_sid, messaging_service_sid, observed_at, max_records=5000)`.
The collector uses passed authenticated clients only. It must never instantiate
app settings, read secrets or construct a client from ambient credentials.

The offline CLI consumes JSON from a named file or stdin and requires the exact
expected project/account/service plus an explicit UTC `--as-of`. It prints only
the aggregate report. No `--apply`, network client setup, uploads or writes.
Snapshot source/provenance remains caller-supplied, not independently certified
by the offline CLI.

The snapshot contains schema version, observation timestamp, project/account/
service binding metadata, per-source completeness and lists of owned provider
numbers, service sender numbers and projected contractors. Validate expected
bindings before interpreting missing membership. The collector checks client
project/account and fetched service SID/account, checks every provider row's
binding, and uses bounded pagination/stream reads with a `max_records + 1`
sentinel. A read error or cap overflow marks that source incomplete. Never echo
an exception message or discard a partial error and claim complete success.

Firestore must read only collection `contractors`, with this exact projection:
`twilio_number`, `active`, `deletion_requested_at`, `deactivated_at`,
`deleted_app_detected_at`, `subscription_status`, `subscription_tier`,
`subscription_expires`, `trial_start`, `last_inbound_call_at`,
`forwarding_last_seen_at`, `owner_sms_enabled`, `owner_sms_opted_out`.
Use a finite limit and 15-second read deadline; no calls, estimates, names,
email, owner phone, transcript, tokens or full-document reads.

Twilio reads only the bound service and its phone-number pool plus owned incoming
number inventory. Do not read messages, calls or regulatory identity documents.
Retain internally only number SID, number, account/service binding and explicit
capabilities needed for the join. Use SDK read operations only and a cap for
both lists. Return lists with per-source complete/error state; omit unsafe data.

## Reconciliation and output

Use provider number SID for inventory-to-pool membership and canonical valid
E.164 numbers for tenant-to-inventory assignment. Do not infer US from a +1
prefix or silently repair malformed values. Detect duplicate SIDs, duplicate
owned numbers, duplicate tenant assignments, malformed assignments, assignments
absent from owned inventory, unassigned owned inventory, and pool entries absent
from inventory. No ambiguous or incomplete join is a review candidate.

Treat a contractor's exact empty-string number as the existing unassigned
sentinel, like missing/null; whitespace-only and other malformed strings remain
malformed. Any duplicate tenant assignment (including an unowned number),
duplicate provider SID/number, or pool-to-inventory conflict makes the entire
snapshot incomplete and yields zero review candidates, even when a separate
clean missing-membership assignment exists. Any malformed contractor number also
blocks all candidates and overall completeness: a rejected value could conceal
a duplicate of a valid assignment. Preserve diagnostic aggregate counts; do not
normalize malformed values or treat legitimate unassigned sentinels as malformed.

Provider number SIDs must be nonempty strings without surrounding whitespace in
both collection and reduction. Never strip an authoritative join key. A known
source `binding_mismatch` must invalidate the report's bindings, including when
a later read failure discards that source's rows. Duplicate unowned tenant
assignments count as both unowned and ambiguous; their membership cohort is
ambiguous, while a unique unowned assignment remains unowned.

An absent join proves unowned or unassigned only when sources are complete and
bindings valid. Suppress those confirmed-absence totals on uncertain sources,
and classify uncertain ownership as unknown. Malformed contractor numbers also
prevent a confirmed unassigned-inventory count. Observed duplicate assignments
remain ambiguous even when ownership is unknown. These diagnostic totals are
not estimates of missing data. Sanitize argument-parsing errors as well as
snapshot/summary failures; never echo unexpected command-line values.

The report contains only schema/version/time, completeness, fixed limitations,
allowlisted aggregate counters and cohort rows. Never print phones, contractor
IDs, provider number IDs, raw unexpected keys/values or exception text. Unknown
enums become a fixed `unknown` bucket, not arbitrary output labels. Reject
unexpected top-level input fields without echoing them. Validate every row,
including rows outside an interesting cohort; cap all lists at 5000.

Include totals for owned inventory, pool membership present/missing/unknown,
assigned/unassigned numbers, malformed/ambiguous/unowned assignments and
missing-owned-unique assignments requiring review. `review_candidates` means
only a complete unambiguous missing-membership join; it is never campaign
eligibility, permission to repair, entitlement, consent or delivery proof.
Incomplete or stale snapshots (more than 15 minutes before `as_of`, or future)
must yield zero review candidates and explicit uncertainty.

Group assigned-account rows by fixed dimensions: membership; active
true/false/missing/malformed; deletion/app-deletion markers; stored subscription
status/tier; expiry and trial validity; inbound and confirmed-forwarding age;
and owner SMS preference state. Keep flags, entitlement and activity separate.
Missing timestamps mean unknown, not inactive. Expired subscriptions and deleted
apps stay visible because voicemail texts may remain relevant. A past stored
expiry does not establish nonpayment. Missing/malformed preferences stay
explicitly unknown rather than becoming new opt-in evidence.

## Independent acceptance tests

- Synthetic 29 owned/19 registered inventory yields 10 missing memberships.
- Active, subscription, deletion, activity and preference intersections remain
  independent; unknowns stay unknown.
- Binding mismatch fails closed; source errors, truncation, stale/future
  snapshots and every ambiguous mapping produce no review candidates.
- Pagination caps cannot convert partial inventory into missing registration.
- Fake clients assert the exact Firestore projection and read-only SDK calls;
  mutation/send/read-message methods raise if called.
- Privacy sentinels in records, IDs, extra fields and raised exception messages
  never appear in stdout, stderr, logs or reports.
- Offline CLI uses a synthetic fixture, produces parseable aggregates, sanitizes
  errors and rejects malformed input without side effects.

The master will mutate binding, completeness, duplicate-assignment and projection
guards and require corresponding tests to fail, then restore the candidate.
