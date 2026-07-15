"""Single frozen source and runtime identity contract for Gate 0B."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from app.services.qualification_identity import (
    canonical_json_bytes,
    capture_environment_identity,
    capture_source_identity,
)


EXPECTED_PYTHON = "3.12.13"
EXPECTED_UV = "0.11.7"
EXECUTION_DEPENDENCY_PATHS = (
    "config/qualification/gate_0b_approval_root.ed25519.pub",
    "app/services/caller_turn_qualification.py",
    "app/services/qualification_environment.py",
    "app/services/qualification_identity.py",
    "app/services/qualification_ledger.py",
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
EXECUTION_IMPORT_NAMES = (
    "websockets",
    "cryptography",
    "app.services.caller_turn_qualification",
    "app.services.qualification_environment",
    "app.services.qualification_identity",
    "app.services.qualification_ledger",
    "app.utils.audio",
)


def build_execution_identity_report(
    repo_root: str | Path,
    *,
    expected_source_sha: str,
) -> dict[str, Any]:
    """Capture the one identity report used by preregistration and execution."""
    source = capture_source_identity(
        repo_root,
        expected_source_sha=expected_source_sha,
        dependency_paths=EXECUTION_DEPENDENCY_PATHS,
    )
    environment = capture_environment_identity(
        repo_root=repo_root,
        expected_python=EXPECTED_PYTHON,
        expected_uv=EXPECTED_UV,
        import_names=EXECUTION_IMPORT_NAMES,
    )
    return {
        "schema_id": "gate_0b_environment_identity_v1",
        "source": source.redacted_report_dict(),
        "environment": environment.redacted_report_dict(),
    }


def execution_identity_sha256(
    repo_root: str | Path,
    *,
    expected_source_sha: str,
) -> str:
    report = build_execution_identity_report(
        repo_root,
        expected_source_sha=expected_source_sha,
    )
    return sha256(canonical_json_bytes(report)).hexdigest()
