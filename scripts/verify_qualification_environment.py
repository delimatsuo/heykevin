#!/usr/bin/env python3
"""Verify the frozen Gate 0B source and runtime identity without network access."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
# Subprocess use is limited to fixed git argv with shell execution disabled.
import subprocess  # nosec B404
import sys
import tempfile
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.qualification_identity import (  # noqa: E402
    IdentityError,
    canonical_json_bytes,
    capture_environment_identity,
    capture_source_identity,
)


EXPECTED_PYTHON = "3.12.13"
EXPECTED_UV = "0.11.7"
DEPENDENCY_PATHS = (
    "config/qualification/gate_0b_approval_root.ed25519.pub",
    "app/services/caller_turn_qualification.py",
    "app/services/qualification_identity.py",
    "app/services/caller_turn_alignment.py",
    "app/services/caller_turn_measurement.py",
    "app/services/caller_turns.py",
    "app/services/gemini_turn_events.py",
    "app/services/voice_turn_replay.py",
    "app/utils/audio.py",
    "scripts/run_gemini_caller_turn_qualification.py",
    "scripts/evaluate_gemini_caller_turn_qualification.py",
    "scripts/verify_qualification_environment.py",
    "app/services/gemini_pipeline.py",
    "app/services/voice_pipeline.py",
    "app/config.py",
    "uv.lock",
)
IMPORT_NAMES = (
    "websockets",
    "cryptography",
    "app.services.caller_turn_qualification",
    "app.services.qualification_identity",
    "app.utils.audio",
)
GIT_BINARY = "/usr/bin/git"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    return parser


def _head() -> str:
    completed = subprocess.run(
        [GIT_BINARY, "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )  # nosec B603
    return completed.stdout.strip()


def _snapshot_path(source_sha: str) -> Path:
    root_digest = sha256(str(REPO_ROOT).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"kevin-gate0b-env-{root_digest}-{source_sha}.json"


def _identity_report(source_sha: str) -> dict[str, object]:
    source = capture_source_identity(
        REPO_ROOT,
        expected_source_sha=source_sha,
        dependency_paths=DEPENDENCY_PATHS,
    )
    environment = capture_environment_identity(
        repo_root=REPO_ROOT,
        expected_python=EXPECTED_PYTHON,
        expected_uv=EXPECTED_UV,
        import_names=IMPORT_NAMES,
    )
    return {
        "schema_id": "gate_0b_environment_identity_v1",
        "source": source.redacted_report_dict(),
        "environment": environment.redacted_report_dict(),
    }


def _write_private(path: Path, report: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, canonical_json_bytes(report) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source_sha = _head()
        report = _identity_report(source_sha)
        snapshot = _snapshot_path(source_sha)
        if args.phase == "before":
            if snapshot.exists():
                raise IdentityError("environment snapshot already exists")
            _write_private(snapshot, report)
        else:
            try:
                expected = json.loads(snapshot.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise IdentityError("before snapshot is unavailable") from exc
            if expected != report:
                raise IdentityError("before and after environment identity differ")
            snapshot.unlink()
    except (IdentityError, OSError, subprocess.SubprocessError):
        print('{"error_code":"identity_verification_failed","status":"fail"}')
        return 1
    print(
        json.dumps(
            {
                "identity_sha256": sha256(canonical_json_bytes(report)).hexdigest(),
                "phase": args.phase,
                "status": "pass",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
