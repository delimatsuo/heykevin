"""Single frozen source and runtime identity contract for Gate 0B."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping

from app.services.qualification_identity import (
    EXECUTION_DEPENDENCY_PATHS,
    IdentityError,
    canonical_json_bytes,
    capture_environment_identity,
    capture_source_identity,
    validate_interpreter_installation_identity,
    validate_runtime_site_packages_identity,
    validate_trusted_startup_policy_report,
)


EXPECTED_PYTHON = "3.12.13"
EXPECTED_UV = "0.11.7"
EXECUTION_IDENTITY_SCHEMA_ID = "gate_0b_environment_identity_v5"
SHA256 = re.compile(r"[0-9a-f]{64}")
SOURCE_SHA = re.compile(r"[0-9a-f]{40,64}")
EXECUTION_IMPORT_NAMES = (
    "websockets",
    "cryptography",
    "app.services.caller_turn_alignment",
    "app.services.caller_turn_measurement",
    "app.services.caller_turn_qualification",
    "app.services.caller_turns",
    "app.services.gemini_turn_events",
    "app.services.qualification_allocation",
    "app.services.qualification_environment",
    "app.services.qualification_identity",
    "app.services.qualification_ledger",
    "app.services.qualification_privacy",
    "app.services.qualification_private_paths",
    "app.services.voice_turn_replay",
    "app.utils.audio",
)


def build_execution_identity_report(
    repo_root: str | Path,
    *,
    expected_source_sha: str,
    trusted_startup: Mapping[str, Any],
) -> dict[str, Any]:
    """Capture the one identity report used by preregistration and execution."""
    startup = validate_trusted_startup_policy_report(trusted_startup)
    source_preflight = startup["source_preflight"]
    if source_preflight["source_sha"] != expected_source_sha:
        raise IdentityError("startup source SHA mismatch")
    source = capture_source_identity(
        repo_root,
        expected_source_sha=source_preflight["source_sha"],
        dependency_paths=EXECUTION_DEPENDENCY_PATHS,
    )
    source_report = source.redacted_report_dict()
    if source_report != source_preflight:
        raise IdentityError("startup source identity mismatch")
    environment = capture_environment_identity(
        repo_root=repo_root,
        expected_python=EXPECTED_PYTHON,
        expected_uv=EXPECTED_UV,
        import_names=EXECUTION_IMPORT_NAMES,
        expected_interpreter_installation=startup["interpreter_installation"],
        expected_runtime_site_packages_manifest=startup[
            "runtime_site_packages_manifest"
        ],
    )
    return {
        "schema_id": EXECUTION_IDENTITY_SCHEMA_ID,
        "source": source_report,
        "environment": environment.redacted_report_dict(),
        "trusted_startup": startup,
    }


def validate_execution_identity_report(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "schema_id",
        "source",
        "environment",
        "trusted_startup",
    }:
        raise ValueError("execution identity report fields are invalid")
    if raw["schema_id"] != EXECUTION_IDENTITY_SCHEMA_ID:
        raise ValueError("execution identity report schema is invalid")
    source = raw["source"]
    if not isinstance(source, Mapping) or set(source) != {
        "source_sha",
        "clean",
        "dependencies",
    }:
        raise ValueError("execution source identity is invalid")
    if (
        not isinstance(source["source_sha"], str)
        or not SOURCE_SHA.fullmatch(source["source_sha"])
        or source["clean"] is not True
        or not isinstance(source["dependencies"], Mapping)
        or not source["dependencies"]
    ):
        raise ValueError("execution source identity is invalid")
    for name, identity in source["dependencies"].items():
        if (
            not isinstance(name, str)
            or not SHA256.fullmatch(name)
            or not isinstance(identity, Mapping)
            or set(identity) != {"worktree_sha256", "git_blob_id"}
            or not isinstance(identity["worktree_sha256"], str)
            or not SHA256.fullmatch(identity["worktree_sha256"])
            or not isinstance(identity["git_blob_id"], str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", identity["git_blob_id"])
        ):
            raise ValueError("execution dependency identity is invalid")

    environment = raw["environment"]
    fields = {
        "python_version",
        "uv_version",
        "python_executable_sha256",
        "uv_executable_sha256",
        "python_executable_location_sha256",
        "uv_executable_location_sha256",
        "interpreter_installation",
        "runtime_site_packages_manifest",
        "platform_id",
        "architecture",
        "unicode_version",
        "monotonic_clock_implementation",
        "monotonic_clock_resolution_ns",
        "bytecode_write_disabled",
        "openssl_version",
        "ca_bundle_sha256",
        "lock_sha256",
        "codec_golden_sha256",
        "import_sha256",
        "distributions",
        "distribution_files_sha256",
    }
    if not isinstance(environment, Mapping) or set(environment) != fields:
        raise ValueError("execution environment identity is invalid")
    for field in (
        "python_executable_sha256",
        "uv_executable_sha256",
        "python_executable_location_sha256",
        "uv_executable_location_sha256",
        "ca_bundle_sha256",
        "lock_sha256",
        "codec_golden_sha256",
    ):
        if not isinstance(environment[field], str) or not SHA256.fullmatch(environment[field]):
            raise ValueError("execution environment digest is invalid")
    for field in (
        "python_version",
        "uv_version",
        "platform_id",
        "architecture",
        "unicode_version",
        "monotonic_clock_implementation",
        "openssl_version",
    ):
        if not isinstance(environment[field], str) or not 0 < len(environment[field]) <= 512:
            raise ValueError("execution environment value is invalid")
    if (
        environment["python_version"] != EXPECTED_PYTHON
        or environment["uv_version"] != EXPECTED_UV
        or isinstance(environment["monotonic_clock_resolution_ns"], bool)
        or not isinstance(environment["monotonic_clock_resolution_ns"], int)
        or not 1 <= environment["monotonic_clock_resolution_ns"] <= 1_000_000_000
        or environment["bytecode_write_disabled"] is not True
    ):
        raise ValueError("execution runtime policy is invalid")
    try:
        interpreter_installation = validate_interpreter_installation_identity(
            environment["interpreter_installation"]
        )
    except IdentityError as exc:
        raise ValueError("execution interpreter identity is invalid") from exc
    if (
        interpreter_installation["python_executable_sha256"]
        != environment["python_executable_sha256"]
    ):
        raise ValueError("execution interpreter identity is inconsistent")
    try:
        runtime_site_packages_manifest = validate_runtime_site_packages_identity(
            environment["runtime_site_packages_manifest"]
        )
    except IdentityError as exc:
        raise ValueError("execution runtime site-packages identity is invalid") from exc
    _validate_digest_map(environment["import_sha256"], label="import")
    _validate_digest_map(
        environment["distribution_files_sha256"],
        label="distribution file",
    )
    distributions = environment["distributions"]
    if not isinstance(distributions, Mapping) or any(
        not isinstance(name, str)
        or not name
        or not isinstance(version, str)
        or not version
        or len(version) > 128
        for name, version in distributions.items()
    ):
        raise ValueError("execution distribution versions are invalid")
    if set(distributions) != set(environment["distribution_files_sha256"]):
        raise ValueError("execution distribution identities are inconsistent")
    try:
        startup = validate_trusted_startup_policy_report(raw["trusted_startup"])
    except IdentityError as exc:
        raise ValueError("execution trusted startup identity is invalid") from exc
    if startup["source_preflight"] != source:
        raise ValueError("execution source and startup identities differ")
    if startup["interpreter_installation"] != interpreter_installation:
        raise ValueError("execution interpreter and startup identities differ")
    if (
        startup["runtime_site_packages_manifest"]
        != runtime_site_packages_manifest
    ):
        raise ValueError("execution site-packages and startup identities differ")
    return json.loads(canonical_json_bytes(raw))


def execution_identity_report_sha256(raw: object) -> str:
    return sha256(canonical_json_bytes(validate_execution_identity_report(raw))).hexdigest()


def _validate_digest_map(raw: object, *, label: str) -> None:
    if not isinstance(raw, Mapping) or not raw or any(
        not isinstance(name, str)
        or not name
        or not isinstance(digest, str)
        or not SHA256.fullmatch(digest)
        for name, digest in raw.items()
    ):
        raise ValueError(f"execution {label} identities are invalid")


def execution_identity_sha256(
    repo_root: str | Path,
    *,
    expected_source_sha: str,
    trusted_startup: Mapping[str, Any],
) -> str:
    report = build_execution_identity_report(
        repo_root,
        expected_source_sha=expected_source_sha,
        trusted_startup=trusted_startup,
    )
    return sha256(canonical_json_bytes(report)).hexdigest()
