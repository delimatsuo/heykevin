# Voice Bakeoff Bootstrap and Runtime Authorization Package

**Status:** review candidate, blocked and not owner-signable. No authorization
has been materialized, signed, consumed, or executed.

**Runtime source SHA:**
`2ed8ea7d1d7f338e84ddf08d5a50a714835e1533`.

**Bootstrap payload digest:**
`3323f05b3384f02ac87f111935304a6e0224720e1beab46fc91841a69b8caefb`.

**Read-only inventory digest:**
`71df00daca8e59998ec0ce91034cdf2c0c800aa6a92731c8dff8d8c693b31e44`.

**Time-invariant revalidation-state digest:**
`d7d81c1c26057493038f5a67ba4f9310ec80a3a8da3806d16455e06403704ad2`.

This package responds to the owner's 2026-07-28 instruction to verify the two
isolated bakeoff projects read-only and prepare an exact-source bootstrap/runtime
authorization package. It does not authorize IAM changes, credentials, workloads,
Firestore document access, provider/PSTN requests, Task 4.8, staging, or
production.

The canonical machine-readable review artifact is:

```text
docs/security/voice-bakeoff-bootstrap-runtime-authorization.review.json
```

The payload-safe read-only observation is:

```text
docs/security/voice-bakeoff-bootstrap-readonly-inventory-2026-07-28.json
```

The time-invariant state projection that must match before any later mutation is:

```text
docs/security/voice-bakeoff-bootstrap-revalidation-state-2026-07-28.json
```

## Verified current state

Read-only GCP control-plane queries were restricted to:

- `hk-voice-bakeoff-0724-iso`;
- `hk-voice-bakeoff-preauth-iso`.

No production or staging project was queried. No Firestore document was read or
written.

Both projects are active and billing-enabled. Each contains exactly one
Firestore Native database in `us-central1`:

| Domain | Project | Database | Current state |
| --- | --- | --- | --- |
| Execution control | `hk-voice-bakeoff-0724-iso` | `voice-bakeoff-control` | Active, pessimistic concurrency, one-hour version retention, PITR and deletion protection disabled |
| Pre-auth | `hk-voice-bakeoff-preauth-iso` | `voice-bakeoff-preauth` | Active, pessimistic concurrency, one-hour version retention, PITR and deletion protection disabled |

The expected control and pre-auth runtime identities are absent. Neither project
has a custom project role, conditional project binding, or user-managed
service-account key. Cloud Run, Compute Engine, GKE, Cloud Functions, Cloud Build,
Secret Manager, IAM Credentials, and STS APIs are disabled in both projects.

The control project has one observed service-account surface associated with its
existing Firebase/Firestore control-plane posture. It also has one enabled
software-protected symmetric KMS key. The key is not an Ed25519 signing key, has
not been approved as immutable authorization custody, and is not treated as an
owner trust root.

The active gcloud configuration has a quota project outside the two approved
isolated projects. Initial read-only calls targeted only the isolated resources,
but no later command may use that ambient quota context. Explicit isolated quota
checks show that Service Usage is available in both projects, while Cloud Resource
Manager, IAM, and Policy Troubleshooter must be enabled in the control project
before those APIs can use the control project as their explicit quota consumer.
The proposed bootstrap restores all three APIs to their disabled baseline.

Because the proposed service-account email can be named in another project's
policy before the account exists, a future owner-signable derivative also requires
a separately authorized, fresh, digest-bound negative-grant attestation across the
complete current production and staging project-level allow, deny, and
service-account impersonation policies. The current instruction keeps those
environments sealed, so that attestation was not collected and this package remains
blocked.

These facts prove only that the named isolation shell exists and is currently
fail-closed with respect to the proposed runtime identities. They do not prove
organization/folder inheritance, production isolation, effective runtime access,
provider privacy, residue handling, or execution authority.

## Why bootstrap and runtime are separate

A single envelope cannot safely authorize both IAM bootstrap and Task 4.8.
Bootstrap changes the identity and policy facts that a runtime envelope must bind.
The runtime package must therefore be generated only after bootstrap teardown and
fresh readback.

The package has two stages:

1. **Future one-use control bootstrap/revocation rehearsal.** Revalidates the
   fields reachable with explicit isolated quota, then enables only the
   three required control-plane APIs under the explicit control-project quota,
   uses that quota for a full read-only identity/policy/role revalidation of both
   isolated projects, and creates only the proposed
   control identity, minimal custom role, and database-conditioned binding;
   performs payload-safe policy readback and Policy Troubleshooter checks; removes
   the binding; disables the identity and custom role; restores the three APIs to
   disabled; then records a residue receipt.
2. **One-use per-arm runtime envelope.** Remains blocked until the pre-auth
   adapter, separate identity/IAM, trust/revocation store, broker, provider
   attestations, production denylist, immutable custody, residue path, and
   arm-specific configuration are complete and separately reviewed.

The bootstrap stage does not create a pre-auth identity or role. Creating dormant
pre-auth authority before a reviewed pre-auth transaction runner and
least-privilege permission set exist would weaken the intended separation.

## Bootstrap proposal

The proposed bootstrap is limited to the control project for mutation.
Read-only verification may examine both isolated projects.

### Permitted future mutations after owner signature

Only the following exact changes are proposed:

1. Enable Cloud Resource Manager, IAM, and Policy Troubleshooter APIs in the
   control project using that same project as the explicit quota project.
2. Create
   `voice-bakeoff-control-adapter@hk-voice-bakeoff-0724-iso.iam.gserviceaccount.com`
   without a user-managed key.
3. Create the project custom role
   `voiceBakeoffControlTransaction` with only:
   - `datastore.databases.get`
   - `datastore.entities.create`
   - `datastore.entities.get`
   - `datastore.entities.update`
4. Add one conditional binding using resource type
   `firestore.googleapis.com/Database` and the exact named
   `voice-bakeoff-control` database.
5. After readback and approved Policy Troubleshooter checks, remove that exact
   binding, disable the service account, disable the custom role, prove the exact
   IAM/identity/role terminal delta while the APIs remain available, restore the
   three APIs to disabled, and then prove their Service Usage state.

The permission set and condition remain proposals until
`list-testable-permissions` and Policy Troubleshooter prove their current GCP
semantics. An unsupported permission, an ambiguous condition, a broader grant, or
an unavailable result aborts and rolls back.

The payload digest binds an exact effective-access matrix: four grants on the
named control database, denials for delete/list on that database, denials for
the same four permissions on the pre-auth database, and project/resource-policy
absence checks for impersonation, signing, deployment, secret access, and
service enablement. Each API is enabled as its own mutation with an immediate
Service Usage readback. A partial or unknown result permits only readback and
disablement of the observed enabled subset before abort.

### Explicitly forbidden

The bootstrap cannot:

- create a pre-auth identity, role, binding, client, or token;
- read or write a Firestore document;
- create or invoke a workload;
- create a user-managed key, secret, provider credential, or PSTN credential;
- enable deployment, compute, broker, or secret-delivery services;
- query or mutate production or staging;
- contact Twilio, Gemini, Deepgram, ElevenLabs, or another workload provider;
- mount a runtime route or entry point;
- create a retention lock;
- execute Task 4.8.

### Materialization remains blocked

The reviewed JSON is not itself executable. After review with zero unresolved P1,
the owner could materialize a separate, externally custodied envelope only after
the domain-separated verifier, exact-action bootstrap executor, and durable atomic
approval/nonce consumer named by the JSON package are implemented and independently
reviewed. They do not exist in this slice.

The specified future envelope must contain:

- the fixed bootstrap payload digest;
- the review digest and reviewer provenance;
- a fresh externally generated nonce;
- issuance and expiry no more than 30 minutes apart;
- the current trust-snapshot reference;
- a fresh digest-bound negative-grant attestation for the exact proposed service
  account principal across the complete current production/staging project
  denylist;
- one detached Ed25519 owner signature;
- an explicit no-break-glass assertion.

Chat approval alone is not the detached signature required by the security
contract. No signing key or trust store is created by this package. The owner
should not approve or sign the bootstrap payload while its status is
`blocked_not_owner_signable`.

The negative-grant attestation is also not silently authorized by this package.
It requires a separate read-only scope approved by the owner because the current
scope expressly forbids production and staging queries.

## Runtime status

Runtime remains `not_authorized`. The two projects satisfy only the physical
project/database separation prerequisite. They do not close the other gates.

| Gate | Current state after read-only verification |
| --- | --- |
| Sealed owner authorization | Not recorded |
| Independent technical review | Bootstrap package pending; runtime review not started |
| Physically separate pre-auth store | Project/database observed; runtime adapter absent |
| Identity and credential broker | Not implemented |
| Durable trust and revocation store | Not implemented |
| Provider privacy and region attestations | Not recorded |
| Complete production denylist | Not recorded |
| Immutable custody and residue routing | Not implemented |
| One-use runtime envelope | Not materialized |

Every Task 4.8 arm requires its own later nonce, signature, caps, manifest,
configuration, dependency bindings, provider attestations, and evidence path. A
bootstrap signature cannot be reused as a runtime signature.

## Approval boundary

No owner approval is requested by this revision because the materialization
verifier, executor, and durable consumer are not implemented. A later reviewed
owner-signable derivative may authorize only the one-use bootstrap/revocation
rehearsal. It still could not authorize the pre-auth bootstrap, a Firestore
transaction, credential resolution, a workload, a provider or PSTN request,
Task 4.8, staging, or production.

If the bootstrap review finds a P1, the payload changes, or the read-only
inventory drifts, the current payload digest is invalidated and must be reviewed
again before the owner can sign it.
