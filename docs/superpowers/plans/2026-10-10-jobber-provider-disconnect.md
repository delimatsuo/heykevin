# Jobber provider disconnect lifecycle — pinned implementation contract

Source base: `ab00cdd07f53df750d93e2eddfa79f94e0da2ace`, tree
`33121861856860f7417e7ee8e4eade6d29bb18b5`.

This implements the missing J4 source contract. It does not activate ingestion,
deploy a service, subscribe a webhook, grant access, or establish Marketplace
acceptance. Configuration changes, real provider experiments and deployment
remain outside this local implementation envelope.

Success criterion from the active Marketplace plan:

> A valid provider disconnect prevents stale refresh/connect/quarantine results
> from restoring revoked credentials.

## Frozen policy

Every authenticated APP_DISCONNECT delivery durably suspends ordinary access
for its exact app/account before acknowledgement and before asynchronous
contractor fanout. A bounded durable worker invalidates the affected binding's
old claims by advancing its lifecycle, disables capture, and requires fresh
authenticated reauthorization. It preserves credential material in its existing
storage form until an authenticated complete replacement or explicit local
removal. There is no automatic provider query, refresh, appDisconnect, credential
deletion based on occurredAt, or attempt to guess which grant was revoked.

A delayed or repeated payload can pause a newer connection. The UI must say
paused/reconnect, never falsely assert that its newer grant was revoked or that
credentials were deleted. Retained material is unavailable to ordinary use.
Jobber acceptance of this conservative delayed-event/deletion policy remains
an external qualification requirement, not a reason to leave safety unimplemented.

The existing exact raw-body SHA256 fingerprint is delivery-byte identity only.
It is not a documented provider event/grant ID. Each independently received
authenticated delivery gets a random server admission ID. Transaction retry and
ambiguous commit recovery reuse that ID and cannot allocate a second sequence.
Later identical bytes receive a new admission sequence and still fence bindings;
never permanently suppress them using the fingerprint. Duplicate effects on a
contractor are idempotent for a given account barrier/lifecycle.

## Architecture and data

Use small cohesive modules for receipt/barrier persistence, bounded recovery,
and the HTTP receiver. Reuse the current token encryption, exact type checks,
transaction/readback patterns, PKCE and account lookup. Do not add a second
token store, a generic workflow framework, or another unused install adapter.

* Fixed control document: `jobber_disconnect_control/current`. Strict schema
  version 1, exact expected app ID, bounded monotonic global receipt counter,
  explicit active enforcement marker and server timestamp. Initialization is an
  explicit later deployment step; the receiver must not initialize it implicitly.
* Account barrier: `jobber_account_barriers/{key}`. Domain-separated hash of
  unambiguous app/account encoding; raw opaque IDs are never path components.
  Strict app ID, account ID, schema, counter and server timestamp. Missing barrier
  is zero only under a valid active control record; malformed records fail closed.
  Unknown accounts retain barriers indefinitely; no TTL/reset policy is invented.
* Receipt: `jobber_disconnect_receipts/{admission_id}`. Strict validated metadata,
  raw-byte fingerprint, app/account, occurredAt, received time and allocated
  counter only. No raw body, signatures, codes, tokens, ciphertext or response
  dumps. Receipt, counter, barrier and durable recovery work commit atomically.
* Durable work: bounded per-account work record or receipt work queue, with strict
  leases, server-time expiry, cursor, next-attempt time, capped attempts and
  needs-review retention. A newer barrier supersedes older work safely. Never
  lose newer work when an old lease finishes. No provider HTTP in recovery.
* Protected contractor binding fields: `jobber_app_id`,
  `jobber_accepted_disconnect_barrier`, `jobber_disconnect_state`,
  `jobber_disconnect_processed_barrier`, and the owning
  `jobber_operation_intent_context` / `jobber_reauthorization_attempt_context`.
  Strictly validate any pending/recovery metadata needed for truthful status.
  The existing `jobber_account_id`,
  generation, lifecycle epoch and raw-credential fingerprint remain authoritative.
  Protect all new fields against contractor PATCH.

No account-to-contractor uniqueness assumption is allowed. All matching bindings
are immediately fenced by the shared barrier, then discovered in bounded pages.
Each binding requires its own authorized reconnect. Recheck exact membership in
every mutation. A late-created binding must pass connect-time barrier checks
regardless of whether a fanout cursor has passed its document ID.

## Preactivation, enforcement and rollback

Add default-off settings for ingestion and recovery, an optional expected app ID,
and an explicit default-off enforcement deployment mode. Existing behavior is
compatible only in the documented preactivation mode with no control document
and no enforcement metadata. Every Jobber credential admission/mutation still
transactionally reads the fixed control document, including in compatibility
mode, so initialization races conflict/retry into strict enforcement.

Configured enforcement requires an existing valid control record. Missing or
malformed control fails closed regardless of receiver/worker switches. A present
active control record enforces barriers even when all switches are false or app
configuration has been removed; its exact app ID remains authoritative. Binding
metadata without valid control fails closed. Never initialize/reset control or
implicitly give legacy bindings barrier zero from ordinary operations.

New claims capture an explicit preactivation or enforced admission context.
Ordinary contexts bind the app, account and accepted barrier; OAuth contexts
bind the app, source account and receipt counter at authorization start.
Persist each context with its owning Jobber claim and remove it with that claim;
preactivation results cannot commit after activation. Existing predeployment
Jobber claims are usable only in preactivation mode. Existing Google exact
schemas and behavior remain unchanged.

Activation later requires all serving revisions to understand barriers, explicit
valid control initialization, legacy fail-closed/reconnect acceptance, qualified
storage latency/indexes, and separately authorized flags/subscription. An old
writer or old deployment cannot safely coexist. After activation, never delete
control/barriers or return to compatibility mode; rollback only to barrier-aware
code or suspend all Jobber operations. Disabling ingestion/recovery leaves
already recorded barriers effective.

## Receiver and recovery

Mount POST `/webhooks/jobber` with HMAC authentication only. Disabled or
misconfigured receiver returns unavailable without DB/provider effects. Require
one unambiguous signature header. Stream raw bytes with the existing 65536-byte
cumulative cap before buffering; reject empty, oversized, malformed, tampered,
wrong-app or wrong-topic payloads with generic responses before persistence.
Use the existing authenticate_jobber_disconnect_envelope parser.

Generate admission randomness outside retryable transactions. Atomically read
all needed documents before any write. Allocate a checked counter once, store
receipt/barrier/work, and acknowledge only a verified durable acceptance. On
ambiguous commit, exact admission readback may confirm acceptance; a later
barrier may be greater but never smaller. Persistence failure/uncertainty is
retryable non-2xx. No background-only receipt, provider HTTP or fanout before ack.
Use bounded datastore timeouts; timing out an executor is not cancellation of
the transaction and must retain same-ID ambiguity semantics. Provider's one-
second deadline remains a measured deployment qualification, not a local claim.

Recovery is a tracked cancellation-aware startup/shutdown task, disabled by
default, with injectable repository/clock for offline tests. Bounded batches and
pages, deterministic pagination, exclusive transactional leases, lease-owner
finalization, capped attempts/backoff, expired-lease restart and poison-record
retention prevent unbounded work/starvation. No new external infrastructure.

For each exact matching stale binding, transactionally advance generation and
epoch (strict bounds), invalidate all ordinary/refresh/reauthorization claims,
retain existing credential material and encrypted-envelope floor, set capture
false, `jobber_disconnect_state='reauthorization_required'` and the matching
`jobber_disconnect_processed_barrier`, and record a validated payload-safe
audit atomically. Do not create a provider revocation outbox. Repeated page or
lease execution cannot advance the same binding twice for the same barrier.
Provider suspension uses these paired fields rather than fabricating an
ordinary refresh-unknown-outcome claim.
Do not alter Google audit validators by silently weakening their closed schemas.
Credential-bearing logs are forbidden; safe counts/status enums are sufficient.

## Integration enforcement

The fixed control and relevant barrier are authoritative transaction reads at
every Jobber ordinary admission and late-write boundary: durable snapshot load
(including normalization/floor promotion), preflight recovery, intent acquire,
reserved-to-started, refresh persistence, connect/reconnect, quarantine, capture
enable (including true-to-true no-op), and normal/ambiguous readbacks. Bind exact
app/account/barrier and generation/epoch/fingerprint to claims/results. No
cached contractor, raw token, pure classifier or retry shortcut authorizes HTTP.
The shared Jobber service remains the mandatory dispatch fence for Clients,
Requests, notes and other helpers. Work already sent cannot be retracted, but
its late result cannot restore local credentials or capture.

Pure token helpers must not acquire a DB dependency. Their local gates are
advisory; durable dispatch and mutations enforce the account barrier. Clearing a
claim must never clear account-level blocking. No successful callback may reuse
the old refresh token or re-enable capture after suspension.

Fresh Jobber OAuth state has a deliberately versioned strict schema with explicit
enforcement context and `receipt_counter_at_start`; all predeployment Jobber
states restart. Google's seven-field state schema remains unchanged. State
creation reads global counter transactionally and binds current source identity,
generation/epoch/fingerprint and PKCE. A pending/blocked binding may preempt old
started/quarantined claims only through an explicit lifecycle advance, then
create this authenticated new flow; ordinary credential use remains blocked.

State consumption and final bind commit reject a relevant source account barrier
greater than the start counter, even if destination differs. After token exchange
and fresh account.id lookup, final CAS also requires destination barrier <= start
counter, captures current accepted barrier, and transactionally reads that
barrier. Unknown destination events before state creation allow a newly verified
binding; an event after state creation requires a new flow. Unrelated-account
events do not invalidate the flow by requiring global counter equality. Same-ID
concurrent receipt conflicts with final bind transaction. No timestamp/grant
chronology inference or cleanup appDisconnect of an uncommitted candidate.
Active claimless Jobber connection commits are prohibited; only the verified
authenticated state path can supply the internal callback proof. Successful
replacement overwrites the complete old pair, advances lifecycle, clears claims,
stores binding acceptance and verifies durable readback. Other bindings stay
blocked. OAuth provider proof cannot be client supplied.

Authenticated explicit local removal must work for a barrier-blocked binding
even with started or quarantined work, by advancing lifecycle and applying the
existing full credential/claim tombstone plus capture false and durable audit.
The local-only tombstone also removes the account, app, accepted barrier and
suspension fields. Store and verify its immutable local-removal audit separately
from the provider revocation audit/outbox pair; repeated removal reuses that audit
and lifecycle. This path makes no appDisconnect request. Preserve encryption
floor and monotonic counters. Report local deletion separately from provider
revocation. Keep normal connected app-side disconnect behavior unchanged outside
this path, including the historical account identifier retained for diagnostics.

Status reads have no hidden provider calls. Add `connection_status` values
connected, disconnected, disconnect_pending, reauthorization_required. Suspended
states always return connected=false, capture=false, no misleading connected_at.
Admin summary must also use authoritative status. Native decoding validates
enum/boolean consistency, supports old-server responses, and presents pending
or reconnect-needed copy with existing auth/operation stale-completion fences.
Native local removal/reconnect controls must reach the authorized paths.

## Offline verification anchors

Tests use fictional data, injected stores/HTTP and no external services:

1. Authentication/body/header/app/schema rejects have no persistence; disabled
   receiver no DB/provider access; durable failure is not acknowledged.
2. Receipt/counter/barrier/work atomicity, same-admission retry/readback, repeated
   payload after replacement, overflow/malformed control/barrier fail closed.
3. Immediate status/use/refresh/capture fencing before fanout; multiple bindings
   and unknown accounts; unrelated account unaffected.
4. Every stale refresh/connect/quarantine/normalization/capture write is rejected
   before fanout and after worker lifecycle advancement/fresh replacement. First
   activation racing preactivation claims cannot commit obsolete results.
5. OAuth relevant source/destination barrier ordering and account switch; state
   before event fails, after event succeeds, unrelated events do not invalidate;
   complete replacement never reuses refresh token/restores capture/other binding.
6. Started/quarantined fanout and explicit local removal; retained encrypted
   credentials stay unavailable; no refresh/appDisconnect and atomic safe audits.
7. Restart at admission/page/lease/finalization; expired/stale lease, bounded work,
   poison data, duplicate page, new receipt during recovery preserve newer work.
8. Sticky enforcement with disabled ingestion/recovery and missing/malformed
   configured control; legacy restart instead of implicit acceptance; no PATCH
   forgery; Google behavior and existing encrypted-floor invariants unchanged.
9. Native truthful pending/reconnect/local-deletion states, strict decoding,
   old-server compatibility and existing auth/operation staleness tests.

Master independently inspects actual diff, runs focused and required checks,
mutation-checks guards, and obtains fresh-context review. Builder reports are
not test evidence. No hosted CI, push or PR until exact-head $0 cost preflight;
no deployment or live provider qualification in this source envelope.
