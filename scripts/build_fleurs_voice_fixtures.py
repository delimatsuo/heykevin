#!/usr/bin/env python3
"""Rebuild checksum-pinned FLEURS voice fixtures without transcript output."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version
from io import BytesIO
import json
from pathlib import Path
from typing import Any


DATASET_REVISION = (  # pragma: allowlist secret
    "70bb2e84b976b7e960aa89f1c648e09c59f894dd"  # pragma: allowlist secret
)
EXPECTED_VERSIONS = {
    "datasets": "5.0.0",
    "numpy": "2.5.1",
    "scipy": "1.18.0",
    "soundfile": "0.14.0",
}


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    config: str
    source_sha256: str
    trim_start_ms: int
    trim_end_ms: int
    output_name: str
    output_sha256: str


FIXTURES = (
    FixtureSpec(
        config="en_us",
        source_sha256=(
            "33aca50159ec2e3cbcc894eb26faa916"  # pragma: allowlist secret
            "d6badb1b49fb75994973561540a7f012"  # pragma: allowlist secret
        ),
        trim_start_ms=560,
        trim_end_ms=9_160,
        output_name="fleurs-en_us-test-row-0-trimmed.raw",
        output_sha256=(
            "5747fe849a2c359f060f7433bf5f45ff"  # pragma: allowlist secret
            "ca0b5d73bf4aa9bbeca2da302992d1c1"  # pragma: allowlist secret
        ),
    ),
    FixtureSpec(
        config="es_419",
        source_sha256=(
            "861ceedc4d50fbe52fc89b6d0c0fa740"  # pragma: allowlist secret
            "b9cd6fa668388e754064847386af0faf"  # pragma: allowlist secret
        ),
        trim_start_ms=1_520,
        trim_end_ms=12_480,
        output_name="fleurs-es_419-test-row-0-trimmed.raw",
        output_sha256=(
            "40c05ed28b55ac7975aa81c55ebcdc1d"  # pragma: allowlist secret
            "d482f6f8cc71b0d50b24e67184caed5b"  # pragma: allowlist secret
        ),
    ),
)


def _require_pinned_versions() -> None:
    installed = {package: version(package) for package in EXPECTED_VERSIONS}
    if installed != EXPECTED_VERSIONS:
        raise RuntimeError("fixture dependency versions do not match the pin")


def _load_source_bytes(config: str) -> bytes:
    from datasets import Audio, load_dataset

    dataset = load_dataset(
        "google/fleurs",
        config,
        split="test",
        revision=DATASET_REVISION,
        streaming=True,
    ).cast_column("audio", Audio(decode=False))
    audio = next(iter(dataset))["audio"]
    source_bytes = audio["bytes"]
    if not isinstance(source_bytes, bytes):
        raise RuntimeError("dataset audio bytes are unavailable")
    return source_bytes


def _build_fixture(spec: FixtureSpec) -> bytes:
    import numpy as np
    from scipy.signal import resample_poly
    import soundfile as sf

    source_bytes = _load_source_bytes(spec.config)
    if sha256(source_bytes).hexdigest() != spec.source_sha256:
        raise RuntimeError("dataset source checksum mismatch")

    samples, sample_rate = sf.read(BytesIO(source_bytes), dtype="float32")
    if samples.ndim != 1 or sample_rate != 16_000:
        raise RuntimeError("unexpected FLEURS audio format")
    samples = resample_poly(samples, 8_000, sample_rate).astype(np.float32)
    pcm = np.clip(
        np.rint(samples * 32_767.0),
        -32_768,
        32_767,
    ).astype("<i2")
    fixture = pcm[spec.trim_start_ms * 8 : spec.trim_end_ms * 8].tobytes()
    if sha256(fixture).hexdigest() != spec.output_sha256:
        raise RuntimeError("derived fixture checksum mismatch")
    return fixture


def rebuild(output_dir: Path) -> dict[str, Any]:
    """Rebuild all fixtures and return payload-free verification metadata."""
    _require_pinned_versions()
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for spec in FIXTURES:
        fixture = _build_fixture(spec)
        destination = output_dir / spec.output_name
        destination.write_bytes(fixture)
        outputs.append({
            "config": spec.config,
            "bytes": len(fixture),
            "sha256": spec.output_sha256,
        })
    return {
        "status": "pass",
        "dataset_revision": DATASET_REVISION,
        "outputs": outputs,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = rebuild(args.output_dir)
    except Exception:
        report = {"status": "fail", "error": "fixture_rebuild_failed"}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
