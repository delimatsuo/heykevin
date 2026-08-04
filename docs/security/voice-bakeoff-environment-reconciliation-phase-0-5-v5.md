# Voice bakeoff environment reconciliation Phase 0.5 v5

Status: source-only exact-review candidate. V5 is not an owner authorization,
credential, connected-read package, mutation plan, workload plan, or Task 4.8
admission.

Source baseline:
`2ed8ea7d1d7f338e84ddf08d5a50a714835e1533`.

## Recommendation

Create no Google Cloud project. Retain these four exact project IDs frozen:

- `kevin-491315`
- `kevin-staging-491315`
- `hk-voice-bakeoff-0724-iso`
- `hk-voice-bakeoff-preauth-iso`

The screenshots show account visibility, not duplicate project identity.
Display names and the Google account currently viewing the console do not
replace the exact project ID, immutable project number, and ancestry needed to
establish project identity.

The source-derived topology remains:

- `kevin-491315` contains the mixed production/staging Cloud Run control plane;
- `kevin-staging-491315` is the staging Firestore/RTDB data plane;
- `hk-voice-bakeoff-0724-iso` is the isolated execution-control project; and
- `hk-voice-bakeoff-preauth-iso` is the isolated preauth project.

The mixed staging control/data topology is separate environment debt. Phase
0.5 does not move, replace, merge, delete, or repair any project.

## What V5 changes

V2 remains the environment graph and request-method source. V3 remains
normative for response filtering, custody, principal isolation, receipt
failures, and authenticated request receipts. V4 remains normative for the
six-phase, 592-request maximum inventory contract and its exact 32-method
composite table.

V5 is a narrow successor to V4. It closes two review findings:

1. A later phase cannot advance from a caller-supplied predecessor digest. It
   must receive and authenticate the exact predecessor receipt and the private
   parameter allowlist bound by that receipt.
2. A manifest cannot pass by satisfying only broad counts and method names. It
   must exactly equal an independently derived, ordered expected manifest.

The V5 root schema const-pins the complete V5 package. Its normative digest is
computed over the complete package except the digest field itself:

`1f737311fda045e06544a7fa94fe51e179f38133f5d4f5d545d3eae15ed55c33`.

A future V5 envelope binds exact V2, V3, V4, V5, composite-method-table, and
effective-access digests. Omitting or changing one binding is invalid.

## Restricted private parameter allowlist

The private allowlist contains raw project numbers, ancestry/resource names,
member queries, and other exact values used to resolve a later phase. It is:

- restricted private custody material;
- forbidden from repository and chat storage;
- closed to the nineteen exact placeholder keys in the V5 package; and
- represented in checked-in receipts only by its canonical SHA-256 digest.

Its schema requires one binding for each of the four frozen project IDs, the
correct query-identity class for each project, non-zero project numbers,
configuration and parent digests, one session, one producing phase and
ordinal, the exact source SHA, and the exact V5 contract digest.

Every ordinary placeholder value is stored as a closed
`(project_id, value)` entry. Effective-access values additionally bind the
identity, principal, full resource name, and permission. Resolution therefore
requires the placeholder key, target project, and exact value to agree; global
membership alone is insufficient.

Schema validation is necessary but not sufficient. The future verifier must
also reject duplicate or missing project bindings, duplicate canonical
entries, type or cap mismatches, non-monotonic later-phase values, and a value
stored under the wrong placeholder or project binding.

No real private allowlist exists in this source-only package.

## Authenticated predecessor continuity

Phase 1 uses `GENESIS`. Before any later phase can be signed or dispatched, the
future verifier must receive:

- the current phase envelope;
- the signed predecessor receipt;
- its bound trusted public key;
- the private predecessor allowlist;
- the verification time; and
- the private phase-once ledger snapshot.

It must verify the receipt signature using the V5 receipt domain, recompute the
signed receipt digest, require a passing result with no error codes, and match
the session, immediately preceding phase and ordinal, source and all contract
digests, timestamps, cumulative request count, and consumed unique ledger
entry.

It must independently canonicalize the private allowlist and require the same
digest in:

- the authenticated predecessor receipt;
- the current envelope; and
- the recomputed private artifact.

Any mismatch aborts before signature or dispatch. There is no fallback,
counter reset, phase skip, or acceptance of a digest without its authenticated
artifact.

## Exact manifest equality

The expected ordered manifest is independently derived from:

- the exact source binding for phase 1; or
- the authenticated predecessor allowlist for later phases;
- the exact composite method table; and
- the phase-specific coverage algorithm.

The actual ordered manifest must equal it entry-for-entry after canonical
serialization. The verifier additionally requires:

- request count equals manifest length and the phase's exact or bounded count;
- each `request_index` equals its zero-based position;
- each `request_id` is `req-%03d` for that position;
- request IDs and canonical request digests are unique;
- the request-set digest matches the ordered manifest;
- every method field exactly matches the composite method table;
- every resolved value comes from the correct authenticated allowlist entry;
- no `{` or `}` remains in a final path or query;
- target ID, project number, identity, configuration, and quota project match
  the same exact project binding; and
- the phase-specific multiset has no omission, duplicate, extra, or reorder.

For phase 2 this means exactly twelve requests: provenance, ancestry, and
billing once for each of the four non-null project numbers authenticated by
phase 1. Twelve requests aimed at one project, null project numbers, literal
placeholders, duplicate IDs, or a fabricated predecessor digest all fail.

The same equality rule applies to the bounded derived manifests in phases
3–6. A cap overflow makes the inventory incomplete; it does not broaden the
allowlist.

## Safety boundaries

V3 response safety remains mandatory. Cloud Run and RTDB management responses
must be server-filtered to their allowed fields, and unexpected fields are
rejected before logging, serialization, projection, or custody. Raw evidence
and the private allowlist stay outside Git and chat.

Google documents the `fields` / `X-Goog-FieldMask` response filter:
<https://cloud.google.com/apis/docs/system-parameters>. Cloud Run service
resources include sensitive template and build configuration fields:
<https://cloud.google.com/run/docs/reference/rest/v2/projects.locations.services>.
The RTDB Management API lists `DatabaseInstance` resources:
<https://firebase.google.com/docs/reference/rest/database/database-management/rest/v1beta/projects.locations.instances/list>.

These references support only the source contract. They do not prove live
behavior or authorize a request.

## What can happen next

Do not authorize V5 itself.

V5 first requires exact-hash staff, security, and operator review with no
unresolved P1. After that review, source-only work may generate one
owner-signable phase-1 envelope with exactly four project-identity reads. That
envelope must still remain unexecuted until the owner separately signs that
exact reviewed one-use artifact.

Until then:

- retain all four projects frozen;
- create no project;
- make no connected inventory request;
- make no IAM, API, billing, credential, database, workload, or retention
  change;
- make no provider or PSTN request;
- do not touch staging or production; and
- keep Task 4.8 sealed regardless of any inventory result.
