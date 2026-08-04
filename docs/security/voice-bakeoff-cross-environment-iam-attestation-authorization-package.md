# Voice Bakeoff Cross-Environment IAM Attestation Authorization Package

**Status:** preparation authorized; execution not authorized. This is a blocked,
non-owner-signable review candidate.

**Runtime source SHA:**
`2ed8ea7d1d7f338e84ddf08d5a50a714835e1533`.

**Bound bootstrap payload digest:**
`3323f05b3384f02ac87f111935304a6e0224720e1beab46fc91841a69b8caefb`.

**IAM-attestation payload digest:**
`bc606630fb32ff8c05e91fc838b0ea300645c503d2a798fb7cccd8729f8f1e77`.

**Full reviewed-contract digest:**
`e0253780d8bf370bfb10843173a52cc6b1036ad542ebfb11a5853c71f95142f0`.

The owner authorized preparation of this package on 2026-07-28. That instruction
does not authorize a production or staging query. It does not authorize IAM or
resource mutations, credentials, impersonation, workloads, Firestore documents,
provider/PSTN access, the bootstrap rehearsal, runtime materialization, or Task
4.8.

The canonical machine-readable artifact is:

```text
docs/security/voice-bakeoff-cross-environment-iam-attestation-authorization.review.json
```

## Purpose

The future control service-account principal belongs to an isolated project, but
another project can name that principal in an IAM binding before the service
account exists. This package defines a one-use read-only attestation that would
search for those pre-existing grants and service-account impersonation paths.

An attestation pass would be evidence only. It could unblock creation of a fresh
bootstrap authorization candidate, but it could not authorize IAM bootstrap,
runtime, provider/PSTN access, or Task 4.8.

## Source-derived target inventory

The exact source at the bound SHA declares:

| Project | Source-declared use |
| --- | --- |
| `kevin-491315` | Production data and Cloud Run; staging Cloud Run control plane |
| `kevin-staging-491315` | Staging data |

This two-project list is source-derived and has not been verified against current
GCP state. The package prohibits project discovery and querying an unlisted
project. It cannot become owner-signable until the owner confirms that this is
the complete current production/staging project inventory and binds that
inventory's digest.

The future service account would also automatically belong to Resource Manager
service-account principal sets for its isolated control project and every parent
folder and organization. The control project's numeric project ID and ancestry
are not bound yet. Before materialization, the owner must confirm and digest-bind
that exact principal-set inventory. A placeholder, missing ancestor, or unknown
number keeps the package blocked.

The IAM-bearing resource inventory and per-asset-type policy coverage matrix are
also absent. Cloud Asset IAM search does not cover every possible asset type.
A later derivative must bind a complete owner-confirmed inventory and prove an
exact policy-read method for every resource type that is actually present.

## Exact future read-only scope

Only a later, separately signed one-use envelope could permit these reads:

1. Verify required APIs are already enabled. API enablement is forbidden.
2. Read identity, lifecycle, ancestry, and the applicable project/ancestor IAM
   allow policies and etags for the two exact projects. Every `getIamPolicy`
   request must request policy version 3 so conditional bindings cannot be
   omitted. List every applicable deny policy and then fetch each policy
   individually because list results omit deny rules.
3. Enumerate IAM-bearing resources and prove that every present resource type
   has an exact supported policy-read method.
4. Search supported resource-level IAM allow policies for:
   - the exact proposed future bakeoff principal;
   - every materialized Resource Manager project/folder/organization
     service-account principal set that would contain it;
   - `allUsers`;
   - `allAuthenticatedUsers`.
5. Enumerate service-account metadata and read each service account's version-3
   IAM policy to detect exact-principal or encompassing-principal-set
   impersonation grants. No impersonation is permitted.
6. Resolve every matched predefined or custom role to its permissions.
7. Run the bound Policy Troubleshooter matrix only if the API accepts the
   absent future principal. Until the principal exists, this is supplementary
   evidence rather than a live non-reachability proof.
8. Emit only a payload-safe receipt. Raw policies and identity listings stay
   out of Git and chat.

The receipt must authenticate its origin. It carries the attestation payload and
reviewed-contract digests, approval and nonce digests, authorization-envelope
self-digest, executor and verifier artifact digests, result, target inventory,
and completion time. A separately trusted Ed25519 attestor signs the
domain-separated canonical receipt payload. Unknown signature, trust, executor,
verifier, approval, nonce, or envelope provenance returns `incomplete`.

The receipt payload digest excludes itself plus the detached signature reference
and signature digest, avoiding self-reference. Signature verification output is
kept in a separate content-addressed verification record. That record binds the
receipt payload digest, signature digest and custody reference, attestor trust
snapshot, verifier artifact, result, and verification time.

Before the first external query, a durable create-once consumer must atomically
consume both the approval ID and nonce. Duplicate, unavailable, or unknown
consumption aborts before querying. After any external query, the approval and
nonce can never be retried or reused, including after partial execution.

The future owner signs the authorization envelope's self-digest, not merely a
subset of envelope fields. The envelope self-digest covers the exact verifier,
durable approval/nonce consumer, read-only executor, receipt attestor, and
receipt-verifier artifact digests; only the detached owner signature and the
self-digest field itself are excluded from that digest.

Every request must use the corresponding target project as its explicit quota
project. Ambient quota, ADC discovery, credential export, access-token printing,
service-account impersonation, fallback API enablement, retries, and concurrency
are forbidden.

The canonical package now pins every allowlisted method to a request template.
Project identity is a bodyless
`GET /v1/projects/{EXACT_TARGET_PROJECT_ID}`. Project ancestry is a zero-length
`POST /v1/projects/{EXACT_TARGET_PROJECT_ID}:getAncestry`. Only after the
identity response supplies the exact project number may API state be checked
with bodyless
`GET /v1/projects/{EXACT_TARGET_PROJECT_NUMBER}/services/{EACH_REQUIRED_SERVICE_NAME}`.
Each request carries `x-goog-user-project: {EXACT_TARGET_PROJECT_ID}` and an
unexpected parameter or response field makes the result `incomplete`.

These future reads would still create ordinary Cloud Audit Log and quota-accounting
records in the target projects. They are not resource mutations, but the later
owner signature must explicitly accept those observable control-plane effects.

## Completeness and fail-closed behavior

A result can be only `pass`, `fail`, or `incomplete`. `pass` requires:

- a complete owner-confirmed two-project inventory digest;
- the exact owner-confirmed control-project/ancestry principal-set inventory;
- an owner-confirmed IAM-bearing resource inventory and asset-type coverage
  matrix;
- all required APIs already enabled without mutation;
- full pagination and visibility for project and supported resource policies;
- every listed deny policy fetched with its complete rules;
- all matched roles resolved;
- zero binding that names the future principal;
- zero binding or impersonation grant that names an encompassing Resource
  Manager service-account principal set;
- zero deny-policy exception that names either the exact future principal or an
  encompassing principal set;
- zero unexpected broad public/authenticated binding. Every broad binding must
  be an exact subset of a separately owner-confirmed resource/member/role/
  condition exception manifest;
- external encrypted immutable custody for raw evidence;
- a verified authenticated receipt whose authorization, nonce, reviewed
  contract, executor, verifier, and attestor trust bindings all match;
- a payload-safe receipt no older than 15 minutes;
- later policy-etag, receipt-digest, control-project number, ancestry, and
  principal-set inventory revalidation immediately before any future identity
  creation. Moving the isolated project can change encompassing principal sets
  without changing a target project's IAM-policy etag.

An unavailable API, missing permission, omitted policy, unsupported relevant
asset type, unresolved role, pagination overflow, cap breach, ambiguous
Troubleshooter result, unlisted project, or unknown state returns `incomplete`
and aborts. No mutation or fallback is allowed.

The source suggests that the production and staging Cloud Run services may
intentionally have exact `allUsers`/`roles/run.invoker` bindings. Those two
resource-specific entries are only a proposed exception manifest until the
owner confirms its digest. Any other broad binding fails. Public invocation
also remains a runtime network-denylist concern and does not authorize a request.

## Bounded caps

The future envelope is capped at one preexisting gcloud user, two projects, eight
ancestors per project,
50 service accounts per project, 10 Cloud Asset pages per project per query,
200 total external API requests, 10 MiB of raw response material, one actor,
one concurrent operation, no retries, and ten minutes wall-clock time.

All mutation, API enablement, impersonation, exported-token, document,
workload, provider, and PSTN caps are zero.

## Why this is not ready for execution

The complete target-inventory, principal-set, IAM-bearing-resource,
asset-coverage, and broad-binding-exception digests are absent. The exact
envelope verifier, read-only executor, durable one-use nonce consumer, and
external encrypted immutable evidence custody do not exist. Neither do the
receipt attestor signer, trust snapshot, or receipt verifier. The
owner-authorization fields are therefore null and `not_materializable`.

The current chat authorization covers preparation only. Do not execute the
attestation or populate owner-authorization fields from that instruction.
