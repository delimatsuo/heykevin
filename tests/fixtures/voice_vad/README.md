# Voice VAD Calibration Fixtures

`py-webrtcvad-test-audio.raw` is the upstream `py-webrtcvad` speech fixture at
commit `e283ca41df3a84b0e87fb1f5cb9b21580a286b09`:

https://github.com/wiseman/py-webrtcvad/blob/e283ca41df3a84b0e87fb1f5cb9b21580a286b09/test-audio.raw

The file is 8 kHz, mono, signed 16-bit little-endian PCM. Its SHA-256 is
`3dbb730b90e0266d78d4fe03f00aba37a83780a6a5240d9084b5c01266194c1e`.
The upstream mode-2 test publishes this 30 ms frame classification:

```text
000000 11111 11111 11111 11111 0000
```

Spaces are included only for readability. The pattern labels voiced frames 6
through 25, or approximately 180-780 ms. Replay tests preserve those source
labels as calibration evidence, not as a claim that one English sample
validates production VAD quality.

The fixture is upstream test data, contains no Kevin customer audio or PII, and
is redistributed under the notices in `LICENSE.py-webrtcvad`.

`turn_replay_manifest.json` derives six deterministic cases from this pinned
source: clean, quiet, bounded background noise, 500 ms and 800 ms within-turn
pauses, and fragmented 10/20/30 ms ingress chunks. Every case passes through a
Twilio mu-law round trip before Gemini replay. The manual arm sends one
`activityStart` before all pre-roll audio and one `activityEnd` only after at
least 500 ms beyond the labeled final speech boundary. The automatic arm gets
identical paced audio and retains Gemini's 500 ms server-side silence setting.

These transformations validate the benchmark and isolate provider endpointing;
they do not turn one English source into a production VAD corpus. Promotion
still requires independently labeled quiet speech, accents, multiple speakers,
multiple languages, corrections, background conditions, and deliberate
barge-in recordings under compatible redistribution licenses.

`fleurs_turn_replay_manifest.json` is the default provider benchmark corpus.
It adds pinned, independently sourced English US and Latin American Spanish
FLEURS utterances with manually inspected endpoints. Each source has exactly
500 ms of pre-roll and post-roll around the labeled speech, and the manifest
derives clean, quiet/noisy, and fragmented-frame cases. Source revision,
attribution, conversion, trimming, and labels are recorded in
`FLEURS-ATTRIBUTION.md`; transcript text is not stored. The v2 manifest keeps
sources separate so more languages and speakers can be added without changing
the replay schema.

This bilingual corpus is stronger endpoint evidence than the original single
source, but it is not broad production qualification. Deliberate barge-in,
corrections, more speakers, more languages, and real telephony conditions still
belong in the release cohort.

Run the deterministic offline replay tests with:

```bash
uv run --python 3.12 --with '.[dev]' \
  python -m pytest tests/unit/test_voice_turn_replay.py -q
```

The opt-in provider diagnostic is isolated from live call serving and requires
an explicit, versioned model ID:

```bash
uv run --python 3.12 --with '.[dev]' \
  python scripts/benchmark_gemini_turn_detection.py \
  --model gemini-3.1-flash-live-preview
```

The diagnostic requires `GEMINI_API_KEY` in the environment, exits nonzero on
any failed gate, and reports aggregate lifecycle metrics plus manifest and
semantic corpus hashes. It does not output case-level events, audio, API keys,
or audio-derived text. Its result is experimental evidence only and cannot
authorize a provider selection, release, or production configuration change.
