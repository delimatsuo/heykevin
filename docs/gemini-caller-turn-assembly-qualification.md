# Gemini Caller-Turn Assembly Qualification

## Status and Boundary

This runbook covers the offline Gate 0A qualification command only. It does not
authorize a Gemini request, real caller data, live-pipeline wiring, staging,
production, deployment, feature flags, or release.

The checked-in audio manifest is intentionally `collection_status: pending`. It
contains no audio, speakers, transcripts, or consent claims. An execution attempt
must remain blocked until a separately reviewed manifest contains at least 200
purpose-recorded turns from consenting adults and satisfies every provenance and
language-stratum check.

## Artifacts

The command consumes four immutable inputs:

1. A synthetic/consented audio manifest and its SHA-256 digest.
2. An external provider-setup JSON file and its SHA-256 digest.
3. A non-sensitive deviations JSON file and its SHA-256 digest.
4. The expected canonical setup SHA-256 generated from those inputs.

Keep the provider-setup file outside the repository. It may contain a synthetic
system instruction and tool declarations. It must contain no credential, caller
data, contractor data, production prompt, phone number, address, call SID, or real
transcript. The command hashes the instruction and tool declarations before they
enter preregistration or reports.

The setup document must explicitly contain:

- exact API version, official endpoint, and model resource;
- complete generation, transcription, VAD, activity, and turn-coverage settings;
- synthetic prompt-fixture digest and tool-response policy;
- reconnect, context-restoration, retry, quiescence, and WebSocket policies;
- runner, evaluator, and immutable-pipeline source/file identities;
- a payload-free immutable-pipeline setup projection.

No behavior-affecting value has a command default. The deviations file must contain
one digest-backed entry for every leaf that differs between the immutable-pipeline
projection and qualification setup. Missing, extra, duplicate, or stale entries
fail closed.

## Corpus Contract

An execution-ready manifest must declare only synthetic scripts and
purpose-recorded audio from consenting adults. Every speaker requires a bounded
local ID, adult-consent assertion, `qualification_only` rights, and an external
consent-record digest. Do not commit a consent record containing identity data.

Every case requires:

- a bounded local case ID and consented speaker ID;
- confined relative audio path and exact audio SHA-256;
- synthetic script SHA-256, never script text;
- BCP-47 language, codec, condition, scenario, and development/holdout split;
- no real-call, production-audio, biometric, caller-ID, or transcript field.

Execution additionally requires at least 200 cases, at least eight language groups,
and at least 20 cases in each qualifying language group. The manifest must include
clean/noisy telephony conditions, pauses, corrections, numbers, barge-in, tool use,
tool cancellation, reconnect, and both code-switch directions defined by the
reviewed plan.

## Dry Run

First compute the raw file digests without printing file contents:

```bash
shasum -a 256 "$MANIFEST" "$SETUP" "$DEVIATIONS"
```

Generate the canonical setup digest locally. This command prints only the canonical
non-sensitive projection and its digest; inspect it before using it as an approval
artifact:

```bash
uv run --python 3.12 --with '.[dev]' python - <<'PY'
import hashlib
import json
import os
from pathlib import Path

from scripts.qualify_gemini_caller_turn_assembly import (
    canonical_json_sha256,
    canonicalize_qualification_setup,
)

setup_path = Path(os.environ["SETUP"])
deviations_path = Path(os.environ["DEVIATIONS"])
setup_sha = hashlib.sha256(setup_path.read_bytes()).hexdigest()
deviations_sha = hashlib.sha256(deviations_path.read_bytes()).hexdigest()
canonical = canonicalize_qualification_setup(
    json.loads(setup_path.read_text()),
    setup_file_sha256=setup_sha,
    deviations_sha256=deviations_sha,
)
print(json.dumps({
    "canonical_setup": canonical,
    "canonical_setup_sha256": canonical_json_sha256(canonical),
}, indent=2, sort_keys=True))
PY
```

Run the default non-executing validation with every value explicit:

```bash
uv run --python 3.12 --with '.[dev]' python \
  scripts/qualify_gemini_caller_turn_assembly.py \
  --dry-run \
  --model-resource "$MODEL_RESOURCE" \
  --api-version "$API_VERSION" \
  --endpoint "$OFFICIAL_ENDPOINT" \
  --project "$NONPRODUCTION_PROJECT" \
  --credential-ref "$DEDICATED_CREDENTIAL_ENV_NAME" \
  --manifest "$MANIFEST" \
  --manifest-sha256 "$MANIFEST_SHA256" \
  --setup "$SETUP" \
  --setup-file-sha256 "$SETUP_SHA256" \
  --canonical-setup-sha256 "$CANONICAL_SETUP_SHA256" \
  --deviations "$DEVIATIONS" \
  --deviations-sha256 "$DEVIATIONS_SHA256" \
  --source-sha "$SOURCE_SHA" \
  --attempt-cap "$ATTEMPT_CAP" \
  --wall-clock-cap-seconds "$WALL_CLOCK_CAP_SECONDS" \
  --session-timeout-seconds "$SESSION_TIMEOUT_SECONDS" \
  --max-cost-usd "$MAX_COST_USD" \
  --max-cost-per-attempt-usd "$MAX_COST_PER_ATTEMPT_USD" \
  --output "$EXTERNAL_REPORT_PATH"
```

Default invocation and `--dry-run` do not read the credential environment variable,
resolve DNS, create a socket, or import the WebSocket client. `dry_run_blocked` is
the expected result while the checked-in manifest remains pending.

## Separate Execution Approval

Do not use `--execute` during Gate 0A. A later approval must quote the exact
machine-readable preregistration block and name all of these immutable values:

- source, runner, evaluator, immutable-pipeline, manifest, setup, deviation, and
  canonical-setup digests;
- exact model resource, API version, official endpoint, non-production project, and
  dedicated qualification credential reference;
- attempt, wall-clock, session-timeout, per-attempt worst-case cost, and total
  maximum-cost caps;
- complete setup projection and every reviewed deviation.

The approved credential must be dedicated to qualification, absent from live-call
services, scoped to the non-production project, and revocable immediately. Never
reuse `GEMINI_API_KEY`, a live-call key, an admin bearer token, or a production
service credential.

Only after that separate approval may the identical dry-run command add
`--execute`. Any digest or value drift voids the approval and requires a new review.

## Output and Stop Conditions

Write reports outside the repository. Reports contain preregistration, aggregate
event/status/failure counts, bounded error codes, and evidence-taxonomy booleans.
They contain no prompt, tool declarations, transcript, raw message, audio, caller or
contractor ID, credential, file path, or exception body.

A partial, cancelled, timed-out, malformed, oversized, setup-rejected, reconnected,
or abnormally closed run remains nonauthorizing. All evidence booleans remain false
in this runner; Gate 0B evaluation and ADR review decide whether
`turn_assembly_validated` can change.

Stop immediately when a cap is reached, an input digest changes, an unlisted setup
deviation appears, provenance or consent fails, a payload reaches output/logging, or
any request would use production infrastructure or real caller data. Do not retry by
raising a cap or editing a sealed holdout.
