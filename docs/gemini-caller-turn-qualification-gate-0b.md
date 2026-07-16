# Gemini Caller-Turn Qualification Gate 0B

Status: Implementation-only; provider execution not approved

This runbook covers the offline Gate 0B implementation and preregistration
mechanism. It does not authorize a Gemini request, credential creation, corpus
collection, holdout access, staging, production, deployment, release, model
migration, or live-pipeline wiring.

The checked-in runner has only an injected connector protocol and no concrete
network connector, credential loader, custody service, custody storage backend, or
credential default. Its CLI can emit a non-executable template or a canonical
preregistration assembled from reviewed external identity values. The `--execute`
path is intentionally blocked. The checked-in approval root contains exactly
`UNPROVISIONED`, so campaign approval verification cannot succeed.

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
- exactly 24 logical sessions, eight fresh-connection restarts, and 32 no-speech
  requests per split, for 64 provider requests per split and 128 per attempt;
- an exact shared allocation validator covering every language, condition, stress,
  code-switch direction, applicable critical span, and the 16 silence plus 16
  background-noise windows in each split;
- the request, duration, timeout, attempt, and cost ceilings in the approved plan.

`app/services/gemini_pipeline.py` and `app/services/voice_pipeline.py` must not
import or call any Gate 0B module.

## Offline Verification

Run from a clean worktree at the reviewed implementation commit:

```bash
uv lock --check
uv run --locked --no-sync --extra dev --python 3.12.13 \
  python -m pytest \
  tests/unit/test_qualification_identity.py \
  tests/unit/test_qualification_ledger.py \
  tests/unit/test_qualification_allocation.py \
  tests/unit/test_qualification_privacy.py \
  tests/unit/test_qualification_private_paths.py \
  tests/unit/test_caller_turn_measurement.py \
  tests/unit/test_run_gemini_caller_turn_qualification.py \
  tests/unit/test_evaluate_gemini_caller_turn_qualification.py \
  tests/unit/test_gate0b_offline_boundaries.py -q
uv run --locked --no-sync --extra dev --python 3.12.13 \
  ruff check app/services/qualification_identity.py \
  app/services/qualification_ledger.py \
  app/services/caller_turn_measurement.py \
  scripts/run_gemini_caller_turn_qualification.py \
  scripts/evaluate_gemini_caller_turn_qualification.py \
  tests/unit/test_qualification_identity.py \
  tests/unit/test_qualification_ledger.py \
  tests/unit/test_caller_turn_measurement.py \
  tests/unit/test_run_gemini_caller_turn_qualification.py \
  tests/unit/test_evaluate_gemini_caller_turn_qualification.py
uv run --locked --no-sync --extra dev --python 3.12.13 \
  bandit -q -lll app/services/qualification_identity.py \
  app/services/qualification_ledger.py \
  app/services/caller_turn_measurement.py \
  scripts/run_gemini_caller_turn_qualification.py \
  scripts/evaluate_gemini_caller_turn_qualification.py
```

These commands perform no DNS lookup, socket connection, provider request, secret
lookup, or corpus access.

## Dry-Run Template

All preregistration and evidence artifacts live outside the repository. Prepare an
access-restricted root as the qualification operator. Do not run this block with
`sudo`; the final directories must be owned by the account that runs qualification:

```bash
QUALIFICATION_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/hey-kevin-qualification"
install -d -m 0700 \
  "$QUALIFICATION_ROOT" \
  "$QUALIFICATION_ROOT/preregistration" \
  "$QUALIFICATION_ROOT/evidence" \
  "$QUALIFICATION_ROOT/capsules" \
  "$QUALIFICATION_ROOT/ledger"
```

Emit the implementation-only template:

```bash
QUALIFICATION_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/hey-kevin-qualification"
uv run --locked --no-sync --extra dev --python 3.12.13 \
  python scripts/run_gemini_caller_turn_qualification.py \
  --dry-run \
  --output "$QUALIFICATION_ROOT/preregistration/gate0b-template.json"
```

The file is created exclusively with mode `0600`. Existing files, symlinks,
relative paths, repository-local paths, and unavailable parent directories are
rejected. The template leaves project, credential reference, source identity,
digests, custody locations, and attestations unset. It is not executable and is
not an approval artifact.

## Canonical Preregistration

Do this only after the implementation PR is merged and an exact clean detached
worktree exists at the merged commit. The external values document uses schema
`gate_0b_preregistration_values_v2` and must contain exactly:

```text
project
project_number
credential_reference
credential_key_resource_sha256
credential_restrictions_sha256
provider_quota_sha256
credential_activated_at
credential_expires_at
credential_revocation_required_by
credential_revocation_policy_sha256
approval_key_id
approval_public_key_sha256
custodian_key_id
custodian_public_key_sha256
privacy_custodian_key_id
privacy_custodian_public_key_sha256
record_root_key_id
record_root_public_key_sha256
ledger_instance_id
ledger_custodian_key_id
ledger_custodian_public_key_sha256
source_sha
source_fact_bundle_sha256
environment_identity_sha256
manifest_sha256
corpus_sha256
development_schedule_sha256
setup_sha256
pricing_sha256
runner_sha256
evaluator_sha256
ledger_location_sha256
audit_capsule_location_sha256
holdout_capsule_location_sha256
evidence_location_sha256
consent_attestation_sha256
retention_attestation_sha256
zdr_or_residual_retention_acceptance_sha256
```

The project number and digest-bound key resource, API restrictions, quota,
activation, expiry, and revocation controls are externally reviewed Task 9/10
values; the implementation-only template leaves them unset. The credential
reference is an opaque identifier for the later approved secret delivery path. The
values file must never contain a credential, authenticated URL,
token, private key, real phone number, participant identity, transcript, audio,
provider request ID, provider session ID, or local asset path.

Generate the canonical artifact outside the repository:

```bash
QUALIFICATION_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/hey-kevin-qualification"
uv run --locked --no-sync --extra dev --python 3.12.13 \
  python scripts/run_gemini_caller_turn_qualification.py \
  --dry-run \
  --values "$QUALIFICATION_ROOT/preregistration/gate0b-values.json" \
  --output "$QUALIFICATION_ROOT/preregistration/gate0b-preregistration.json"
```

The builder rejects missing or unknown fields, a production project, the live Hey
Kevin project, malformed identities, noncanonical digests, reused key IDs, and
reused public-key digests. It adds
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

The executor depends only on `LedgerCustodyClient`, an IPC protocol owned by a
separate durable custodian. There is no in-process or file-backed ledger authority
in the application. The custodian identity binds a random ledger instance ID, an
Ed25519 key ID and public-key digest, and an external ledger-location digest into
preregistration and campaign approval. Every exported receipt is signature-checked,
strictly sequenced, hash-chained, and replayed from genesis.

An approved attempt must atomically consume its one-use authorization in that
external ledger before source revalidation, credential lookup, provider DNS, or
connector construction. Before that claim, its authorization must reserve the exact
preregistered per-run liability of 128 requests and USD 10. Every post-mutation
snapshot must contain the previously accepted signed record sequence plus exactly
one expected event; an independently valid alternate history is not continuity. The
claim signs only the SHA-256 of its opaque lease capability, and the executor matches
that digest before credential lookup and again at holdout resume. One active attempt
spans development checkpoint, policy
lock, holdout release, holdout execution, and terminal outcome. The custodian must
append a signed one-shot `holdout_execution_claim` before the first holdout provider
request; a missing or duplicate claim fails closed. A crash after that claim cannot
resume or replace the holdout. Each provider request consumes reserved allowance
immediately before connector construction. There are no case, session, setup,
provider, malformed-message, timeout, or gate retries.

Corpus and schedule bytes are not executor arguments. A separate opaque loader may
release one split only after a fresh Ed25519-signed privacy-custody receipt verifies
the campaign, attempt, split, source, preregistration, schedule, corpus, project,
model, consent and withdrawal status, purpose, rights, provider disclosure,
retention decision, deletion deadline, and residual-retention acceptance. A stale,
withdrawn, mismatched, or alternate-fork receipt cannot invoke the loader. Holdout
verification additionally requires the signed post-lock ledger state and exact
holdout schedule commitment before the one-shot holdout claim.

Raw provider messages are reduced independently twice and then discarded. Output
audio is counted and discarded. Canonical references and transcript fragments may
exist only inside the allowlisted encrypted audit capsule. Capsule schema v6 stores
adapted transcript events and ordered raw wire facts once per logical session;
activity entries contain metadata and references only. The evaluator independently
derives timing, premature output, response gaps, terminal ordering, causal tool
cancellation/interruption, close, malformed-message, runaway-output, and teardown
facts from that session evidence. The storage sink must return a digest-matched
handoff receipt before an attempt can be marked complete.

Critical spans qualify only when one unique token sequence (or character sequence
for unsegmented languages) is preserved by an optimal reference-to-hypothesis
alignment. Moved or duplicated spans fail, and every scenario-specific required span
kind must be present. Contamination includes a foreign token absent from the current
reference even when several foreign activities share it. Interruption tail includes
all causally prior response audio emitted after the new caller-audio trigger,
including audio that continues after cancellation is observed.

Each sealed capsule retains the complete payload-safe runtime identity report before
and after its provider split, together with both report hashes. The evaluator binds
those reports to preregistration, rejects intra-split or cross-split drift, and
publishes the campaign's complete before/after reports and hashes in the final report.
Repository-relative dependency names and executable locations appear only as
SHA-256 identifiers; the report contains no cleartext file path.

Every external values file, capsule, custody bundle, key, and report uses an
absolute path outside the repository. Input files must be current-user-owned,
single-link regular files with mode `0600`; output parents must be
current-user-owned directories with mode `0700`; symlink ancestry is rejected.

Request counts, modality token totals, input duration, output bytes, observed
elapsed time, completion state, and bounded failure enums are also stored only in
strict capsule accounting records. The custody bundle cannot supply top-level usage
or failure summaries. The evaluator derives both splits' accounting from opened
capsules, recomputes cost, and checks the development and combined usage digests,
actual totals, and signed reservation before producing a report.
The evaluator also requires both the authorization and replay-derived reservation to
equal the exact preregistered per-run liability; lower signed values are invalid even
when they cover actual usage.

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
holdout asset is touched before policy lock, the external custodian cannot provide
an atomic signed append/anti-replay registry, or a live-pipeline import/diff appears.

## References

- `docs/superpowers/plans/2026-07-15-gemini-caller-turn-qualification-gate-0b.md`
- `docs/adr/0001-gemini-retrospective-caller-turns.md`
- `docs/gemini-caller-turn-assembly-qualification.md`
