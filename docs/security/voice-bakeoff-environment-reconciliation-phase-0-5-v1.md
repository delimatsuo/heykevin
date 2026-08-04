# Voice Bakeoff Environment Reconciliation — Phase 0.5

**Status:** source-only draft; connected inventory, mutation, and execution are
not authorized.

**Source SHA:** `2ed8ea7d1d7f338e84ddf08d5a50a714835e1533`.

**Canonical machine-readable package:**
`docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v1.json`.

**Schema:**
`docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v1.schema.json`.

This package is not an owner-authorization record. It does not authorize a
Google Cloud query, Cloud Audit Log event, IAM or resource mutation, API
enablement, credential, workload, provider/PSTN access, staging, production, or
Task 4.8. The dominant status remains:

```text
CONNECTED EXECUTION SEALED — READ-ONLY RECONCILIATION NOT AUTHORIZED
```

## Decision and recommendation

Do not create another project and do not move, delete, or modify an existing
project.

The screenshots show project *visibility* from different Google identities.
That is not evidence that a project was duplicated. Google Cloud projects are
identified by an exact project ID and immutable project number. Display names,
the account currently signed into the console, recent-project lists, and
organization grouping are not valid resource selectors.

The source currently describes four distinct project roles:

| Exact project ID | Source-declared role | Phase 0.5 disposition |
| --- | --- | --- |
| `kevin-491315` | Production data and the Cloud Run control plane for both `kevin-api` and `kevin-api-staging` | Retain frozen |
| `kevin-staging-491315` | Staging Firestore and RTDB data plane | Retain frozen |
| `hk-voice-bakeoff-0724-iso` | Isolated execution-control security domain | Retain frozen |
| `hk-voice-bakeoff-preauth-iso` | Physically separate isolated preauth security domain | Retain frozen |

This is a resource graph, not a one-project-equals-one-environment table.
Specifically, the source-declared staging runtime and staging data plane span
two projects: `kevin-api-staging` is in `kevin-491315`, while its Firestore and
RTDB targets are in `kevin-staging-491315`. That mixed staging control plane is
environment debt. Phase 0.5 records it; it does not repair it.

The recommended sequence is:

1. Freeze the four source-declared project IDs and make no changes.
2. Separately review and owner-authorize one bounded read-only reconciliation
   of those exact four IDs using the manifest in the canonical package.
3. Bind each exact project ID to its immutable project number, ancestry,
   explicit local identity/configuration, self-quota target, resource inventory,
   and freshness window.
4. Mark missing, denied, stale, ambiguous, or partial visibility as
   `incomplete`; never infer that a resource or grant is absent.
5. Freeze a current registry only after the read-only evidence is complete and
   payload-safe.
6. Produce versioned successor bootstrap/runtime authorization packages bound
   to that frozen registry and a newly reviewed exact source SHA.

If the inventory later proves that a move or replacement is needed, use a
separate transition authorization, perform the transition, repeat the
inventory, freeze the new registry, and only then prepare successor
authorization packages. Project creation remains capped at zero until such a
state machine is separately reviewed and approved.

## Why the existing packages are not being edited

The existing bootstrap and cross-environment packages are historical,
untracked artifacts. They predate this environment-identity clarification.
Editing them in place would erase the evidence trail and could make an older
approval appear to cover a materially different topology.

Phase 0.5 therefore records their byte digests and requires any future
correction to be a versioned successor. It does not stage, rewrite, or treat
those artifacts as authority.

## Registry semantics

The registry separates facts that the console UI visually combines:

- **Google identity visibility:** which explicit local operator identity can
  read a project;
- **GCP project identity:** exact project ID plus immutable project number;
- **resource placement:** the project that contains a Cloud Run service,
  Firestore database, or RTDB instance;
- **environment role:** production, staging, bakeoff control, or bakeoff
  preauth;
- **security domain:** the trust boundary that owns the resource;
- **governance state:** retain, move, or replace decision;
- **resource authority state:** candidate, authoritative, retiring, or retired;
- **connected authority:** whether a read, mutation, or execution is authorized.

Every source-declared resource remains `candidate` and `incomplete` because this
source-only phase did not query Google Cloud. Zero authoritative resources is
valid while evidence is incomplete. More than one authoritative resource for a
security domain is always invalid.

The checked-in identity aliases intentionally contain no email address,
billing-account ID, credential, access token, or local configuration name.
Those bindings belong in separately custodied owner authorization, not Git.

## Future operator targeting banner

Before every future connected read, the operator must display and verify one
screen containing:

- operation class and exact source SHA;
- explicit local configuration reference and identity alias;
- exact target project ID and expected immutable project number;
- observed ancestry digest and evidence freshness;
- exact self-quota project;
- exact API method and response field mask;
- inventory, governance, mutation, and execution status;
- the sealed banner literal.

The first call for each project may only bind the immutable project number for
one already allowlisted exact project ID. No other call is allowed until that
number and the ancestry binding are exact. A mismatch or ambiguity terminates
the session; it does not trigger discovery, fallback, retry, API enablement,
impersonation, or a credential change.

## Read-manifest boundaries

The machine-readable manifest is descriptive and non-executable. It admits only
the four exact project IDs and fixes the permitted future read methods, request
bodies, response field masks, pagination requirement, quota target, and receipt
projection.

It explicitly forbids:

- ambient project, quota, identity, or credential selection;
- Application Default Credential discovery or service-account impersonation;
- credential or access-token export;
- API enablement, fallback discovery, automatic retries, or concurrency;
- request parameter expansion;
- raw evidence in Git or chat;
- every mutation, workload, provider/PSTN, staging, production, or Task 4.8
  action.

Project ancestry, resource names, IAM members, conditional expressions, deny
rules, service-account identities, billing metadata, and KMS names are
restricted raw evidence. A future connected package needs a separately bound
private custody mechanism before the first query. The checked-in receipt may
contain only counts, booleans, digests, completeness errors, freshness,
timestamps, and the exact project ID/number binding.

## Review result

The staff architecture, security, and operator-experience panel reached
consensus on this direction:

- source-only Phase 0.5 is appropriate;
- a flat project/environment model is unsafe;
- a new project is not justified;
- current projects remain frozen by default;
- connected inventory remains a separate owner-authorized step;
- advisory review does not substitute for owner authorization;
- bootstrap/runtime successors must follow, not precede, a current frozen
  registry.

Task 4.8 remains sealed regardless of whether a future read-only reconciliation
passes.
