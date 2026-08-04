# Voice bakeoff environment reconciliation Phase 0.5 v4

Status: source-only exact-review candidate. V4 is not an owner authorization,
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

The screenshots show different account visibility, not duplicate projects.
Project identity requires the exact project ID, immutable project number, and
complete ancestry. One project may have authenticated visibility claims from
multiple identities without being moved, replaced, deleted, or duplicated.

The source-derived topology remains:

- `kevin-491315` is a mixed control-plane container for production and staging
  Cloud Run;
- `kevin-staging-491315` is the staging data-plane project;
- `hk-voice-bakeoff-0724-iso` is the isolated execution-control project; and
- `hk-voice-bakeoff-preauth-iso` is the isolated preauth project.

The staging control/data split is separate environment debt. Phase 0.5 does not
repair it.

## Composite contract

V2 remains the environment graph and method source. V3 remains normative for
server-side Cloud Run/RTDB response filtering, the pre-custody raw-response
gate, the two-principal isolation matrix, enumerated receipt errors, and
authenticated request receipts.

V4 supersedes V2/V3 only where its JSON says so. In particular, V4 replaces the
status model, phase plan, session sequencing, operator fixtures, and transition
policy.

The V4 schema const-pins the complete V4 package. It also defines closed schemas
for a future V4 phase envelope and phase receipt. A nested contract change
therefore fails schema validation unless a versioned successor updates the
complete package, digest, tests, and exact review.

V4 binds five distinct digests:

- exact V2 JSON;
- exact V3 JSON;
- exact V4 normative contract;
- the derived 32-method composite table; and
- the V3 effective-access contract.

The V4 normative digest covers the complete V4 package except for its own
digest field. An envelope that omits or changes one binding is invalid.

## Exact composite method table

The future verifier must:

1. load the exact V2 method table;
2. remove the three methods replaced by V3;
3. insert all three V3 replacements and the V3 ancestor-log-sink addition;
4. canonicalize the resulting 32-method table; and
5. match
   `4603e4b83775cc87a8d076da1398c3113a02b758b6a244fe50ed28edc8f64967`.

Every manifest entry must exactly match the selected composite method's API
method, endpoint, HTTP verb, fully resolved path and query, body digest,
response mask, effective V4 pagination rule, and evidence class. V3 forbidden
response paths and scope rules also remain mandatory.

There is no method-validation fallback. A V3-labelled method cannot carry V2
or arbitrary request data.

## Six dependency-safe phases

Every phase is a separately reviewed, separately owner-signed exact manifest.
The maximum across all six is 592 connected requests. A next-page token,
resource cap, request cap, or missing derived request makes the inventory
incomplete; it never authorizes another request.

### 1. Project ID binding

Exactly four requests: one `project_identity_get` for each exact project ID.
No project number is required in this manifest. The receipt binds all four
immutable numbers and immediate-parent digests.

### 2. Project-number binding

Exactly twelve requests: provenance, ancestry, and billing for each exact
project number from phase 1. This separation removes the V3 bootstrap cycle.

### 3. Metadata discovery

Between 64 and 96 requests. This phase observes the twelve allowlisted service
states per project, bounded Cloud Asset metadata, project/ancestor log-sink
metadata, Firestore database names, and RTDB instance names.

### 4. Metadata detail

Between 2 and 32 requests. Its manifest is derived only from the authenticated
metadata-discovery receipt. It reads every allowlisted discovered Firestore
database and RTDB instance within cap, plus the two source-declared Cloud Run
services. V3's server-side response filters and raw-response gate apply.

### 5. Access-control discovery

Between 4 and 64 requests. This phase discovers project/ancestor/resource
policy, deny-policy names, service-account names, PAB policy names,
organization-policy constraints, and Cloud Run IAM policy.

The V4 scope and member-query caps produce at most 52 discovery requests,
below the 64-request phase ceiling.

PAB binding search is not in this phase because its exact policy names do not
exist until this phase's receipt passes.

### 6. Access-control detail

Between 88 and 384 requests. The minimum 88 covers the static two-principal
matrix:

- 8 data-resource tuples times 6 permissions = 48;
- 4 Cloud Run tuples times 4 permissions = 16; and
- 2 principals times 4 projects times 3 project permissions = 24.

The phase also covers bounded dynamic secret, KMS, service-account, deny,
role, PAB, organization-policy, and Policy Troubleshooter requests. The
worst-case V4 resource caps require at most 320 requests, below the 384-request
phase ceiling. If a real inventory exceeds any resource cap, the result is
`incomplete`.

## Session and phase-once enforcement

All six envelopes bind one owner-selected UUIDv4 inventory-session ID. Every
envelope includes:

- exact phase and ordinal;
- predecessor phase-receipt digest, or `GENESIS` for phase 1;
- predecessor payload-safe parameter-allowlist digest, or `GENESIS` for phase 1;
- cumulative prior and resulting request counts;
- exact contract and method-table digests;
- identity/configuration and private-custody digests;
- complete ordered request manifest;
- one-use nonce seed;
- issue and expiry times;
- audit/quota acknowledgment; and
- owner Ed25519 signature.

The separately reviewed private ledger accepts at most one envelope and one
receipt for `(inventory_session_id, phase_ordinal)`. It consumes the phase entry
before the first request. Every later phase must bind the immediately preceding
authenticated passing receipt. Cumulative count must equal prior count plus the
current exact request count and may never exceed 592.

Every method placeholder must resolve from the exact project binding or the
authenticated predecessor's parameter allowlist. The next receipt signs the
new payload-safe allowlist digest; raw names remain in private custody.

This prevents a caller from replaying a phase, signing competing phase
manifests, skipping an ordinal, resetting the global counter, or advancing from
an incomplete receipt.

V4 defines these checks; it does not implement a signer, verifier, ledger,
identity loader, credential resolver, or network client.

## One canonical status model

V4 explicitly supersedes the V2 status model. The canonical current values are:

```text
inventory_status       incomplete
governance_status      undecided
review_status          pending_exact_artifact_review
connected_read_status  not_authorized
mutation_status        mutation_not_authorized
execution_status       execution_not_authorized
dominant_status        execution_not_authorized
```

The exact precedence starts with `execution_not_authorized`, then
`mutation_not_authorized`, then connected-read authority. Execution therefore
remains the dominant sealed state even during a future owner-signed read-only
phase.

All four JSON operator fixtures render the same 25 fields and use only the V4
vocabulary:

- sealed source-only;
- future project-ID binding;
- future number-bound/detail phase; and
- blocked incomplete.

Every banner says execution is sealed. The corrected field name is always
`response_field_mask`.

## Safety boundaries

V3's response safety remains mandatory:

- Cloud Run is filtered server-side to identity, generation, timestamps, and
  etag; template, environment values, secret references, service accounts,
  images, build configuration, traffic, and URLs are forbidden.
- RTDB is filtered server-side to instance name, project, state, and type;
  database URL and records are forbidden.
- An unexpected field is rejected before logging, serialization, projection,
  or custody; the started nonce remains consumed.
- Raw evidence remains outside Git and chat.

Google documents `fields` / `X-Goog-FieldMask` for response filtering:
<https://cloud.google.com/apis/docs/system-parameters>. Cloud Run's service
resource includes sensitive `template` and `buildConfig` fields:
<https://cloud.google.com/run/docs/reference/rest/v2/projects.locations.services>.
The RTDB Management API returns `DatabaseInstance` objects:
<https://firebase.google.com/docs/reference/rest/database/database-management/rest/v1beta/projects.locations.instances/list>.

These references support the source-only contract. They neither prove live
provider behavior nor authorize a request.

## What can happen next

Do not authorize V4 itself.

First obtain exact-hash staff, security, and operator approval with no
unresolved P1. Only then may source-only work generate one owner-signable
`project_id_binding` envelope containing exactly four read requests. That
successor must still remain unexecuted until the owner signs that exact,
reviewed envelope.

Until then:

- retain all four projects frozen;
- create no project;
- make no IAM, API, database, billing, credential, or workload change;
- make no connected inventory, provider, or PSTN request;
- do not touch staging or production; and
- keep Task 4.8 sealed.
