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
  It requires `GEMINI_API_KEY` and an explicit non-latest model ID.
- `scripts/build_fleurs_voice_fixtures.py` rebuilds the pinned FLEURS fixtures
  into an explicitly supplied directory and fails closed on dependency, source,
  format, or checksum drift.
- `tests/fixtures/voice_vad/` contains pinned WebRTC and FLEURS sources,
  manifests, checksums, licenses, and attribution.

The replay evaluator emits only aggregate counts, bounded error codes, latency
statistics, gate results, and corpus identities. It does not emit provider
payloads, audio, transcripts, API keys, or per-case observations.

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

After separate approval to spend provider quota, run:

```bash
uv run --python 3.12 --with '.[dev]' \
  python scripts/benchmark_gemini_turn_detection.py \
  --model gemini-3.1-flash-live-preview
```

The command exits nonzero when credentials are unavailable, configuration is
invalid, a provider attempt fails, sample coverage is incomplete, or any
latency or lifecycle gate fails. A passing report remains experimental evidence
only. It cannot authorize provider selection, live wiring, staging, or
production deployment.
