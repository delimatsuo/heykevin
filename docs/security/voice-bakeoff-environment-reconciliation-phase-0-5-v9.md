# Voice bakeoff environment reconciliation Phase 0.5 v9

Status: source-only exact-review candidate. Materialization is blocked.

V9 replaces the V8 operator path; it does not change or authorize V8. V8 remains
non-authorizing source evidence because its executable predecessor imports occur
before its in-function integrity check and its materializer is repeatable.

V9 may eventually perform one offline attempt to create one validated, unsigned
phase-1 signing payload. It is not an owner signature, owner authorization,
connected-read authorization, credential, mutation plan, provider/PSTN probe,
staging or production action, or Task 4.8 admission.

Source baseline:
`2ed8ea7d1d7f338e84ddf08d5a50a714835e1533`.

That SHA names the tracked runtime baseline only. The untracked V9 review bundle
has six separately exact-hashed artifacts: this guide, the V9 JSON contract,
schema, predecessor manifest, standalone runner, and tests. Any byte change
invalidates every review.

## Product evidence: none

V9 proves nothing about real calls or callers, native-audio quality, latency,
turn-taking, interruption, multilingual behavior, accessibility, user testing,
user approval, owner approval, provider capability, or Task 4.8 eligibility.
It is offline authorization/custody preparation only and cannot substitute for
native-audio qualification.

## Why V9 is a standalone runner

The V9 runner imports only Python standard-library modules. It imports and
executes no V1-V8 module. Before private input is released, it:

1. validates a separately custodied materialization-authorization record;
2. verifies the externally reviewed V9 sextet named by that record;
3. verifies the exact V1-V8 predecessor manifest file and all 32 artifacts;
4. rejects missing, duplicate, reordered, writable, non-regular, symlink,
   absolute, or parent-traversing source artifacts; and
5. atomically creates the attempt record with `0600` and `O_EXCL`.

It repeats the complete snapshot verification after attempt consumption and
before reading private input. No predecessor code is imported in either pass.

The runner itself cannot authenticate its own executing bytes. An operator must
execute it only from the exact externally reviewed, immutable, read-only snapshot
bound by the materialization-authorization record.

## Roles are distinct

- Staff, security/privacy, and operator review acceptance is advisory only.
- A separate materialization authority supplies the exact-bundle, expiry,
  attempt, custodian, ceremony-root, and private-input-bound authorization
  record.
- The operator executes only that offline ceremony.
- The owner has not signed or authorized a connected read.
- Connected-read, credential, provider/PSTN, staging/production, and Task 4.8
  authority do not exist.

“Operator approval” is not a valid status. Use “operator review acceptance.”

The standard-library runner validates the materialization record and its
self-digest, but it does not implement cryptographic proof of the external
authority. Provenance and custody of that record remain an external gate and
must be reviewed before any real attempt.

## Operator status card

| Item | Required state |
|---|---|
| Current state | `not_attempted`; V9 exact review incomplete; materialization blocked |
| Allowed now | Source review and fixture-only tests |
| Forbidden now | Any real private input, materialization attempt, signature, connected read, credential, provider/PSTN, staging/production, or Task 4.8 action |
| Success later | Exactly one canonical `unsigned-payload.json`, one payload-safe audit, and attempt state `generated_one_payload` |
| Validation failure later | Attempt state `consumed_no_payload`; no payload; no retry |
| Crash or residue later | Attempt state or inferred state `consumed_with_residue_stop`; quarantine; do not use output; no retry |
| Second/concurrent call | Rejected before private-input read |
| Custody | Private `0700` directory; inputs and created files `0600`; never Git, chat, argv values, environment values, stdout, stderr, traceback, or application logs |
| Terminal stop | Hash and validate the unsigned output, then take no signature or connected action |

Any consumed state ends V9 permanently. A failure, crash, partial ceremony, or
residue requires a new successor contract and fresh exact-hash review. Never
delete or reuse the attempt record to retry.

## Private ceremony directory

The runner accepts one absolute directory path and no private values on the
command line. The directory must be a pre-existing, non-symlink `0700`
directory outside the repository, chat-synced storage, application logs, and
ordinary shell history disclosure.

Before admission it contains exactly these reviewed inputs:

- `materialization-authorization.json`
- `private-input.json`

The runner may create only:

- `attempt-record.jsonl`
- `unsigned-payload.json`
- `payload-safe-audit.json`
- exclusive temporary files used for atomic no-replace publication

The materialization-authorization record binds the exact private-input file
digest. The private input is read only after the attempt record is durably
created. The attempt and audit contain stable states and digests only, never
private input or payload values.

The payload and audit are written to exclusive `0600` temporary files, fsynced,
and atomically linked into their final absent paths without replacement. A
crash before publication cannot expose a partial final file. Any temporary or
final residue after a failed ceremony is quarantined and cannot be used.

## One-attempt state machine

```text
not_attempted
  -> atomic attempt consumption
     -> consumed_no_payload
        -> generated_one_payload
        -> consumed_no_payload (validation or write failure)
        -> consumed_with_residue_stop (output/temp residue or ambiguous crash)
```

The first durable attempt event defaults to `consumed_no_payload`. A successful
payload and audit append a terminal `generated_one_payload` event. Failure
appends a stable payload-safe failure event when possible. A missing final event
after admission is still consumed and never retryable.

## Verification required before any real attempt

1. Exact-hash staff, security/privacy, and operator review with no unresolved P1.
2. A separate, exact-bundle-bound materialization-authorization record.
3. Independent V9 sextet and predecessor-manifest verification before starting
   Python.
4. Immutable/read-only snapshot, isolated offline host, and private custody.
5. Focused, full-suite, Ruff, concurrency, second-call, malicious predecessor,
   TOCTOU, crash, residue, file-mode, no-output, no-network, and no-subprocess
   checks.
6. Deny-only gate report remains `execution_status: not_authorized` with all
   nine blockers.

After a later successful attempt, independently validate the closed schema and
relationships, reproduce the canonical payload digest, and retain it only in
restricted private custody. Then stop.

## Sealed boundary

Until all later gates separately pass:

- create no project;
- make no connected inventory request;
- resolve no credential or secret;
- make no IAM, API, billing, database, workload, or retention change;
- make no provider or PSTN request;
- touch neither staging nor production; and
- keep Task 4.8 sealed.
