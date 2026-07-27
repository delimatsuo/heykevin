# Voice Bakeoff Sealed Control Composition and IAM Packet

**Status:** apply-prohibited, source/reference-only preparation.

**Source baseline:** `ea470ab` (`feat: add isolated Firestore transaction runner`).

This packet does not authorize a workload, service account, IAM change,
Firestore write, credential delivery, provider/PSTN request, deployment,
production or staging access, retention locking, or Task 4.8. It prepares
only the control-domain portion of a future external-gate review without
creating dormant authority.

The current gate remains `execution_status: not_authorized`. Its sealed owner
authorization, independent technical review, credential broker, durable live
trust/revocation implementation, complete production denylist, immutable
custody, one-use execution envelope, and provider privacy attestations remain
unmet.

## Sealed control composition contract

### One future control-domain boundary

The only permitted future construction site for the **control** component is
an isolated nonproduction module, proposed as:

```text
app/experiments/voice_bakeoff_control_composition.py
```

That file does not exist today and must remain absent from `app.main`, normal
API routers, webhooks, production/staging deployment configuration, candidate
adapters, and the shipped dry-run runner. It cannot mount a route or be reached
by dependency injection, environment selection, plugin discovery, dynamic
import, task queue, or test-only configuration.

Any later implementation must be an exact-SHA, separately reviewed change. It
may own one isolated control workload identity and construct exactly one
execution-control Firestore client through the injected
`GoogleFirestoreClientHandle`. It must not expose the raw SDK client, use a
default project/database, create a provider/Twilio/evidence client, resolve a
credential, or construct the physically separate pre-auth store.

### Required control input bundle

The future control boundary accepts only injected, validated values:

| Input | Required binding |
| --- | --- |
| Sealed approval envelope | canonical encoding, self-digest, owner signature, no unresolved P1, unexpired and unused |
| Trust/revocation state | current generation, root/key provenance, durable revocation proof |
| Source bundle | exact source SHA, clean-worktree result, configuration/dependency/runner/harness digest set |
| Control store target | immutable project/database/root attestation bound to the dedicated control identity |
| Firewall policy | current production-denylist and credential-broker policy digests |
| Stop/rollback plan | idempotent revoke, drain, residue, IAM removal, and identity-teardown procedures |

No default project, default Firestore database, unpinned endpoint, ambient
environment setting, ADC discovery, caller input, or production/staging
configuration can supply any field in this bundle.

### Fail-closed control sequence

1. Reject all invocation before an exact sealed control input bundle is
   present.
2. Locally verify envelope syntax, self-digest, owner/trust provenance,
   independent-review status, source/configuration/dependency bindings, current
   firewall policy, caps, expiry, and nonproduction target attestation.
3. Verify that the proposed control principal has no production, staging,
   cross-store, key, impersonation, or broader equivalent grant in current IAM
   evidence.
4. Construct only the injected, attested execution-control client and
   atomically reserve the nonce/approval/binding through the control store.
5. Return only a signed, digest-bound admission result to a future separate
   pre-auth boundary; do not resolve a pre-auth token, construct a pre-auth
   client, or activate any external dependency.

The control-store workload identity in step 4 is not a provider credential.
Its only permitted exception is atomic control-store access after all local
sealed-authority checks in step 2. No provider, PSTN, evidence, or pre-auth
token credential may resolve before the active control record exists.

There is no pre-auth composition, pre-auth SDK runner, pre-auth identity, or
pre-auth IAM proposal in this packet. A separately reviewed pre-auth boundary
must later consume the digest-only control result using a different project,
database, identity, construction site, and transaction runner. Until that
boundary exists and passes its own proof, the full execution path fails closed.

Any error or ambiguous result before an active record fails closed. Any error
after nonce consumption preserves the consumed nonce, revokes the control
record, drains work, and emits only payload-safe receipts.

### Mandatory composition tests

Before the control IAM packet may be applied, the exact implementation must
prove:

- static route/import/text enumeration shows no mount or reference from normal
  backend, webhook, candidate, dry-run, deployment, CI, production, or staging
  paths;
- the control constructor rejects absent, stale, mismatched, expired, revoked,
  template-only, or already-consumed authorization before creating a client or
  resolving any credential;
- it rejects default/mismatched project/database, production/staging targets,
  missing source/configuration digests, and absent production-denial evidence;
- it cannot construct provider/PSTN/evidence clients before durable control
  reservation and cannot resolve provider credentials before active control;
- stop, expiry, unavailable, conflict, and unknown transaction outcomes leave
  no mount, route, provider request, active usable capability, or partially
  released credential;
- a post-rollback readback proves no active control record and no remaining
  control identity binding/reference.

Current tests intentionally prove the inverse: the transaction port and Google
runner are unmounted from executable and deployment paths.

## Control-domain IAM packet — DO NOT APPLY

The following names and commands are a reviewed proposal only. They create no
reservation and must not be run before the sealed control composition contract
and external gate are complete.

| Domain | Proposed service account | Project/database | Proposed custom role and condition |
| --- | --- | --- | --- |
| Control | `voice-bakeoff-control-adapter@hk-voice-bakeoff-0724-iso.iam.gserviceaccount.com` | `hk-voice-bakeoff-0724-iso` / `voice-bakeoff-control` | `projects/hk-voice-bakeoff-0724-iso/roles/voiceBakeoffControlTransaction`, conditionally limited to the exact named control database |

`roles/datastore.user` is prohibited for this packet because it is broader than
the control transaction operations. The proposed custom role contains only:

```text
datastore.databases.get
datastore.entities.get
datastore.entities.create
datastore.entities.update
```

The exact runner must first prove that these are the only Firestore permissions
required for begin/rollback, read, exists-false create, and update-time-fenced
replacement. The future operator must also confirm that all four permissions
are supported for a project custom role before creating its permanent role ID:

```bash
# DO NOT RUN: future gated preflight only.
gcloud iam list-testable-permissions \
  //cloudresourcemanager.googleapis.com/projects/hk-voice-bakeoff-0724-iso \
  --filter='customRolesSupportLevel!=NOT_SUPPORTED' \
  --format='value(name)'
```

IAM conditions restrict the named database; they do not constrain document
roots. The adapter's closed path mapper remains the required root-path control.

### Proposed condition

The candidate condition intentionally combines a Firestore database resource
type and exact named database:

```text
expression=resource.type=="firestore.googleapis.com" && resource.name=="projects/hk-voice-bakeoff-0724-iso/databases/voice-bakeoff-control"
title=voiceBakeoffControlDatabaseOnly
description=Task4_8_control_database_only
```

This condition is reference-only, not a claim of effective access. Before any
apply, Policy Troubleshooter and an isolated synthetic control workload must
both prove that it grants the four listed permissions only on the named control
database and denies every out-of-scope target.

No user-managed keys, service-account impersonation grants, cross-project
grants, workload-identity-federation binding, Cloud Run binding, default
compute binding, build binding, service-agent binding, provider credential,
PSTN credential, or secret are part of this packet.

### Apply sequence — prohibited until the sealed gate

The exact source/configuration digest, independent review, sealed owner
authorization, composition proof, production-denial inventory, and rollback
operator must all be current before this sequence can be considered. IAM
changes can take time to propagate; the later execution plan must wait through
the bounded propagation window and verify effective access before an emulator or
any workload request.

```bash
# DO NOT RUN: future gated control creation only.
gcloud iam service-accounts create voice-bakeoff-control-adapter \
  --project=hk-voice-bakeoff-0724-iso \
  --display-name="Hey Kevin Voice Bakeoff control adapter"

gcloud iam roles create voiceBakeoffControlTransaction \
  --project=hk-voice-bakeoff-0724-iso \
  --title="Hey Kevin Voice Bakeoff Control Transaction" \
  --description="Minimum Firestore control transaction permission set" \
  --stage=GA \
  --permissions=datastore.databases.get,datastore.entities.get,datastore.entities.create,datastore.entities.update

gcloud projects add-iam-policy-binding hk-voice-bakeoff-0724-iso \
  --member='serviceAccount:voice-bakeoff-control-adapter@hk-voice-bakeoff-0724-iso.iam.gserviceaccount.com' \
  --role='projects/hk-voice-bakeoff-0724-iso/roles/voiceBakeoffControlTransaction' \
  --condition='expression=resource.type=="firestore.googleapis.com" && resource.name=="projects/hk-voice-bakeoff-0724-iso/databases/voice-bakeoff-control",title=voiceBakeoffControlDatabaseOnly,description=Task4_8_control_database_only'
```

### Required effective-access, readback, and negative proof

The future operator must capture payload-safe evidence outside Git:

```bash
# Read-only future verification.
gcloud projects get-iam-policy hk-voice-bakeoff-0724-iso --format=json

gcloud iam service-accounts keys list \
  --iam-account=voice-bakeoff-control-adapter@hk-voice-bakeoff-0724-iso.iam.gserviceaccount.com \
  --managed-by=user

# One future Policy Troubleshooter query per permitted and prohibited permission.
gcloud beta policy-intelligence troubleshoot-policy iam \
  //firestore.googleapis.com/projects/hk-voice-bakeoff-0724-iso/databases/voice-bakeoff-control \
  --principal-email=voice-bakeoff-control-adapter@hk-voice-bakeoff-0724-iso.iam.gserviceaccount.com \
  --permission=datastore.entities.get
```

An empty `--managed-by=user` listing proves zero **user-managed** service
account keys; it does not claim zero Google-managed keys. Project policy
readback alone does not prove effective access because inherited, deny, and
equivalent grants may apply.

Policy Troubleshooter must show an expected grant for each of the four listed
permissions against the exact control database. It must show no grant for
pre-auth, `kevin-491315`, staging resources, deletion/list/secret/credential
permissions, impersonation, or deploy permissions. Unknown, unavailable, or
ambiguous results block execution.

An isolated synthetic control workload, using only the dedicated identity, must
then prove its intended begin/rollback/read/create/update transaction behavior
and direct requests to pre-auth, production, and staging targets must return
`PERMISSION_DENIED`. It must not resolve provider credentials or make provider
or PSTN requests.

The future review must additionally enumerate default compute, Cloud Run,
build, service-agent, and other principals with relevant project/resource
bindings, then verify that no equivalent access path reaches the control
database or any prohibited resource.

### Rollback sequence — future controlled operation

Rollback first revokes active control records and drains the isolated workload.
Only after a zero-active-record readback may it remove the exact conditional
binding, read back its absence, and disable the newly created identity:

```bash
# DO NOT RUN: future gated control rollback only.
gcloud projects remove-iam-policy-binding hk-voice-bakeoff-0724-iso \
  --member='serviceAccount:voice-bakeoff-control-adapter@hk-voice-bakeoff-0724-iso.iam.gserviceaccount.com' \
  --role='projects/hk-voice-bakeoff-0724-iso/roles/voiceBakeoffControlTransaction' \
  --condition='expression=resource.type=="firestore.googleapis.com" && resource.name=="projects/hk-voice-bakeoff-0724-iso/databases/voice-bakeoff-control",title=voiceBakeoffControlDatabaseOnly,description=Task4_8_control_database_only'

gcloud projects get-iam-policy hk-voice-bakeoff-0724-iso --format=json

gcloud iam service-accounts disable \
  voice-bakeoff-control-adapter@hk-voice-bakeoff-0724-iso.iam.gserviceaccount.com \
  --project=hk-voice-bakeoff-0724-iso

gcloud iam service-accounts describe \
  voice-bakeoff-control-adapter@hk-voice-bakeoff-0724-iso.iam.gserviceaccount.com \
  --project=hk-voice-bakeoff-0724-iso \
  --format=json
```

The post-removal policy must not contain the exact member, role, or condition.
The service-account readback must show `disabled: true`. Disabling or deleting
the custom role, and deleting the service account, are separately controlled
changes after that proof. No retention lock is part of this packet.
