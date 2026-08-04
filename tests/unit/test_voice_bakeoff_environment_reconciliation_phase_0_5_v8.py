"""Offline guards for safe-integer Phase 0.5 v8 materialization."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v3 import (
    _schema_errors,
)
from tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v4 import (
    _digest,
)
from tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v6 import (
    _private_fixture_inputs,
)
from tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v7 import (
    _candidate_errors as _v7_candidate_errors,
    materialize_validated_phase_one_payload as _materialize_v7,
)


_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_PATH = _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v8.json"
_SCHEMA_PATH = (
    _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v8.schema.json"
)
_GUIDE_PATH = _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v8.md"
_V7_PACKAGE_PATH = (
    _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v7.json"
)
_V7_HASHES = {
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v7.json": (
        "1cdcb68d67d9285cc40c783483083f96e60bb3b9dd227fc16c2a0b42170b44ae"
    ),
    (
        "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v7.schema.json"
    ): "edea6eac9471fe8ccb734f36904b7429e91482e45c629d90c48d30480801ee9f",
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v7.md": (
        "418e0033944f3ec9a07c5a72023d55ccb6a6b1da6875ca9420511c9dc30ea288"
    ),
    "tests/unit/test_voice_bakeoff_environment_reconciliation_phase_0_5_v7.py": (
        "f34f0756f8eb262c2075a8411dc734719bb52a6fd6834b670a48e961094cfac7"
    ),
}
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_PREDECESSOR_MANIFEST_DIGEST = (
    "0eb91f2be8ed804400c1c267e7f244e33de244589c5a26a27261b5feb8065326"
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _package() -> dict[str, Any]:
    return _load_json(_PACKAGE_PATH)


def _schema() -> dict[str, Any]:
    return _load_json(_SCHEMA_PATH)


def _verify_predecessor_bundle(package: dict[str, Any]) -> None:
    binding = package["review_bundle_binding"]
    manifest = binding["predecessor_manifest"]
    if (
        binding["predecessor_manifest_digest_sha256"]
        != _PREDECESSOR_MANIFEST_DIGEST
        or _digest(manifest) != _PREDECESSOR_MANIFEST_DIGEST
    ):
        raise ValueError("predecessor_bundle_manifest_invalid")

    observed_paths: list[str] = []
    for entry in manifest:
        relative_path = entry["path"]
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("predecessor_bundle_path_invalid")
        observed_paths.append(relative_path)
        try:
            observed = hashlib.sha256((_ROOT / path).read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError(
                f"predecessor_bundle_unreadable:{relative_path}"
            ) from exc
        if observed != entry["sha256"]:
            raise ValueError(f"predecessor_bundle_digest_mismatch:{relative_path}")

    if observed_paths != sorted(observed_paths) or len(observed_paths) != len(
        set(observed_paths)
    ):
        raise ValueError("predecessor_bundle_paths_not_exact")


def _candidate_errors(payload: dict[str, Any]) -> list[str]:
    schema = _schema()
    errors = _schema_errors(
        payload,
        schema["$defs"]["phase_one_signing_payload_v8"],
        root=schema,
    )
    v7_projection = deepcopy(payload)
    v7_projection["payload_version"] = 7
    v7_projection["v7_normative_contract_digest"] = _load_json(_V7_PACKAGE_PATH)[
        "normative_contract_binding"
    ]["normative_contract_digest_sha256"]
    v7_projection.pop("v7_json_digest")
    v7_projection.pop("v8_normative_contract_digest")
    v7_projection.pop("predecessor_bundle_manifest_digest")
    errors.extend(_v7_candidate_errors(v7_projection))
    return errors


def materialize_validated_phase_one_payload_v8(
    inputs: dict[str, Any],
    *,
    validation_time_ms: int,
) -> dict[str, Any]:
    """Return one safe-integer validated payload or raise with no payload."""
    package = _package()
    _verify_predecessor_bundle(package)
    values = {
        "issued_at_ms": inputs.get("issued_at_ms"),
        "validation_time_ms": validation_time_ms,
        "expires_at_ms": inputs.get("expires_at_ms"),
    }
    for field, value in values.items():
        if type(value) is not int or value < 1 or value > _MAX_SAFE_INTEGER:
            raise ValueError(f"{field}_outside_safe_integer_range")

    payload = _materialize_v7(
        inputs,
        validation_time_ms=validation_time_ms,
    )
    payload["payload_version"] = 8
    payload["v7_json_digest"] = package["normative_contract_binding"]["v7_json_sha256"]
    payload["v8_normative_contract_digest"] = package["normative_contract_binding"][
        "normative_contract_digest_sha256"
    ]
    payload["predecessor_bundle_manifest_digest"] = package[
        "review_bundle_binding"
    ]["predecessor_manifest_digest_sha256"]
    payload.pop("v7_normative_contract_digest")
    errors = _candidate_errors(payload)
    if errors:
        raise ValueError("v8_candidate_invalid:" + ",".join(sorted(set(errors))))
    return payload


def test_v8_root_schema_and_normative_digest_are_exact():
    package = _package()
    schema = _schema()
    assert _schema_errors(package, schema, root=schema) == []
    assert schema["const"] == package
    digest_input = deepcopy(package)
    digest_input["normative_contract_binding"].pop("normative_contract_digest_sha256")
    assert (
        _digest(digest_input)
        == package["normative_contract_binding"]["normative_contract_digest_sha256"]
    )


def test_normal_minimum_and_maximum_safe_timestamps_materialize():
    normal = materialize_validated_phase_one_payload_v8(
        _private_fixture_inputs(),
        validation_time_ms=1_500,
    )
    assert _candidate_errors(normal) == []

    minimum = _private_fixture_inputs()
    minimum["issued_at_ms"] = 1
    minimum["expires_at_ms"] = 2
    assert (
        materialize_validated_phase_one_payload_v8(
            minimum,
            validation_time_ms=1,
        )["issued_at_ms"]
        == 1
    )

    maximum = _private_fixture_inputs()
    maximum["issued_at_ms"] = _MAX_SAFE_INTEGER - 900_000
    maximum["expires_at_ms"] = _MAX_SAFE_INTEGER
    result = materialize_validated_phase_one_payload_v8(
        maximum,
        validation_time_ms=_MAX_SAFE_INTEGER - 1,
    )
    assert result["expires_at_ms"] == _MAX_SAFE_INTEGER
    assert (
        result["predecessor_bundle_manifest_digest"]
        == _PREDECESSOR_MANIFEST_DIGEST
    )
    assert _candidate_errors(result) == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("issued_at_ms", 0),
        ("issued_at_ms", -1),
        ("issued_at_ms", _MAX_SAFE_INTEGER + 1),
        ("issued_at_ms", True),
        ("issued_at_ms", 1_000.0),
        ("expires_at_ms", 0),
        ("expires_at_ms", -1),
        ("expires_at_ms", _MAX_SAFE_INTEGER + 1),
        ("expires_at_ms", True),
        ("expires_at_ms", 2_000.0),
        ("validation_time_ms", 0),
        ("validation_time_ms", -1),
        ("validation_time_ms", _MAX_SAFE_INTEGER + 1),
        ("validation_time_ms", True),
        ("validation_time_ms", 1_500.0),
    ),
)
def test_out_of_range_or_non_exact_timestamps_return_no_payload(
    field: str,
    value: Any,
):
    inputs = _private_fixture_inputs()
    validation_time_ms: Any = 1_500
    if field == "validation_time_ms":
        validation_time_ms = value
    else:
        inputs[field] = value
    with pytest.raises(ValueError, match="safe_integer_range"):
        materialize_validated_phase_one_payload_v8(
            inputs,
            validation_time_ms=validation_time_ms,
        )


def test_public_materializer_fails_before_inputs_if_predecessor_manifest_drifts(
    monkeypatch,
):
    original_package = _package

    def drifted_package() -> dict[str, Any]:
        package = original_package()
        package["review_bundle_binding"]["predecessor_manifest"][0]["sha256"] = (
            "0" * 64
        )
        return package

    monkeypatch.setattr(
        "tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v8._package",
        drifted_package,
    )
    with pytest.raises(ValueError, match="predecessor_bundle_manifest_invalid"):
        materialize_validated_phase_one_payload_v8(
            {},
            validation_time_ms=True,
        )


def test_public_materializer_fails_closed_on_predecessor_file_drift(monkeypatch):
    original_read_bytes = Path.read_bytes
    target = (
        _ROOT
        / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v1.json"
    )

    def drifted_read_bytes(path: Path) -> bytes:
        value = original_read_bytes(path)
        return value + b"\n" if path == target else value

    monkeypatch.setattr(Path, "read_bytes", drifted_read_bytes)
    with pytest.raises(ValueError, match="predecessor_bundle_digest_mismatch"):
        materialize_validated_phase_one_payload_v8(
            _private_fixture_inputs(),
            validation_time_ms=1_500,
        )


def test_v8_revalidates_transformed_payload_before_return(monkeypatch):
    original_materializer = _materialize_v7

    def corrupt_materializer(
        inputs: dict[str, Any],
        *,
        validation_time_ms: int,
    ) -> dict[str, Any]:
        payload = original_materializer(
            inputs,
            validation_time_ms=validation_time_ms,
        )
        payload["requests"][0]["http_method"] = "POST"
        payload["requests"][0]["canonical_request_digest"] = _digest(
            {
                key: value
                for key, value in payload["requests"][0].items()
                if key != "canonical_request_digest"
            }
        )
        payload["request_set_digest"] = _digest(payload["requests"])
        return payload

    monkeypatch.setattr(
        "tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v8._materialize_v7",
        corrupt_materializer,
    )
    with pytest.raises(ValueError, match="v8_candidate_invalid"):
        materialize_validated_phase_one_payload_v8(
            _private_fixture_inputs(),
            validation_time_ms=1_500,
        )


def test_v8_preserves_v7_and_keeps_all_connected_actions_sealed():
    for relative_path, expected_hash in _V7_HASHES.items():
        assert hashlib.sha256((_ROOT / relative_path).read_bytes()).hexdigest() == (expected_hash)

    package = _package()
    _verify_predecessor_bundle(package)
    assert package["source_binding"]["source_sha_role"] == (
        "tracked_runtime_baseline_only_not_untracked_review_bundle_identity"
    )
    assert package["review_bundle_binding"]["review_bundle_state"] == (
        "untracked_source_only_exact_hash_set"
    )
    assert package["authority"]["source_only_payload_generation_status"] == ("not_generated")
    assert package["authority"]["owner_signature_status"] == "not_recorded"
    assert package["authority"]["owner_authorization_status"] == "not_recorded"
    assert package["authority"]["connected_inventory_status"] == "not_authorized"
    assert package["authority"]["mutation_status"] == "not_authorized"
    assert package["authority"]["execution_status"] == "not_authorized"
    assert package["authority"]["task_4_8_status"] == "sealed"
    assert package["dispatch_and_later_phase_blockers"]["dispatch_eligibility"] is False
    assert (
        package["transition_policy"]["materialization_is_signature_authorization_or_connected_read"]
        is False
    )

    guide = _GUIDE_PATH.read_text(encoding="utf-8")
    assert "Create no Google Cloud project" in guide
    assert "Do not authorize V8 itself" in guide
    assert "9007199254740991" in guide
    assert "task_4_8_status              sealed" in guide
