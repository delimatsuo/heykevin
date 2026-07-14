# Gemini Live Offline Replay

This slice provides isolated, reproducible evidence for Gemini Live turn
detection experiments. It is not imported by the live Gemini or ElevenLabs
call paths and does not change a provider model, runtime configuration,
deployment workflow, or release decision.

## Components

- `app/services/voice_turn_replay.py` validates manifests, renders deterministic
  Twilio codec conditions, builds paired automatic/manual schedules, and
  evaluates aggregate gates.
- `scripts/benchmark_gemini_turn_detection.py` is an opt-in provider diagnostic.
  It requires an explicit provider and model plus provider-specific credentials:
  `GEMINI_API_KEY` for Developer or Application Default Credentials for Vertex.
- `scripts/build_fleurs_voice_fixtures.py` rebuilds the pinned FLEURS fixtures
  into an explicitly supplied directory and fails closed on dependency, source,
  format, or checksum drift.
- `tests/fixtures/voice_vad/` contains pinned WebRTC and FLEURS sources,
  manifests, checksums, licenses, and attribution.

The replay evaluator emits only aggregate counts, bounded error codes, latency
statistics, gate results, and corpus identities. It does not emit provider
payloads, audio, transcripts, API keys, or per-case observations.

Provider execution is capped at 60 attempts. A lower ceiling may be supplied
with `--max-provider-attempts`, but the hard ceiling cannot be raised. The
runner stops after the first provider error so a rejected or unhealthy setup
cannot consume the remainder of the cohort.

## Local Verification

```bash
uv run --python 3.12 --with '.[dev]' \
  python -m pytest tests/unit/test_voice_turn_replay.py -q

uv run --python 3.12 --with '.[dev]' \
  ruff check app/services/voice_turn_replay.py \
  scripts/benchmark_gemini_turn_detection.py \
  scripts/build_fleurs_voice_fixtures.py \
  tests/unit/test_voice_turn_replay.py
```

The test suite verifies manifest confinement and checksums, deterministic
rendering and scheduling, provider error normalization, aggregate fail-closed
gates, payload-safe output, and static isolation from the live call pipelines.

## Provider Diagnostic

After separate approval to spend provider quota, choose one explicit provider.
For the Gemini Developer API, provide `GEMINI_API_KEY` through the approved
secret-delivery path and run:

```bash
uv run --python 3.12 --with '.[dev]' \
  python scripts/benchmark_gemini_turn_detection.py \
  --provider developer \
  --model gemini-3.1-flash-live-preview
```

For Vertex, use pre-approved Application Default Credentials with Vertex AI
permissions, set `GCP_PROJECT_ID` to the approved project, and run the pinned
model and location:

```bash
uv run --python 3.12 --with '.[dev]' \
  python scripts/benchmark_gemini_turn_detection.py \
  --provider vertex \
  --model gemini-live-2.5-flash-native-audio \
  --project "$GCP_PROJECT_ID" \
  --location us-central1
```

Do not create temporary credentials or substitute a different project, model, or
location for a recorded diagnostic run.

The selected command exits nonzero when credentials are unavailable, configuration is
invalid, a provider attempt fails, sample coverage is incomplete, or any
latency or lifecycle gate fails. Automatic and manual speech-end latency gates
are fixed at 1,500 ms p95 and 2,500 ms maximum. Reports include the effective
thresholds, timeouts, attempt ceiling, `decision_scope: offline_diagnostic_only`,
and `release_authorized: false`.

A passing report remains experimental evidence only. It cannot authorize
provider selection, live wiring, staging, or production deployment.
