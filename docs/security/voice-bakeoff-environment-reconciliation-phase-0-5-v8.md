# Voice bakeoff environment reconciliation Phase 0.5 v8

Status: source-only exact-review candidate. V8 qualifies only fail-closed
materialization of one validated, unsigned phase-1 signing payload. It is not
a signature, authorization record, connected read, credential, runtime,
mutation plan, or Task 4.8 admission.

Source baseline:
`2ed8ea7d1d7f338e84ddf08d5a50a714835e1533`.

## Recommendation

Create no Google Cloud project. Retain these four exact projects frozen:

- `kevin-491315`
- `kevin-staging-491315`
- `hk-voice-bakeoff-0724-iso`
- `hk-voice-bakeoff-preauth-iso`

The screenshots show account visibility, not duplicate identity. Project
identity still requires the exact project ID, immutable project number, and
complete ancestry. The mixed staging topology is separate debt and remains
unchanged.

## V8 closure

V7 made validation mandatory before any payload return and bounded lifetime to
15 minutes. Security review found that Python still accepted integers too
large to round-trip exactly through the IEEE-754 number model used by
cross-language RFC 8785 implementations.

V8 requires all three timestamps to be exact integers, never booleans, in this
inclusive range:

```text
1 <= issued_at_ms, validation_time_ms, expires_at_ms
  <= 9007199254740991
```

It also retains:

```text
issued_at_ms <= validation_time_ms < expires_at_ms
expires_at_ms - issued_at_ms <= 900000
```

The public V8 materializer applies those checks before calling the already
fail-closed V7 validation path. It then binds exact V2, V5, V6, V7, V8, and
bodyless phase-1 method digests, validates the closed V8 schema and full
four-request relations, and returns only when the complete error set is empty.

Tests cover the inclusive minimum, a valid payload ending at the maximum,
zero, negative values, booleans, non-integers, and `9007199254740992` for each
timestamp. Every invalid case raises and returns no payload.

## Bundle identity and invocation integrity

The `source_sha` is the tracked runtime baseline only. It is not the identity
of this untracked source-only review bundle and cannot be cited as proof that
the V8 quartet is committed.

The package therefore records an ordered, exact-hash manifest for all 28 V1-V7
predecessor JSON, schema, guide, and test artifacts. Its canonical digest is:

`0eb91f2be8ed804400c1c267e7f244e33de244589c5a26a27261b5feb8065326`.

The public V8 operation verifies the hard-pinned manifest digest, every
predecessor file path, and every predecessor file SHA-256 before it validates
private inputs or constructs a candidate. Missing, changed, duplicated,
reordered, absolute, or parent-traversing entries fail without a payload.
The returned payload binds the manifest digest.

V8 cannot self-authenticate its own executing code. Exact-hash staff, security,
and operator review must externally bind the V8 JSON, schema, guide, and test
quartet. Immediately before a one-time call, the operator must recheck that
quartet and the predecessor manifest from the same immutable, read-only
snapshot. Any drift invalidates review.

## Scope remains sealed

The only possible output is an unsigned four-project `projects.get` signing
payload in restricted private custody. It contains no owner-signature or
authorization record and accepts no credentials, tokens, keys, responses,
project numbers, ancestry, or private allowlist.

Atomic current-phase replay prevention remains unimplemented and blocks every
dispatch. Dynamic bodies, owner-bound seeds, encoding, receipt freshness, and
phases 2–6 remain unqualified. No credential broker, network client, response
handler, or receipt signer exists here.

## What can happen next

Do not authorize V8 itself.

After exact-hash staff, security, and operator approval with no unresolved P1,
and an immediate same-snapshot integrity recheck, the V8 materializer may be
called once with separately supplied private inputs and trusted validation
time. The result must be independently schema/relationship validated,
canonicalized, hashed, and retained only in restricted private custody. It
would still be unsigned and non-authorizing:

```text
owner_signature_status       not_recorded
owner_authorization_status   not_recorded
connected_inventory_status   not_authorized
mutation_status              not_authorized
execution_status             not_authorized
task_4_8_status              sealed
```

Until a later exact payload and separately custodied owner authorization record
are reviewed, create no project and make no connected read, IAM/credential,
workload, database, billing, retention, provider/PSTN, staging, production, or
Task 4.8 action.
