# Returning customer continuity rollout

This feature is part of the normal receptionist service. Public-demo ingress may
reuse these domain and tool contracts only through sandbox provider adapters; it
must not own a separate memory model or receive production provider credentials.

## Runtime gate

All activation controls default closed when absent and are server-owned:

- `customer_memory_capture_enabled` permits caller-confirmed identity writes.
- `customer_memory_personalization_enabled` permits memory reads for prompts and
  spoken-name greetings. Trusted owner contacts remain a separate existing source.
- `service_request_mutations_enabled` permits both new provider-backed appointment
  creation and returning-customer changes for one contractor.
- `SERVICE_REQUEST_RECOVERY_ENABLED` is the environment-wide readiness gate. Keep
  it false until the Firestore requirements and recovery runtime below are verified.

Capture, personalization, and mutations are deliberately independent. Never enable
capture before retention, disclosure, and deletion requirements are approved. Once
provider intents have been admitted, do not turn off the recovery readiness gate as
a kill switch; recovery of already-durable intents must continue.

Identity memory and appointment continuity are also separate runtime projections.
An appointment is found from the tenant plus hashed normalized caller number; it does
not require a fabricated identity-memory document. Disabling name personalization
therefore does not reinterpret or erase an already-authorized provider request.

## Required Firestore configuration

Set `PROJECT_ID` explicitly for each isolated environment. Do not infer it from a
developer's active gcloud configuration.

```bash
gcloud firestore indexes composite create \
  --project="$PROJECT_ID" \
  --database="(default)" \
  --collection-group=service_requests \
  --query-scope=collection \
  --field-config=field-path=customer_key,order=ascending \
  --field-config=field-path=status,order=ascending \
  --field-config=field-path=updated_at,order=descending

gcloud firestore indexes composite create \
  --project="$PROJECT_ID" \
  --database="(default)" \
  --collection-group=service_requests \
  --query-scope=collection-group \
  --field-config=field-path=provider_recovery_state,order=ascending \
  --field-config=field-path=next_attempt_at,order=ascending

for collection_group in customer_memory service_requests command_receipts; do
  gcloud firestore fields ttls update expires_at \
    --project="$PROJECT_ID" \
    --database="(default)" \
    --collection-group="$collection_group" \
    --enable-ttl
done
```

Wait for the composite index to report `READY` and each TTL policy to report an
enabled state before turning on mutations. Verify with:

```bash
gcloud firestore indexes composite list \
  --project="$PROJECT_ID" \
  --database="(default)" \
  --filter='COLLECTION_GROUP:service_requests'

gcloud firestore fields ttls list \
  --project="$PROJECT_ID" \
  --database="(default)"
```

## Qualification gates

- Use an isolated staging Firestore project and a dedicated Google Calendar.
- Book a real staging appointment, then call back from the same number and verify
  the heard greeting exactly matches the greeting seeded into model history.
- Verify cancel, reschedule, and add-service each change the provider once and
  increment the canonical service-request revision once.
- Interrupt or time out a provider mutation after preparation, retry it, and verify
  Kevin never claims success before both provider confirmation and canonical
  finalization.
- Fault-inject a restart after durable preparation and after Google success. Verify
  the bounded recovery worker leases the same logical operation, uses the same
  provider resource ID, and CAS-finalizes exactly once.
- Run two recovery workers against the same due record and verify only one obtains
  the active lease. Let a lease expire and verify another worker can safely replay
  the desired state.
- Force eight bounded provider failures and verify the proposal becomes
  `needs_review`; it must not be deleted or silently treated as abandoned.
- Verify a different tenant and a different caller number cannot read or mutate the
  request.
- Verify expired memory and conflicted names receive the generic greeting.
- Verify the complete capture/personalization/mutation flag matrix: absent and false
  flags cause no memory read/write, no memory-derived name, and no new provider intent.

## Provider recovery

The worker scans only a bounded collection-group batch where
`provider_recovery_state == pending` and `next_attempt_at` is due. A Firestore
transaction claims each proposal with an owner, opaque lease ID, expiry, and
attempt count. Provider replay always uses the persisted logical operation ID;
finalization is a second transaction that must still own that exact lease.

Failures and uncertain cancellation release the lease with exponential backoff.
After eight attempts, the durable proposal is retained as `needs_review`; recovery
never auto-deletes or auto-abandons it. Recovery does not re-check the contractor's
current mutation flag because the persisted intent may represent a provider write
that already succeeded. The protected flag still gates creation of new mutation
intents at the call-tool boundary.

Pending and `needs_review` service-request documents intentionally omit the
top-level `expires_at` field, so the service-request TTL policy cannot erase
uncertain provider state. Atomic finalization restores the canonical 90-day
retention timestamp. Operators must explicitly resolve reviewed proposals; they
are not retention-swept automatically.

## Privacy boundary

Customer memory is stored only under
`contractors/{contractor_id}/customer_memory/{sha256(normalized_e164)}`. Durable
service requests live under the same contractor. Full customer-memory cards and
service details must not be copied into RTDB active-call state.

Top-level legacy `contacts` and `caller_contacts` records are quarantined and are
never read by tenant runtime paths. Tenant `caller_contacts` records without an
exact provenance schema, `tenant_post_call` source, and matching contractor ID are
also ignored because an older migration could not prove which tenant supplied their
contents. A fresh tenant-bound post-call write replaces the quarantined record and
stamps that provenance. `scripts/migrate_caller_contacts.py` is inventory-only and
must never copy phone-matched legacy data into tenant subcollections.

The existing account-delete endpoint deactivates the contractor but does not yet
recursively erase all historical contractor data. Do not claim complete account
erasure for this feature until that broader product deletion path is implemented
and tested. Do not enable capture in production until that purge path and the user
disclosure are approved. TTL is retention enforcement, not an immediate-delete
substitute.
