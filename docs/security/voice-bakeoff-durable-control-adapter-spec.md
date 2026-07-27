# Voice Bakeoff Durable Control Adapter Specification

**Status:** source-only implementation specification. It is non-executable and
does not authorize Task 4.8, provider connectivity, credentials, workloads,
deployments, PSTN activity, production/staging access, or retention locking.

**Baseline:** `e137c78760a2b77a5213e9b4dfde36f3e207a10d`
(`feat: add transactional bakeoff control seam`).

This specification closes the design gap between the source-only transaction
seam and a future isolated Firestore adapter. It deliberately creates no
identity, role binding, client, credential, Firestore document, TTL rule, or
network request. The current `execution_status` remains `not_authorized`.

## Preconditions for implementation

No connected implementation, composition, IAM mutation, or cloud write may begin
until all of the following are bound to a new exact SHA and independently
reviewed:

1. a real isolated application/workload boundary that owns no production or
   staging configuration;
2. a complete source/configuration/dependency digest bundle;
3. the final document paths, retention/TTL behavior, and reconciliation
   procedures;
4. a least-privilege policy proposal with resource-condition semantics verified
   against the actual cloud IAM surface;
5. an explicit rollback plan for every identity and binding;
6. a sealed owner authorization that is still unexpired and unconsumed.

The source-only seam is not evidence that these prerequisites exist.

## Store and identity separation

The control and pre-auth stores are independent security domains. A future
adapter must use distinct projects, named Firestore Native databases, runtime
identities, deployment identities, and credential-delivery paths:

| Domain | Project/database | Proposed runtime identity | Permitted data |
| --- | --- | --- | --- |
| Execution control | `hk-voice-bakeoff-0724-iso` / `voice-bakeoff-control` | `voice-bakeoff-control-adapter` | trust-generation pins, nonce/approval consumption, binding epochs, reservations, revocations, aggregate receipt references |
| Pre-auth | `hk-voice-bakeoff-preauth-iso` / `voice-bakeoff-preauth` | `voice-bakeoff-preauth-adapter` | short-lived opaque token digests, activation records, grant index, acknowledgement/proof references, revocations |

The names in this table are proposals only. They do not create service accounts
or reserve names.

Both runtime identities must have no user-managed keys and no role/binding in
`kevin-491315`, staging, or any production-adjacent project. Neither identity
may impersonate the other. The execution-control identity must not read or write
the pre-auth database; the pre-auth identity must not read or write the control
database. A future deployment identity is separate from both runtime
identities and may not gain runtime datastore access merely by deploying code.

The owner may perform a narrowly documented bootstrap/revocation operation only
through a separately reviewed, keyless administrative path. That path must not
become a standing execution identity and must be removed or disabled after its
approved operation.

## Data model and payload boundary

All identifiers are canonical opaque references or domain-separated digests.
Records contain no credential, token value, provider payload, phone number,
audio, transcript, prompt, endpoint, raw session ID, callback code, or exception
text.

### Execution-control database

The implementation must map the transaction seam namespaces to deterministic
document paths under one dedicated root:

| Namespace | Deterministic record | Required invariant |
| --- | --- | --- |
| Trust pin | `trust_pins/current` | generation, snapshot digest, and CAS token advance monotonically; root-key fingerprint and persistence reference remain immutable in this CAS path |
| Consumed nonce | `consumed_nonces/{nonce_digest}` | create-once; never deleted before the envelope expiry plus residue audit |
| Consumed approval | `consumed_approvals/{approval_id_digest}` | create-once; points to the same reservation/binding as the nonce |
| Binding epoch | `binding_epochs/{binding_digest}/{epoch}` | each `(binding_digest, epoch)` pair points to one compatible reservation |
| Reservation | `reservations/{control_ref}` | state transition is pending -> active/revoked/expired only; binding, approval, nonce, and epoch are immutable after creation |
| Revocation receipt | `revocations/{control_ref}` | idempotent reason/time/terminal-state evidence only |

The reservation transaction must atomically create or validate every index above.
It must reject a missing, misrouted, or mismatched binding pointer and reject a
matching pending or active pointer without committing any partial record. For an
exact matching terminal pointer, it may expire that pointer if needed and rebind
it only inside the same complete reservation transaction. A root-key rotation is
a separate trust-anchor transition and gate; it is not a trust-pin CAS update.

### Pre-auth database

The pre-auth store remains a separate compensating-saga participant rather than
a distributed transaction participant:

| Record | Deterministic key | Required invariant |
| --- | --- | --- |
| Activation record | `activations/{preauth_ref}` | contains only token digest and the bound control/grant/binding digests |
| Grant index | `grants/{grant_id_digest}` | points to exactly one compatible activation |
| Acknowledgement reference | `acknowledgements/{preauth_ref}` | immutable digest-only acknowledgement/proof material |
| Revocation receipt | `revocations/{preauth_ref}` | idempotent terminal state and reason |

A pre-auth activation must be recoverable by grant digest without reissuing or
disclosing a token. Expiry, consume, revoke, and teardown must make the token
unusable even when a later cleanup operation is unavailable.

## Adapter boundary

The only future vendor-specific code belongs behind a Firestore
`TransactionPort` implementation. The existing
`TransactionalTrustGenerationPinStore` and
`TransactionalExecutionControlStore` remain policy-level consumers and may not
import Firestore, Google authentication, environment/settings, filesystem,
process, socket, or provider modules.

The future composition root may construct one adapter per store only after the
sealed gate is satisfied. It must inject:

- an already-attested dedicated database handle;
- an immutable `TransactionScope` whose opaque project/database references match
  the attestation;
- an allowlisted clock and bounded retry policy;
- payload-safe metrics/receipt writer interfaces.

It must not discover a database from an environment variable, default project,
caller input, metadata server, dynamic import, or arbitrary endpoint. It must
fail closed when the injected scope does not exactly match the attested database.

## Transaction, retry, and recovery contract

Firestore may retry transaction callbacks. Therefore callbacks may access no
external state except the supplied transaction view. They may perform
deterministic local validation and derivation using already-injected material,
but their result may be released only after `COMMITTED`. They may not mint a
token, resolve a credential, call a provider, emit a notification, write a log
payload, schedule work, or communicate with the other store.

The adapter must translate datastore outcomes as follows:

| Datastore outcome | `TransactionPort` result | Required caller behavior |
| --- | --- | --- |
| committed | `COMMITTED` | proceed only with the returned canonical record |
| conflict/retry exhaustion | `CONFLICT` | fail closed; no external retry outside bounded policy |
| permission, availability, deadline, or serialization failure | `UNAVAILABLE` | fail closed; do not assume no write occurred |
| ambiguous commit/result | `UNKNOWN` | fail closed and reconcile by deterministic identifiers |
| policy rejection | `ABORTED` | no record changes and no later side effect |

After an `UNKNOWN` outcome, reconciliation reads only the deterministic
nonce/approval/binding/reservation records. It may conclude only:

1. the exact expected reservation committed, in which case it returns that
   canonical record without re-consuming the nonce;
2. no compatible reservation exists, in which case it is failed and requires
   teardown; or
3. any mismatch, in which case it is a security failure and all associated
   records are revoked where possible.

No recovery branch may issue a second pre-auth token, repeat a provider request,
or broaden authority.

The two databases never participate in a claimed distributed transaction:

1. control transaction atomically reserves the one-use nonce and creates a
   pending control record;
2. pre-auth transaction creates or recovers the activation by signed grant;
3. control transaction validates the acknowledgement and activates the
   reservation;
4. pre-auth transaction confirms the control proof;
5. any failure, expiry, ambiguity, or stop trigger executes idempotent
   revocation in both stores and preserves only payload-safe receipts.

The current `ExecutionSecuritySaga` remains the required cross-store
compensation model. A real adapter must demonstrate that every compensation
operation is retry-safe and that retrying it cannot reactivate authority.

## Future IAM proposal and proof obligations

No IAM binding is created by this specification. At the approved implementation
boundary, the proposal must grant only the minimum Firestore transaction
permissions necessary for the exact named database and record roots. It must
be resource-conditioned or otherwise technically constrained to that database;
an unconditional project-wide `roles/datastore.*` binding is not acceptable.

Before an IAM change can be considered complete, the implementation packet must
contain all of the following:

1. identity inventory: exact service-account emails, creation time, project,
   purpose, and confirmation of zero user-managed keys;
2. effective IAM export for each isolated project/database and a justification
   for every granted permission;
3. negative proofs using each actual runtime identity that production and
   staging projects/databases are denied;
4. negative proofs that control identity cannot access pre-auth and pre-auth
   identity cannot access control;
5. a readback showing no default compute, Cloud Run, build, or service-agent
   identity gained an equivalent grant;
6. a removal command/procedure that removes every new binding before deleting an
   identity, plus a readback proving the removal.

Credentials must be keyless and delivered only from a future approved isolated
runtime/broker. Workload Identity Federation, service-account impersonation, or
an equivalent control is acceptable only if it is separately attested and has
the same production-denial proofs. User-managed JSON keys are prohibited.

## Required implementation tests

The implementation change must add focused tests before it receives any cloud
authority:

- AST isolation: policy modules do not import cloud/auth/config/network/process
  modules; the only Firestore import is the narrowly scoped adapter;
- adapter scope mismatch, default-project rejection, and database-name
  substitution rejection;
- atomic create-once nonce/approval/binding reservation under concurrency;
- all-or-nothing rollback for late transaction mutation failures;
- trust-pin bootstrap, monotonic rotation, stale generation, and non-advancing
  CAS rejection;
- conflict, unavailable, deadline, and unknown-result reconciliation;
- misrouted binding pointer, incompatible deterministic record, and attempted
  replay rejection;
- cross-store activation/confirmation recovery and idempotent compensation;
- TTL/expiry/revocation/teardown behavior with no authority resurrection;
- runtime composition rejection before a sealed authorization, before credential
  resolution, and before any provider/PSTN construction.

An emulator or isolated integration test may demonstrate Firestore behavior only
after the IAM proposal is approved. It remains nonproduction and cannot contact
a provider, PSTN, production, or staging resource.

## Rollback and stop behavior

An IAM or adapter rollout is incomplete without a tested rollback. Stop triggers
must first deny new admissions, revoke active control and pre-auth records, and
drain the isolated workload before any cleanup. The rollback runbook must then:

1. read back zero active execution records and zero unexpired usable pre-auth
   records;
2. remove new IAM bindings and verify effective-policy absence;
3. disable or delete only the newly created adapter identities after confirming
   no binding or workload still references them;
4. retain only the approved aggregate/revocation receipts under the separately
   approved retention policy;
5. prove no provider, PSTN, production, or staging action occurred.

Retention locking remains out of scope. Any retention change, immutable custody
lock, identity creation, IAM mutation, Firestore write beyond the existing
metadata bootstrap, credential broker, workload, or provider contact requires a
new explicit gate decision.
