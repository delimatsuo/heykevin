# Voice bakeoff phase 0.5 V10: fail-closed acceptance contract

## Status

Real payload materialization is blocked. Do not invoke the V9 materializer. V10
contains no materialization command and grants no credential, provider, PSTN,
staging, production, or Task 4.8 authority. Its only purpose is to make the
blocking decision and the two required acceptance layers machine-checkable.

The V9 exact review was unanimous: staff, security/privacy, and operator accepted
retention as advisory non-authorizing evidence and blocked real materialization.
V10 does not reinterpret that review as authority.

## What an operator may do now

An operator may inspect public contracts, run the offline V10 verifier, run
fixture-only tests, and obtain exact-hash advisory review. An operator must not
create real private input or authorization records. If a future successor
introduces a ceremony-directory argument, it must never contain sensitive information.

## Two acceptance layers

Every `L` requirement is a local runnable-candidate invariant. These include
directory-descriptor anchoring, exact initial inventory, file and directory
durability before private reads, invocation-time freshness, distinct terminal
states, closed schemas, a committed-envelope usability predicate, process/crash
tests, and safe custody preflight.

Every `E` requirement is external ceremony evidence. These include authenticated
authority provenance, global deletion/restore-resistant one-use custody, an
isolated provenance-bound runtime, a trusted clock and custody medium, and
separate acceptance by reviewers, materialization authority, and custodian.

All requirements are currently `not_satisfied`. Passing only one layer never
permits materialization.

## Operator decision table

| State | Consumed | Usable | Required action |
| --- | --- | --- | --- |
| `pre_admission_rejected` | No | No | Stop. Correct prerequisites only in a new exact successor and authorization. |
| `consumed_no_payload` | Yes | No | Stop and quarantine. Never retry the authorization. |
| `consumed_with_residue_stop` | Yes | No | Quarantine all residue. Never retry the authorization. |
| `generated_one_payload` | Yes | Only as a complete committed envelope | Retain without execution. The payload grants no connected authority. |

A future payload is usable only if the payload, matching successful audit, and
durable terminal attempt event agree exactly. A payload file by itself is always residue
and must be quarantined.

## Current verifier outcomes

The V10 verifier returns `0` only when the public V10 contract is internally
consistent, its schema is closed, and the exact V9 predecessor bundle is present.
That success means `verified_blocked_contract`; it never means ready or
authorized. Exit `64` is CLI misuse, `65` is a public-contract verification
failure, and `70` is an unexpected verifier failure. The verifier reads no
private input and performs no connected action.

## Promotion rule

Promotion requires a new exact runnable successor, independent staff,
security/privacy, and operator acceptance, independent evidence for every local
and external requirement, and a separate exact-SHA one-use materialization
authorization. There is no automatic promotion.
