# Voice bakeoff environment reconciliation Phase 0.5 v3

Status: source-only exact-review candidate. This document is not an owner
authorization, credential, connected-read package, mutation plan, workload
plan, or Task 4.8 admission.

Source baseline:
`2ed8ea7d1d7f338e84ddf08d5a50a714835e1533`.

## Recommendation

Do not create another Google Cloud project. Keep these four exact project IDs
frozen while their immutable project numbers and complete ancestry remain
unobserved:

- `kevin-491315`
- `kevin-staging-491315`
- `hk-voice-bakeoff-0724-iso`
- `hk-voice-bakeoff-preauth-iso`

The screenshots establish account visibility, not duplicate project identity.
The same immutable project may be visible from more than one Google identity.
That produces multiple visibility claims for one project; it does not justify
project creation, movement, replacement, or deletion.

The source currently describes `kevin-491315` as a mixed control-plane
container: production Cloud Run and the staging Cloud Run service are there,
while staging Firestore and RTDB data are in `kevin-staging-491315`. Treat that
as separate environment debt. Phase 0.5 does not repair it.

## Why v3 exists

V2 remains frozen as source-only evidence, but exact-hash staff and security
review rejected it as an input to a future connected-read package. V3 is a
normative amendment, not an in-place edit. It closes four high-risk gaps:

1. Direct Cloud Run and RTDB reads now include an exact server-side `fields`
   response filter. The raw response must pass an allowlisted-field gate before
   it can be logged, serialized, projected, or placed in private custody.
2. Owner approval is bounded to five phase envelopes with exact request
   manifests and a total ceiling of 256 network requests. A response with a
   next-page token is incomplete; it does not authorize a pagination request.
3. Receipts are authenticated and bound to the reviewed contract, exact request
   set, target, method, authorization, derived nonce, and ordered freshness
   window. Free-form error text is forbidden.
4. Effective-access evidence covers both proposed isolated principals, both
   isolated stores, production and staging stores, production and staging
   Cloud Run services, sensitive project permissions, and dynamic service
   account impersonation/signing permissions.

Google documents `fields` / `X-Goog-FieldMask` as the system parameter for
response filtering:
<https://cloud.google.com/apis/docs/system-parameters>. The Cloud Run service
resource includes sensitive configuration under `template` and `buildConfig`,
which is why a client-side projection is insufficient:
<https://cloud.google.com/run/docs/reference/rest/v2/projects.locations.services>.
The RTDB Management API returns `DatabaseInstance` objects, so list and get
requests also use server-side filtering:
<https://firebase.google.com/docs/reference/rest/database/database-management/rest/v1beta/projects.locations.instances/list>.

These references support the source-only contract. They do not prove provider
behavior and do not authorize a request.

## Composite interpretation

V2's environment graph, four-project boundary, source-derived resource edges,
and frozen default remain normative. V3 supersedes V2 only for:

- owner-authorization granularity;
- operator targeting;
- request caps and pagination;
- Cloud Run and RTDB direct-read contracts;
- coverage statements;
- effective-access tuples;
- receipts; and
- the transition sequence.

On a conflict, V3 wins. Neither artifact is owner-signable.

## Five sealed phases

Every phase is a separately reviewed and separately owner-signed envelope. It
contains the complete ordered request manifest before signature.

### 1. Identity binding

Maximum: 16 requests.

For each of the four exact project IDs, the manifest may contain only one
project identity read, one number-bound provenance read, one ancestry read, and
one billing-association read. The result binds the immutable project number,
complete ancestry digest, project provenance, and a digest of the billing
association. A missing or inaccessible value yields `incomplete`.

### 2. Metadata discovery

Minimum: 64 requests. Maximum: 96 requests.

This phase may begin only after the identity phase receipt passes. The exact
manifest is generated from those bound project numbers and ancestry values,
reviewed, and separately signed. It discovers names and types through:

- state of the twelve service names allowlisted by V2, not every enabled API;
- bounded Cloud Asset resource types and metadata;
- project and ancestor log-sink asset metadata, not log entries or routing
  effectiveness;
- Firestore database names, not documents; and
- RTDB instance names, not database URLs or records;
- KMS key identity/type/location/parent metadata, not algorithm, protection,
  rotation, cryptographic state, or key use; and
- secret metadata, never secret payloads.

### 3. Metadata detail

Minimum: 2 requests. Maximum: 32 requests.

This phase's manifest is derived only from a passing metadata-discovery receipt.
It includes every allowlisted Firestore database and RTDB instance name found
within cap, plus the two source-declared Cloud Run services. Detail remains
limited to:

- Firestore database metadata, never documents;
- RTDB instance name, project, state, and type, never database URLs or records;
- Cloud Run service identity, generation, timestamps, and etag.

### 4. Access-control discovery

Minimum: 4 requests. Maximum: 48 requests.

This phase discovers project and ancestor allow/deny policy, service accounts,
referenced roles, principal access boundary policy, organization-policy
constraints, and source-declared Cloud Run IAM policy. Its manifest is known
and separately reviewed after metadata detail.

### 5. Access-control detail

Minimum: 1 request. Maximum: 64 requests.

The final manifest is derived only from a passing access-discovery receipt. It
fetches every allowlisted discovered policy, service-account policy, referenced
role, effective organization policy, and exact Policy Troubleshooter tuple that
fits the phase cap. Anything over cap makes the inventory incomplete.

No phase can widen its request set. Across all five phases, the hard maximum is
256 network requests, one page per list/search, zero retries, and concurrency
one.

## One-use envelope and nonce

The owner signs a canonical RFC 8785 JSON phase envelope with Ed25519. The
envelope binds:

- source SHA;
- exact V2 and V3 contract digests;
- exact coverage digest;
- owner public-key digest;
- private identity/configuration digests;
- private raw-evidence custody digest;
- an owner-signed random envelope nonce seed;
- phase;
- ordered request manifest and count;
- request-set digest;
- issue and expiry times; and
- acknowledgment of audit and quota effects.

Each request nonce is:

```text
sha256(
  domain_separator
  || envelope_nonce_seed
  || uint32_be(request_index)
  || canonical_request_digest
)
```

The future executor must consume the nonce in a separately reviewed private
local one-use ledger before dispatch. There is no retry. Any started request
consumes its nonce even if the response is denied, malformed, partial,
unexpected, or over cap.

V3 defines the contract only. It does not implement a signer, verifier, ledger,
executor, identity loader, or network client.

## Response safety gate

Before a future direct read:

1. disable HTTP debug-body and raw exception logging;
2. require TLS and the exact endpoint;
3. enforce status, content type, and the 256 KiB per-response byte cap;
4. buffer only in memory and parse without logging or serialization;
5. reject every response path outside the exact server-side field mask;
6. discard an unexpected response in memory and consume the nonce; and
7. only then project into private custody or a payload-safe receipt.

Cloud Run permits only:
`createTime`, `etag`, `generation`, `name`, `observedGeneration`, `uid`, and
`updateTime`. In particular, `template`, environment values, secret references,
service accounts, images, traffic URLs, and build configuration are forbidden.

RTDB permits only instance `name`, `project`, `state`, and `type`.
`databaseUrl` and database records are forbidden.

## Effective-access matrix

The proposed principal references are:

- control:
  `voice-bakeoff-control-adapter@hk-voice-bakeoff-0724-iso.iam.gserviceaccount.com`
- preauth:
  `voice-bakeoff-preauth-adapter@hk-voice-bakeoff-preauth-iso.iam.gserviceaccount.com`

Current inventory expects every tuple to be `NOT_GRANTED` or the proposed
principal to be absent. Future runtime expectations are separate proposal
facts:

- control may receive only a future reviewed narrow grant on the control store;
- preauth may receive only a future reviewed narrow grant on the preauth store;
- each principal must be denied from the other isolated store;
- both must be denied from production and staging data and Cloud Run;
- both must lack project-scoped API enablement, project IAM mutation, and
  Cloud Run creation;
- both must lack KMS decrypt on every enumerated CryptoKey and secret payload
  access on every enumerated Secret; and
- both must lack deployment, deletion, invocation, impersonation, token, and
  signing permissions everywhere outside the exact future same-domain
  proposal.

An unknown, denied, partial, unsupported, or over-cap result is `incomplete`;
it is never interpreted as proof of absence.

## Receipts

Every payload-safe phase receipt contains one receipt for every exact manifest
request. A receipt is invalid unless an independent future verifier checks:

- exact source, V2, V3, and coverage digests;
- owner envelope and receipt signatures;
- exact phase, request count, ordered request-set digest, and request mapping;
- request ID, target, method, canonical request digest, authorization digest,
  and derived nonce for every request;
- one-use consumption before dispatch;
- project-specific query identity;
- `issued_at <= completed_at < expires_at` and verification before expiry;
- server-side response filter and pre-custody field gate;
- no next-page token; and
- only enumerated error codes with no raw/free-form evidence.

Many-to-many visibility claims are first-class receipt items. Two identities
may produce separately authenticated claims for the same project ID and number.
That never recommends a duplicate.

A passing receipt changes only inventory evidence. It does not authorize IAM,
credentials, workloads, provider/PSTN access, staging, production, bootstrap,
runtime, or Task 4.8.

## Complete operator states

The JSON contains four complete fixtures:

- sealed source-only;
- future identity binding;
- future bound metadata read; and
- blocked incomplete.

Each fixture renders the same nineteen fields, including the consistent
`response_field_mask` name. A future operator surface must render all nineteen
before any request.

## What can happen next

Do not authorize V3 itself. First obtain exact-hash staff, security, and
operator-experience approval of the composite V2+V3 contract.

Only after that review may source-only work generate one owner-signable
`identity_binding` phase envelope. That successor must contain real private
identity/configuration/custody bindings and the exact request manifest, but
must still remain unexecuted until the owner signs that exact reviewed
envelope.

Until then:

- keep all four projects frozen;
- create no project;
- make no IAM, API, database, billing, credential, or workload change;
- make no provider or PSTN request;
- do not touch staging or production; and
- keep Task 4.8 sealed.
