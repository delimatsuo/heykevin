# FLEURS Fixture Attribution

The two `fleurs-*.raw` fixtures are derived from the Google FLEURS dataset,
licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

- Dataset: [google/fleurs](https://huggingface.co/datasets/google/fleurs)
- Paper: [FLEURS: Few-shot Learning Evaluation of Universal Representations of Speech](https://arxiv.org/abs/2205.12446)
- Dataset revision: `70bb2e84b976b7e960aa89f1c648e09c59f894dd`
- Split and row: `test`, row index `0`
- Configs: English US (`en_us`) and Latin American Spanish (`es_419`)
- English compressed source SHA-256: `33aca50159ec2e3cbcc894eb26faa916d6badb1b49fb75994973561540a7f012`
- Spanish compressed source SHA-256: `861ceedc4d50fbe52fc89b6d0c0fa740b9cd6fa668388e754064847386af0faf`

Both sources were decoded from 16 kHz mono audio, resampled to 8 kHz with a
polyphase filter, rounded to signed 16-bit little-endian PCM, and trimmed to
500 ms before the manually inspected speech onset and 500 ms after the
manually inspected final speech tail. The English source uses original range
560-9160 ms with speech labeled at 500-8100 ms in the derived fixture. The
Spanish source uses original range 1520-12480 ms with speech labeled at
500-10460 ms in the derived fixture.

No transcript field was printed, copied, or persisted. These fixtures contain
no Hey Kevin customer audio.

The dataset's numeric storage filenames are intentionally not retained as
semantic identifiers. Config, split, row index, pinned revision, and compressed
source hash identify each input without adding phone-shaped strings to release
artifacts.

Rebuild into an explicit temporary directory with the pinned toolchain:

```bash
uv run --no-project \
  --with datasets==5.0.0 \
  --with numpy==2.5.1 \
  --with scipy==1.18.0 \
  --with soundfile==0.14.0 \
  python scripts/build_fleurs_voice_fixtures.py \
  --output-dir /tmp/kevin-fleurs-rebuild
```

The script fails closed unless the source bytes, dependency versions, audio
format, and both derived fixture hashes match this pinned record. It does not
write into the repository unless that repository path is explicitly supplied.
