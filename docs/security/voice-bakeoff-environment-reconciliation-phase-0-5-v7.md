# Voice bakeoff environment reconciliation Phase 0.5 v7

Status: source-only exact-review candidate. V7 qualifies only one fail-closed
operation that may return a validated, unsigned phase-1 owner-signing payload.
It is not a signature, authorization record, connected-read package,
credential, runtime, mutation plan, or Task 4.8 admission.

Source baseline:
`2ed8ea7d1d7f338e84ddf08d5a50a714835e1533`.

## Recommendation

Create no Google Cloud project. Retain these exact project IDs frozen:

- `kevin-491315`
- `kevin-staging-491315`
- `hk-voice-bakeoff-0724-iso`
- `hk-voice-bakeoff-preauth-iso`

The screenshots show different account visibility, not duplicate project
identity. Identity requires exact project ID, immutable project number, and
complete ancestry. The mixed staging control/data topology remains separate
debt and is not changed here.

## V7 closure

V6 correctly contained scope to an unsigned, bodyless, four-request phase-1
payload, but its example builder returned a candidate before a separate
validator was called. V7 replaces that composition with one public operation:
`materialize_validated_phase_one_payload`.

The operation returns either:

- one complete payload that passed every check; or
- an error with no payload, no partial persistence, and no fallback.

Before returning, it must:

1. require the exact private-input and trusted-context key sets;
2. reject booleans and require exact integer timestamp types;
3. validate lowercase SHA-256 digests and the UUIDv4 session;
4. require `issued_at_ms <= validation_time_ms < expires_at_ms`;
5. limit lifetime to 900,000 ms (15 minutes);
6. construct the candidate only in private ephemeral memory;
7. validate the closed V7 schema;
8. validate the exact ordered four-project/bodyless-request relations and all
   request/set digests; and
9. return only if the complete error set is empty.

Direct negative tests cover invalid UUIDs, malformed digests, Boolean and
non-integer timestamps, future issue times, expired bounds, non-increasing
bounds, and excessive lifetime. They assert the public operation raises rather
than returns a payload.

## Exact unsigned payload

The request sequence remains one bodyless Cloud Resource Manager
`projects.get` for each frozen project ID in the listed order. Each request
binds its position-derived ID, exact path, target and quota project, identity
configuration digest, canonical null body, response mask, and request digest.
The payload contains no owner-signature or authorization-record field.

Materialization requires only a private UUID, lowercase digests for the owner
key, two identity configurations and raw custody, a nonce seed, and time
bounds, plus trusted validation time. Credentials, tokens, service-account
keys, provider secrets, cloud responses, project numbers, ancestry, and private
allowlists are forbidden.

The materialized payload must remain outside Git and chat.

## Still sealed

V7 does not fix or qualify the V5 runtime findings. Atomic current-phase
once-only admission remains unimplemented and blocks every dispatch. Dynamic
request-body resolution, the owner-bound source seed, encoding, receipt
freshness, and phases 2–6 remain unqualified and sealed.

Even a later owner signature would not itself authorize dispatch. No
credential broker, network client, response handler, receipt signer, or
current-phase ledger exists in this package.

## What can happen next

Do not authorize V7 itself.

After exact-hash staff, security, and operator approval with no unresolved P1,
the validated materializer may be called once with separately supplied private
inputs. That source-only result would still have:

```text
owner_signature_status       not_recorded
owner_authorization_status   not_recorded
connected_inventory_status   not_authorized
mutation_status              not_authorized
execution_status             not_authorized
task_4_8_status              sealed
```

Until a later exact payload and separately custodied owner authorization record
are reviewed, create no project; make no connected read, IAM/credential,
workload, database, billing, retention, provider/PSTN, staging, production, or
Task 4.8 action.
