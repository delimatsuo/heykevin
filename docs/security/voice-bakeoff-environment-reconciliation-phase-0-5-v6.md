# Voice bakeoff environment reconciliation Phase 0.5 v6

Status: source-only exact-review candidate for one narrow operation:
deterministically validating generation of an unsigned phase-1 signing payload.
V6 is not an owner signature, authorization record, connected-read package,
credential, mutation plan, workload plan, or Task 4.8 admission.

Source baseline:
`2ed8ea7d1d7f338e84ddf08d5a50a714835e1533`.

## Recommendation

Create no Google Cloud project. Retain these four exact project IDs frozen:

- `kevin-491315`
- `kevin-staging-491315`
- `hk-voice-bakeoff-0724-iso`
- `hk-voice-bakeoff-preauth-iso`

The screenshots show what each signed-in account can see. They do not show
duplicate project identity. Establishing identity still requires the exact
project ID, immutable project number, and complete ancestry.

The source-derived topology remains:

- `kevin-491315` contains the mixed production/staging Cloud Run control plane;
- `kevin-staging-491315` is the staging Firestore/RTDB data plane;
- `hk-voice-bakeoff-0724-iso` is the isolated execution-control project; and
- `hk-voice-bakeoff-preauth-iso` is the isolated preauth project.

The mixed staging control/data arrangement is separate debt. This package
does not move, merge, replace, delete, create, or repair a project.

## Why V6 is narrower

The exact V5 panel review rejected V5's six-phase qualification claim. V5
closed phase-1 and phase-2 manifest attacks, but it did not qualify:

- atomic current-phase replay prevention;
- concrete Policy Troubleshooter request-body binding;
- the complete owner-bound static seed;
- method-specific query encoding and aggregate serialization;
- future-receipt rejection; or
- phase 3–6 manifest derivation.

V5 is preserved at its reviewed hashes. V6 does not pretend those findings are
fixed. Instead, it makes every dispatch and every phase after phase 1 an
explicit blocker and removes them from the qualified scope.

This is deliberate scope containment. V6 may qualify only offline construction
of a phase-1 owner-signing payload. It cannot qualify a signature or a request.

## Exact phase-1 payload

The future payload contains exactly four ordered manifest entries. Each is the
same bodyless Cloud Resource Manager `projects.get` operation applied once to
the corresponding frozen project ID:

1. `kevin-491315`
2. `kevin-staging-491315`
3. `hk-voice-bakeoff-0724-iso`
4. `hk-voice-bakeoff-preauth-iso`

Every entry binds:

- its zero-based position and `req-%03d` request ID;
- the exact target and quota project ID;
- the identity class and its private configuration digest;
- `GET https://cloudresourcemanager.googleapis.com/v1/projects/{project_id}`;
- a canonical `null` request body and its exact digest;
- only `projectId`, `projectNumber`, `lifecycleState`, and `parent`;
- no pagination;
- restricted external evidence custody; and
- a digest of the complete manifest entry.

The payload binds the exact source, V2, rejected V5, V6, and phase-1 method
contract digests; one owner-selected UUIDv4 session; owner public-key,
identity-configuration, and raw-custody digests; a one-use nonce seed; the
ordered request-set digest; issue/expiry times; and audit/quota acknowledgment.

The schema contains no owner-signature field. The materialized payload is the
thing an owner could later choose to sign through a separate authorization
record; its generation is not that signature.

## Private inputs needed for materialization

After a clean V6 review, materialization would still require the owner to
supply, in separate private custody:

- an inventory-session UUIDv4;
- owner public-key digest;
- the organization-operator configuration digest;
- the isolated-bakeoff-operator configuration digest;
- raw-evidence custody digest;
- one-use nonce seed; and
- issue and expiry timestamps.

These are digests and bounds, not credentials. V6 generation accepts no access
token, service-account key, provider secret, raw cloud response, project
number, ancestry, or private parameter allowlist. A materialized payload must
remain outside Git and chat.

## Dispatch is still sealed

Even an exactly generated payload would have:

```text
owner_signature_status       not_recorded
owner_authorization_status   not_recorded
connected_inventory_status   not_authorized
mutation_status              not_authorized
execution_status             not_authorized
task_4_8_status              sealed
```

An owner signature alone would not make it dispatchable. Before any future
connected read, a separately reviewed runtime must atomically consume the
current `(session, phase)` entry before request 1 and reject a second dispatch.
It must also provide the source-pinned identity/credential broker,
server-filtered response rejection, restricted custody, receipt signer, and
other blockers listed in the V6 JSON.

None of those implementations exists in V6. Therefore the V5 replay,
request-body, source-seed, encoding, freshness, and later-phase findings cannot
be reached through this package.

## What can happen next

Do not authorize V6 itself.

First obtain exact-hash staff, security, and operator approval with no
unresolved P1 for this narrow generation-only scope. Only then may source-only
work materialize one unsigned payload from separately supplied private
generation inputs.

Until a later exact payload and separately custodied owner authorization record
are reviewed:

- create no project;
- make no connected inventory request;
- create no IAM, credential, workload, database, billing, or retention state;
- make no provider or PSTN request;
- do not touch staging or production;
- generate no phase 2–6 payload; and
- keep Task 4.8 sealed.
