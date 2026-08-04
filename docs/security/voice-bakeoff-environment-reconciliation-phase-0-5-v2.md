# Voice Bakeoff Environment Reconciliation — Phase 0.5 v2

**Status:** source-only exact-review candidate; not owner-signable.

**Source SHA:** `2ed8ea7d1d7f338e84ddf08d5a50a714835e1533`.

**Canonical candidate:**
`docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.json`.

**Receipt and package schema:**
`docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.schema.json`.

This candidate authorizes no Google Cloud read. It also authorizes no project,
billing, API, IAM, database, credential, workload, provider/PSTN, staging,
production, or Task 4.8 action.

```text
CONNECTED EXECUTION SEALED — READ-ONLY RECONCILIATION NOT AUTHORIZED
```

## Recommendation

Retain the four source-declared projects frozen. Do not create, move, replace,
delete, or modify a project.

The screenshots do not show a duplicate project. They show that Google
identities can have different visibility into projects and organizations. One
project can be visible to multiple accounts. The only valid identity test is
the exact project ID plus its immutable project number; display names and the
currently selected Google account are not resource identifiers.

The source-declared graph remains:

| Exact project ID | Container role | Source-declared resources |
| --- | --- | --- |
| `kevin-491315` | Mixed production and staging control-plane container | Production Cloud Run, staging Cloud Run, production Firestore, production RTDB |
| `kevin-staging-491315` | Staging data container | Staging Firestore and RTDB |
| `hk-voice-bakeoff-0724-iso` | Bakeoff execution-control container | `voice-bakeoff-control` Firestore |
| `hk-voice-bakeoff-preauth-iso` | Bakeoff preauth container | Physically separate `voice-bakeoff-preauth` Firestore |

Project-container nodes are neutral. They do not inherit the role of one child
resource. Authority uniqueness is enforced per canonical resource role, so a
project, Firestore database, and RTDB instance can all be truthfully observed
without violating a one-authority-per-role invariant.

The staging Cloud Run service in `kevin-491315` and staging data in
`kevin-staging-491315` remain a recorded split control/data topology. That is
separate environment debt, not a reason to duplicate a bakeoff project and not
work Phase 0.5 may remediate.

## Why v2 exists

The exact-hash panel accepted v1 only as freeze evidence and rejected it as a
future connected-read contract. v1 is preserved byte-for-byte. v2 corrects the
review findings:

- an explicit four-state bootstrap removes the project-number/ancestry
  deadlock;
- the JSON Schema pins the complete method dictionary, request bodies, paths,
  field masks, evidence classes, pagination, service names, asset types, and
  caps;
- billing, project provenance, Firestore metadata, Cloud Run placement,
  resource and ancestor IAM, deny policies, service accounts, role resolution,
  principal access boundary policies, organization policies, effective access,
  KMS/RTDB/secret metadata, and audit-sink metadata are represented in one
  closed coverage matrix;
- every connected request requires its own consumed owner authorization and
  explicit acknowledgment of audit-log and quota effects;
- raw ancestry, billing, policy, identity, resource, KMS, secret, and log
  metadata requires private custody;
- a payload-safe receipt cannot be `pass` while coverage, pagination,
  list-to-get closure, role resolution, custody, caps, freshness, or
  completeness errors remain unresolved;
- governance remains `undecided`, while `retain_frozen` is only the safe default
  disposition;
- exact-artifact review remains `pending` inside the candidate and must be
  recorded externally.

## Status precedence

The operator must display the highest-precedence state:

1. `execution_not_authorized`
2. `mutation_not_authorized`
3. connected read `not_authorized`
4. `pending_exact_artifact_review`
5. governance `undecided`
6. inventory `incomplete`

A future inventory receipt marked `pass` can change only inventory evidence.
It cannot authorize a mutation, bootstrap, runtime, provider request, or Task
4.8. Advisory review can close review findings but cannot grant the owner’s
connected-read authority.

## Four-state bootstrap

The current state is always `sealed`; it permits no connected method.

A future connected session would require a separately reviewed and signed
single-request envelope for each transition:

1. `project_identity_only`: one exact `projects.get` binds the immutable project
   number and immediate-parent digest, consumes its authorization, and returns
   to sealed.
2. `ancestry_only`: a new envelope binds the exact project ID, number, immediate
   parent, and one exact ancestry request. It consumes the authorization and
   returns to sealed.
3. `bound_single_read`: only after project number and ancestry are bound may a
   new envelope name one exact method and request digest. One response produces
   one payload-safe request receipt; then the envelope is consumed and the
   state returns to sealed.

There is no automatic next call, fallback identity, fallback project, API
enablement, retry, concurrency, impersonation, or credential export.

## One-screen operator examples

Current source-only state:

```text
OPERATION              source_only_review
PROJECT ID             hk-voice-bakeoff-0724-iso
PROJECT NUMBER         UNBOUND — NO CONNECTED CALL AUTHORIZED
ANCESTRY               UNBOUND
IDENTITY / CONFIG      UNBOUND / UNBOUND
QUOTA PROJECT          UNBOUND
CONNECTED READ         not_authorized
MUTATION               mutation_not_authorized
EXECUTION              execution_not_authorized
STATUS                 CONNECTED EXECUTION SEALED — READ-ONLY RECONCILIATION NOT AUTHORIZED
```

Illustrative first-bind state after a future separate owner authorization:

```text
OPERATION              read_only_environment_reconciliation
PROJECT ID             <exact allowlisted ID>
PROJECT NUMBER         UNBOUND — PROJECT GET ONLY
ANCESTRY               UNBOUND — ANCESTRY CALL NOT YET AUTHORIZED
IDENTITY / CONFIG      <bound digests>
QUOTA PROJECT          <same exact target project>
METHOD                 project_identity_get
CONNECTED READ         read_only_reconciliation_only
MUTATION               mutation_not_authorized
EXECUTION              execution_not_authorized
STATUS                 ONE AUTHORIZED READ — PROJECT IDENTITY ONLY — EXECUTION SEALED
```

Blocked state:

```text
PROJECT NUMBER         MISMATCH_OR_STALE
ANCESTRY               INCOMPLETE
CONNECTED READ         not_authorized
RECOVERY               NO FALLBACK; NEW REVIEWED SINGLE-REQUEST PACKAGE REQUIRED
STATUS                 INCOMPLETE — NO FALLBACK — EXECUTION SEALED
```

## Exact read surface

The v2 schema fixes 31 method contracts. A method cannot be added, removed, or
changed without failing schema validation. Credential/token generation,
mutation methods, arbitrary POSTs, field-mask widening, an extra response
field, and an extra project all fail the offline guards.

The asset workflow is deliberately two-stage:

- an exact project-scoped search returns asset types only, allowing an
  unsupported relevant type to force `incomplete`;
- detailed metadata is then limited to the exact reviewed asset-type allowlist.

Cloud Run service reads exclude template, environment-variable, secret,
service-account, image, traffic-URI, and build configuration fields. Firestore
reads cover database metadata only and prohibit document reads. The RTDB
Management API lists and gets instance metadata while excluding database URLs
and prohibiting record reads. Secret reads cover
metadata only and prohibit payload access. Log coverage reads sink metadata
only and prohibits log entries.

All restricted raw output remains outside Git and chat. A checked-in receipt may
contain only opaque identity/configuration/custody digests, exact project
ID/number, coverage and policy digests, booleans, counts, completeness errors,
and freshness timestamps.

## Behavioral fail-closed cases

The machine-readable candidate binds ten tabletop cases to outcomes. Wrong
account/config/project/number, multiple-account visibility, stale evidence,
403 or partial visibility, missing ancestry, ambient quota, unexpected fields
or exceeded caps, and unbound custody all:

- yield `inventory_status: incomplete`;
- prohibit fallback;
- prohibit a duplicate-project recommendation;
- abort before another request or consume the one-use authorization if the
  request already started.

Multiple-account visibility is recorded as separate visibility claims only
after the owner binds the exact project number and ancestry. It never creates a
second registry project.

## What can happen next

Do not authorize v2 itself. First:

1. complete exact-hash staff, security, and operator-experience review of v2;
2. keep the external review receipt separate from the candidate;
3. bind private evidence custody plus the owner’s account-recovery and billing
   custody attestation;
4. generate a versioned, owner-signable package for exactly one read request,
   containing the explicit local identity/configuration digests, exact target,
   quota project, method, request digest, caps, expiry, and audit/quota
   acknowledgment;
5. review that one-request package again before the owner decides whether to
   sign it.

After every required single-read receipt is complete, freeze a current registry.
Only that frozen registry may feed versioned successor bootstrap/runtime
packages. If any evidence is incomplete, retain the projects frozen and do not
generate an owner-signable bootstrap/runtime successor.

Task 4.8 remains sealed regardless of a future inventory result.
