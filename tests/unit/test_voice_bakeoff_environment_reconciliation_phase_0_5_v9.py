"""Offline qualification for the standalone V9 one-attempt custody runner."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any

import pytest

from tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v3 import (
    _schema_errors,
)


_ROOT = Path(__file__).resolve().parents[2]
_RUNNER_RELATIVE_PATH = "scripts/materialize_voice_bakeoff_phase_one_payload_v9.py"
_RUNNER_PATH = _ROOT / _RUNNER_RELATIVE_PATH
_PACKAGE_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v9.json"
)
_SCHEMA_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v9.schema.json"
)
_GUIDE_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v9.md"
)
_MANIFEST_PATH = (
    _ROOT
    / "docs/security/"
    "voice-bakeoff-environment-reconciliation-phase-0-5-v9.predecessors.json"
)
_REVIEW_PATHS = tuple(
    sorted(
        (
            "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v9.json",
            (
                "docs/security/"
                "voice-bakeoff-environment-reconciliation-phase-0-5-v9.schema.json"
            ),
            "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v9.md",
            (
                "docs/security/"
                "voice-bakeoff-environment-reconciliation-phase-0-5-v9.predecessors.json"
            ),
            _RUNNER_RELATIVE_PATH,
            "tests/unit/test_voice_bakeoff_environment_reconciliation_phase_0_5_v9.py",
        )
    )
)
_ALLOWED_RUNNER_IMPORTS = {
    "__future__",
    "errno",
    "hashlib",
    "json",
    "os",
    "pathlib",
    "re",
    "stat",
    "sys",
    "typing",
}


def _load_runner():
    specification = importlib.util.spec_from_file_location(
        "voice_bakeoff_v9_materializer",
        _RUNNER_PATH,
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


_RUNNER = _load_runner()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _private_input() -> dict[str, Any]:
    return {
        "owner_selected_inventory_session_uuidv4": (
            "12345678-1234-4234-9234-123456789abc"
        ),
        "owner_public_key_digest": "0" * 64,
        "organization_operator_identity_configuration_digest": "1" * 64,
        "isolated_bakeoff_operator_identity_configuration_digest": "2" * 64,
        "raw_evidence_custody_digest": "3" * 64,
        "one_use_nonce_seed": "4" * 64,
        "issued_at_ms": 1_000,
        "expires_at_ms": 2_000,
        "audit_and_quota_effects_acknowledged": True,
    }


def _copy_read_only_snapshot(tmp_path: Path) -> Path:
    snapshot = tmp_path / "snapshot"
    manifest = _load_json(_MANIFEST_PATH)
    paths = set(_REVIEW_PATHS)
    paths.update(entry["path"] for entry in manifest["entries"])
    for relative_path in sorted(paths):
        source = _ROOT / relative_path
        destination = snapshot / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o444)
    for directory in sorted(
        (path for path in snapshot.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        directory.chmod(0o555)
    snapshot.chmod(0o555)
    return snapshot


def _review_bundle(snapshot: Path) -> list[dict[str, str]]:
    return [
        {
            "path": relative_path,
            "sha256": _sha256(snapshot / relative_path),
        }
        for relative_path in _REVIEW_PATHS
    ]


def _write_private_file(path: Path, value: bytes) -> None:
    path.write_bytes(value)
    path.chmod(0o600)


def _create_ceremony(
    tmp_path: Path,
    snapshot: Path,
    *,
    private_input: dict[str, Any] | bytes | None = None,
) -> tuple[Path, dict[str, Any]]:
    ceremony = tmp_path / f"ceremony-{len(list(tmp_path.glob('ceremony-*')))}"
    ceremony.mkdir(mode=0o700)
    ceremony.chmod(0o700)
    if private_input is None:
        private_bytes = _canonical(_private_input())
    elif isinstance(private_input, bytes):
        private_bytes = private_input
    else:
        private_bytes = _canonical(private_input)
    _write_private_file(ceremony / "private-input.json", private_bytes)

    review_bundle = _review_bundle(snapshot)
    authorization = {
        "record_version": 1,
        "record_kind": "voice_bakeoff_v9_materialization_authorization",
        "decision": "authorize_one_offline_unsigned_payload_materialization",
        "authorization_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "attempt_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "source_sha": _RUNNER._SOURCE_SHA,
        "v9_normative_contract_digest": _RUNNER._V9_CONTRACT_DIGEST,
        "predecessor_manifest_digest": _RUNNER._PREDECESSOR_MANIFEST_DIGEST,
        "review_bundle": review_bundle,
        "review_bundle_digest": _RUNNER._digest(review_bundle),
        "review_acceptances": deepcopy(_RUNNER._REVIEW_ACCEPTANCES),
        "materialization_authority_identity_digest": "5" * 64,
        "custodian_identity_digest": "6" * 64,
        "ceremony_root_digest": _RUNNER._ceremony_root_digest(ceremony),
        "private_input_sha256": hashlib.sha256(private_bytes).hexdigest(),
        "issued_at_ms": 1_000,
        "validation_time_ms": 1_500,
        "expires_at_ms": 2_000,
        "failure_consumes_attempt": True,
        "connected_action_authorized": False,
    }
    authorization["authorization_digest"] = _RUNNER._digest(authorization)
    _write_private_file(
        ceremony / "materialization-authorization.json",
        _canonical(authorization),
    )
    return ceremony, authorization


def _attempt_events(ceremony: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (ceremony / "attempt-record.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def _assert_mode(path: Path, expected: int) -> None:
    assert stat.S_IMODE(os.lstat(path).st_mode) == expected
    assert not path.is_symlink()


def test_v9_contract_schema_manifest_and_canonical_digests_are_exact():
    package = _load_json(_PACKAGE_PATH)
    schema = _load_json(_SCHEMA_PATH)
    manifest = _load_json(_MANIFEST_PATH)

    digest_input = deepcopy(package)
    digest_input["normative_contract_binding"].pop(
        "normative_contract_digest_sha256"
    )
    assert _RUNNER._digest(digest_input) == (
        package["normative_contract_binding"]["normative_contract_digest_sha256"]
    )
    assert (
        package["normative_contract_binding"]["normative_contract_digest_sha256"]
        == _RUNNER._V9_CONTRACT_DIGEST
    )
    assert _RUNNER._digest(manifest["entries"]) == (
        manifest["manifest_digest_sha256"]
    )
    assert manifest["manifest_digest_sha256"] == (
        _RUNNER._PREDECESSOR_MANIFEST_DIGEST
    )
    assert manifest["entries"] == sorted(
        manifest["entries"],
        key=lambda entry: entry["path"],
    )
    assert len(manifest["entries"]) == 32
    assert len({entry["path"] for entry in manifest["entries"]}) == 32
    assert _schema_errors(
        package["sealed_status"]["execution_status"],
        {"const": "not_authorized"},
    ) == []
    assert schema["$defs"]["phase_one_signing_payload_v9"]["additionalProperties"] is (
        False
    )


def test_runner_imports_only_stdlib_and_no_connected_capability():
    tree = ast.parse(_RUNNER_PATH.read_text(encoding="utf-8"))
    observed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            observed.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            observed.add(node.module.split(".", 1)[0])
    assert observed <= _ALLOWED_RUNNER_IMPORTS
    source = _RUNNER_PATH.read_text(encoding="utf-8").lower()
    for forbidden in (
        "import socket",
        "import subprocess",
            "requests.",
            "httpx.",
            "google.cloud",
            "secretmanager",
    ):
        assert forbidden not in source


def test_happy_path_publishes_one_canonical_payload_and_safe_audit(tmp_path):
    snapshot = _copy_read_only_snapshot(tmp_path)
    ceremony, authorization = _create_ceremony(tmp_path, snapshot)

    result = _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)

    assert result["status"] == "generated_one_payload"
    output = ceremony / "unsigned-payload.json"
    payload_bytes = output.read_bytes()
    payload = json.loads(payload_bytes)
    assert payload_bytes == _canonical(payload)
    assert hashlib.sha256(payload_bytes).hexdigest() == result["payload_digest"]
    assert payload["materialization_authorization_digest"] == (
        authorization["authorization_digest"]
    )
    assert "owner_signature" not in payload
    assert "owner_authorization" not in payload
    schema = _load_json(_SCHEMA_PATH)
    assert _schema_errors(
        payload,
        schema["$defs"]["phase_one_signing_payload_v9"],
        root=schema,
    ) == []

    audit = _load_json(ceremony / "payload-safe-audit.json")
    assert audit["status"] == "generated_one_payload"
    assert audit["payload_digest"] == result["payload_digest"]
    assert audit["private_values_present"] is False
    assert audit["connected_action_occurred"] is False
    assert _schema_errors(
        audit,
        schema["$defs"]["payload_safe_audit_v9"],
        root=schema,
    ) == []

    events = _attempt_events(ceremony)
    assert [event["state"] for event in events] == [
        "consumed_no_payload",
        "generated_one_payload",
    ]
    for name in (
        "materialization-authorization.json",
        "private-input.json",
        "attempt-record.jsonl",
        "unsigned-payload.json",
        "payload-safe-audit.json",
    ):
        _assert_mode(ceremony / name, 0o600)
    assert sorted(path.name for path in ceremony.iterdir()) == [
        "attempt-record.jsonl",
        "materialization-authorization.json",
        "payload-safe-audit.json",
        "private-input.json",
        "unsigned-payload.json",
    ]


def test_second_invocation_rejects_before_private_input_read(tmp_path, monkeypatch):
    snapshot = _copy_read_only_snapshot(tmp_path)
    ceremony, _ = _create_ceremony(tmp_path, snapshot)
    _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)

    monkeypatch.setattr(
        _RUNNER,
        "_read_private_input",
        lambda *_args, **_kwargs: pytest.fail("private input was read"),
    )
    with pytest.raises(
        _RUNNER.MaterializationError,
        match="attempt_already_consumed",
    ):
        _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)


def test_concurrent_invocations_allow_exactly_one_payload(tmp_path):
    snapshot = _copy_read_only_snapshot(tmp_path)
    ceremony, _ = _create_ceremony(tmp_path, snapshot)

    def run() -> str:
        try:
            return _RUNNER.materialize_one_attempt(
                ceremony,
                repo_root=snapshot,
            )["status"]
        except _RUNNER.MaterializationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(lambda _index: run(), range(2)))
    assert outcomes == ["attempt_already_consumed", "generated_one_payload"]
    assert len(list(ceremony.glob("unsigned-payload.json"))) == 1
    assert [event["state"] for event in _attempt_events(ceremony)] == [
        "consumed_no_payload",
        "generated_one_payload",
    ]


def test_invalid_private_input_consumes_attempt_and_forbids_retry(tmp_path):
    snapshot = _copy_read_only_snapshot(tmp_path)
    invalid = _private_input()
    invalid.pop("owner_public_key_digest")
    ceremony, _ = _create_ceremony(
        tmp_path,
        snapshot,
        private_input=invalid,
    )

    with pytest.raises(
        _RUNNER.MaterializationError,
        match="private_input_keys_invalid",
    ):
        _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)
    assert not (ceremony / "unsigned-payload.json").exists()
    audit = _load_json(ceremony / "payload-safe-audit.json")
    assert audit["status"] == "consumed_no_payload"
    assert audit["error_code"] == "private_input_keys_invalid"
    assert _attempt_events(ceremony)[-1]["state"] == "consumed_no_payload"

    with pytest.raises(
        _RUNNER.MaterializationError,
        match="attempt_already_consumed",
    ):
        _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)


def test_malicious_predecessor_drift_never_executes_top_level_code(tmp_path):
    snapshot = _copy_read_only_snapshot(tmp_path)
    ceremony, _ = _create_ceremony(tmp_path, snapshot)
    sentinel = tmp_path / "sentinel"
    target = (
        snapshot
        / "tests/unit/test_voice_bakeoff_environment_reconciliation_phase_0_5_v8.py"
    )
    target.chmod(0o644)
    target.write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    target.chmod(0o444)

    with pytest.raises(
        _RUNNER.MaterializationError,
        match="snapshot_digest_mismatch",
    ):
        _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)
    assert not sentinel.exists()
    assert not (ceremony / "attempt-record.jsonl").exists()


def test_second_snapshot_check_catches_swap_before_private_input(
    tmp_path,
    monkeypatch,
):
    snapshot = _copy_read_only_snapshot(tmp_path)
    ceremony, _ = _create_ceremony(tmp_path, snapshot)
    target = (
        snapshot
        / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v1.json"
    )

    def inject(point: str) -> None:
        if point == "after_attempt_consumed":
            target.chmod(0o644)
            target.write_bytes(target.read_bytes() + b"\n")
            target.chmod(0o444)

    monkeypatch.setattr(_RUNNER, "_fault", inject)
    monkeypatch.setattr(
        _RUNNER,
        "_read_private_input",
        lambda *_args, **_kwargs: pytest.fail("private input was read"),
    )
    with pytest.raises(
        _RUNNER.MaterializationError,
        match="snapshot_digest_mismatch",
    ):
        _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)
    assert _attempt_events(ceremony)[-1]["state"] == "consumed_no_payload"
    assert not (ceremony / "unsigned-payload.json").exists()


def test_fault_after_payload_publish_is_terminal_residue_and_no_retry(
    tmp_path,
    monkeypatch,
):
    snapshot = _copy_read_only_snapshot(tmp_path)
    ceremony, _ = _create_ceremony(tmp_path, snapshot)

    def inject(point: str) -> None:
        if point == "after_payload_publish":
            raise _RUNNER.MaterializationError("injected_crash")

    monkeypatch.setattr(_RUNNER, "_fault", inject)
    with pytest.raises(_RUNNER.MaterializationError, match="injected_crash"):
        _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)
    assert (ceremony / "unsigned-payload.json").exists()
    assert _attempt_events(ceremony)[-1]["state"] == (
        "consumed_with_residue_stop"
    )
    audit = _load_json(ceremony / "payload-safe-audit.json")
    assert audit["status"] == "consumed_with_residue_stop"
    with pytest.raises(
        _RUNNER.MaterializationError,
        match="attempt_already_consumed",
    ):
        _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)


def test_existing_output_is_not_overwritten_and_attempt_stops_with_residue(tmp_path):
    snapshot = _copy_read_only_snapshot(tmp_path)
    ceremony, _ = _create_ceremony(tmp_path, snapshot)
    existing = b"preexisting"
    _write_private_file(ceremony / "unsigned-payload.json", existing)

    with pytest.raises(
        _RUNNER.MaterializationError,
        match="exclusive_path_exists",
    ):
        _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)
    assert (ceremony / "unsigned-payload.json").read_bytes() == existing
    assert _attempt_events(ceremony)[-1]["state"] == (
        "consumed_with_residue_stop"
    )


def test_writable_or_symlink_snapshot_rejects_before_attempt(tmp_path):
    snapshot = _copy_read_only_snapshot(tmp_path)
    ceremony, _ = _create_ceremony(tmp_path, snapshot)
    writable = (
        snapshot
        / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.md"
    )
    writable.chmod(0o644)
    with pytest.raises(
        _RUNNER.MaterializationError,
        match="snapshot_file_writable",
    ):
        _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)
    assert not (ceremony / "attempt-record.jsonl").exists()

    writable.chmod(0o444)
    target = (
        snapshot
        / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v3.md"
    )
    target.chmod(0o644)
    target.parent.chmod(0o755)
    target.unlink()
    target.symlink_to(_ROOT / target.relative_to(snapshot))
    target.parent.chmod(0o555)
    with pytest.raises(
        _RUNNER.MaterializationError,
        match="required_file_not_regular",
    ):
        _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)
    assert not (ceremony / "attempt-record.jsonl").exists()


def test_private_input_mode_failure_occurs_after_attempt_consumption(tmp_path):
    snapshot = _copy_read_only_snapshot(tmp_path)
    ceremony, _ = _create_ceremony(tmp_path, snapshot)
    (ceremony / "private-input.json").chmod(0o644)

    with pytest.raises(
        _RUNNER.MaterializationError,
        match="private_file_mode_invalid",
    ):
        _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)
    assert (ceremony / "attempt-record.jsonl").exists()
    assert _attempt_events(ceremony)[-1]["state"] == "consumed_no_payload"


def test_authorization_failure_does_not_consume_attempt(tmp_path):
    snapshot = _copy_read_only_snapshot(tmp_path)
    ceremony, _ = _create_ceremony(tmp_path, snapshot)
    authorization_path = ceremony / "materialization-authorization.json"
    authorization = _load_json(authorization_path)
    authorization["connected_action_authorized"] = True
    _write_private_file(authorization_path, _canonical(authorization))

    with pytest.raises(
        _RUNNER.MaterializationError,
        match="authorization_contract_invalid",
    ):
        _RUNNER.materialize_one_attempt(ceremony, repo_root=snapshot)
    assert not (ceremony / "attempt-record.jsonl").exists()
    assert not (ceremony / "unsigned-payload.json").exists()


def test_cli_emits_no_stdout_stderr_or_traceback_and_writes_only_ceremony_files(
    tmp_path,
):
    snapshot = _copy_read_only_snapshot(tmp_path)
    ceremony, _ = _create_ceremony(tmp_path, snapshot)
    runner = snapshot / _RUNNER_RELATIVE_PATH

    result = subprocess.run(
        [sys.executable, str(runner), str(ceremony)],
        check=False,
        capture_output=True,
        env={},
    )
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == b""
    assert sorted(path.name for path in ceremony.iterdir()) == [
        "attempt-record.jsonl",
        "materialization-authorization.json",
        "payload-safe-audit.json",
        "private-input.json",
        "unsigned-payload.json",
    ]


def test_guide_keeps_every_authority_and_product_claim_sealed():
    guide = _GUIDE_PATH.read_text(encoding="utf-8")
    package = _load_json(_PACKAGE_PATH)
    assert "Status: source-only exact-review candidate. Materialization is blocked." in guide
    assert "## Product evidence: none" in guide
    assert "Operator approval" in guide
    assert "The owner has not signed or authorized a connected read." in guide
    assert "no retry" in guide.lower()
    assert package["product_evidence"]["status"] == "none"
    assert package["sealed_status"] == {
        "source_only_payload_generation_status": "not_generated",
        "owner_signature_status": "not_recorded",
        "owner_authorization_status": "not_recorded",
        "connected_inventory_status": "not_authorized",
        "credential_resolution_status": "not_authorized",
        "mutation_status": "not_authorized",
        "execution_status": "not_authorized",
        "provider_pstn_status": "sealed",
        "staging_production_status": "sealed",
        "task_4_8_status": "sealed",
    }
