# Gemini Caller-Turn Qualification Gate 0B

Status: Implementation-only; provider execution not approved

This runbook covers the offline Gate 0B implementation and preregistration
mechanism. It does not authorize a Gemini request, credential creation, corpus
collection, holdout access, staging, production, deployment, release, model
migration, or live-pipeline wiring.

The checked-in runner has no network connector and no credential default. Its CLI
can emit a non-executable template or a canonical preregistration assembled from
reviewed external identity values. The `--execute` path is intentionally blocked.

## Fixed Boundary

Gate 0B evaluates retrospective caller-turn assembly for purpose-recorded,
consented qualification audio. It does not evaluate real calls, semantic intake,
controller mutation, caller-experience improvement, accessibility, or broad
multilingual product readiness.

The implementation pins:

- model `models/gemini-3.1-flash-live-preview`;
- API version `v1beta` and the official Gemini Bidi WebSocket endpoint;
- WebSocket over TLS with no proxy, redirect, debug, crash-dump, or TLS-key-log
  path;
- candidate quiescence policies `100`, `250`, `500`, and `750` milliseconds;
- Python `3.12.13`, uv `0.11.7`, and the checked-in `uv.lock`;
- exactly 256 eligible activities, 128 sealed holdout activities, and 64
  separately scheduled no-speech windows;
- the request, duration, timeout, attempt, and cost ceilings in the approved plan.

`app/services/gemini_pipeline.py` and `app/services/voice_pipeline.py` must not
import or call any Gate 0B module.

## Offline Verification

Run from a clean worktree at the reviewed implementation commit:

```bash
uv lock --check
uv run --locked --no-sync --extra dev --python 3.12.13 \
  python -m pytest tests/unit/test_run_gemini_caller_turn_qualification.py -q
uv run --locked --no-sync --extra dev --python 3.12.13 \
  ruff check scripts/run_gemini_caller_turn_qualification.py \
  tests/unit/test_run_gemini_caller_turn_qualification.py
uv run --locked --no-sync --extra dev --python 3.12.13 \
  bandit -q -lll scripts/run_gemini_caller_turn_qualification.py
```

These commands perform no DNS lookup, socket connection, provider request, secret
lookup, or corpus access.

## Dry-Run Template

All preregistration and evidence artifacts live outside the repository. Prepare an
access-restricted root owned by the qualification operator:

```bash
sudo install -d -m 0700 /var/lib/hey-kevin-qualification/
sudo install -d -m 0700 /var/lib/hey-kevin-qualification/preregistration/
sudo install -d -m 0700 /var/lib/hey-kevin-qualification/evidence/
sudo install -d -m 0700 /var/lib/hey-kevin-qualification/capsules/
sudo install -d -m 0700 /var/lib/hey-kevin-qualification/ledger/
```

Emit the implementation-only template:

```bash
uv run --locked --no-sync --extra dev --python 3.12.13 \
  python scripts/run_gemini_caller_turn_qualification.py \
  --dry-run \
  --output /var/lib/hey-kevin-qualification/preregistration/gate0b-template.json
```

The file is created exclusively with mode `0600`. Existing files, symlinks,
relative paths, repository-local paths, and unavailable parent directories are
rejected. The template leaves project, credential reference, source identity,
digests, custody locations, and attestations unset. It is not executable and is
not an approval artifact.

## Canonical Preregistration

Do this only after the implementation PR is merged and an exact clean detached
worktree exists at the merged commit. The external values document uses schema
`gate_0b_preregistration_values_v1` and must contain exactly:

```text
project
credential_reference
approval_key_id
approval_public_key_sha256
source_sha
environment_identity_sha256
manifest_sha256
corpus_sha256
setup_sha256
pricing_sha256
runner_sha256
evaluator_sha256
ledger_location_sha256
audit_capsule_location_sha256
evidence_location_sha256
consent_attestation_sha256
retention_attestation_sha256
zdr_or_residual_retention_acceptance_sha256
```

The credential reference is an opaque identifier for the later approved secret
delivery path. The values file must never contain a credential, authenticated URL,
token, private key, real phone number, participant identity, transcript, audio,
provider request ID, provider session ID, or local asset path.

Generate the canonical artifact outside the repository:

```bash
uv run --locked --no-sync --extra dev --python 3.12.13 \
  python scripts/run_gemini_caller_turn_qualification.py \
  --dry-run \
  --values /var/lib/hey-kevin-qualification/preregistration/gate0b-values.json \
  --output /var/lib/hey-kevin-qualification/preregistration/gate0b-preregistration.json
```

The builder rejects missing or unknown fields, a production project, the live Hey
Kevin project, malformed identities, and noncanonical digests. It adds
`preregistration_sha256`, computed over the complete artifact before that digest
field is attached.

The values themselves still require independent source, environment, corpus,
consent, retention, pricing, setup, custody-location, and trust-root verification.
The builder validates shape and binding; it does not prove those external facts.

## Approval Sequence

The canonical preregistration still does not authorize provider execution.

1. Review the exact merged source commit and clean-tree identity.
2. Validate the external corpus, consent, rights, withdrawal, retention, paid
   non-production project, and holdout-custody attestations.
3. Review the exact model, endpoint, transport, project label, credential reference,
   trust root, digests, locations, counts, timeouts, request limits, and cost limits.
4. Run staff, security/privacy, caller-experience, and QA review on the exact commit
   and canonical preregistration digest.
5. Ask the user for a separate explicit approval quoting the complete digest and
   bounded values.
6. Only an independent custodian may then issue a short-lived signed campaign
   approval and one signed attempt authorization. The signing private key is never
   available to the executor.

The current CLI has no provider connector and keeps `--execute` blocked even when a
canonical preregistration exists. A future execution ceremony requires the separate
Task 9 approval boundary and exact merged-source review. Merge, provider execution,
runtime wiring, staging, deployment, production, and release remain separate
decisions.

## Attempt And Evidence Handling

An approved attempt must atomically consume its one-use authorization in the
external hash-chained ledger before source revalidation, credential lookup, provider
DNS, or connector construction. Each provider request consumes reserved allowance
immediately before connector construction. There are no case, session, setup,
provider, malformed-message, timeout, or gate retries.

Raw provider messages are reduced independently twice and then discarded. Output
audio is counted and discarded. Canonical references and transcript fragments may
exist only inside the allowlisted encrypted audit capsule. The storage sink must
return a digest-matched handoff receipt before an attempt can be marked complete.

Primitive records and published reports contain no text, audio, prompt, tool
arguments, credential, path, subject identifier, caller identifier, provider ID, or
phone number. The evidence custodian deletes the capsule and key after independent
recomputation and records the deletion attestation.

## Nonauthorization Flags

Every preregistration starts with every evidence and authorization flag false,
including:

```text
future_execution_authorized false
model_migration_authorized false
runtime_wiring_authorized false
controller_work_authorized false
staging_authorized false
deployment_authorized false
production_authorized false
release_authorized false
```

Only the independent evaluator may set sample-evidence fields from complete,
verified records. It cannot set any future-execution, model-migration, runtime,
staging, deployment, production, or release authorization.

## Stop Conditions

Stop before any credential lookup or provider request if any approved value,
signature, source byte, dependency, import origin, lockfile, interpreter, CA bundle,
clock, codec digest, corpus asset, consent record, retention setting, custody path,
request reservation, cost reservation, or ledger record is missing or mismatched.

Also stop if the model or official API behavior changes, the transport can expose a
credential, either reducer disagrees, a payload escapes the encrypted capsule, a
holdout asset is touched before policy lock, or a live-pipeline import/diff appears.

## References

- `docs/superpowers/plans/2026-07-15-gemini-caller-turn-qualification-gate-0b.md`
- `docs/adr/0001-gemini-retrospective-caller-turns.md`
- `docs/gemini-caller-turn-assembly-qualification.md`
