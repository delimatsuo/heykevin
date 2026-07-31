"""Tests for the fail-closed Task 4.8 synthetic preparation package."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import textwrap
from types import ModuleType
from typing import Any

import pytest

from app.services.voice_bakeoff_gate_report import (
    GateReportError,
    build_task_4_8_gate_report,
)


_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = (
    _ROOT / "docs/security/task-4-8-synthetic-preparation.manifest.json"
)
_OVERVIEW_PATH = _ROOT / "docs/security/task-4-8-synthetic-preparation.md"
_VALIDATOR_PATH = _ROOT / "tests/support/task_4_8_synthetic_preparation.py"
_STATIC_GATE_PATH = (
    _ROOT / "tests/support/task_4_8_synthetic_preparation_static_gate.py"
)
_COMPLETION_VALIDATOR_PATH = (
    _ROOT / "tests/support/voice_bakeoff_task_4_8_gate_validator.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


static_gate = _load_module("task_4_8_synthetic_preparation_static_gate", _STATIC_GATE_PATH)
completion = _load_module(
    "voice_bakeoff_task_4_8_gate_validator_for_preparation_test",
    _COMPLETION_VALIDATOR_PATH,
)


def _preparation() -> ModuleType:
    source = _VALIDATOR_PATH.read_bytes()
    inspection = static_gate.inspect_verifier_source(
        source=source,
        location="tests/support/task_4_8_synthetic_preparation.py",
    )
    assert inspection.status == "not_authorized"
    assert inspection.diagnostics == ()
    return _load_module("task_4_8_synthetic_preparation", _VALIDATOR_PATH)


def _manifest() -> dict[str, Any]:
    value = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _artifacts(preparation: ModuleType) -> dict[str, bytes]:
    return {
        preparation.MANIFEST_PATH: _MANIFEST_PATH.read_bytes(),
        preparation.OVERVIEW_PATH: _OVERVIEW_PATH.read_bytes(),
        preparation.STATIC_GATE_PATH: _STATIC_GATE_PATH.read_bytes(),
        preparation.VALIDATOR_PATH: _VALIDATOR_PATH.read_bytes(),
        preparation.TEST_PATH: Path(__file__).read_bytes(),
    }


def _changes(preparation: ModuleType) -> tuple[object, ...]:
    return tuple(
        preparation.CandidateChange(
            status="A",
            path=path,
            old_path=None,
            mode="100644",
            file_kind="regular",
        )
        for path in preparation.CHANGED_PATHS
    )


def _with_artifact_digests(
    package: dict[str, Any], artifacts: dict[str, bytes]
) -> dict[str, Any]:
    result = deepcopy(package)
    for item in result["artifact_digests"]:
        item["sha256"] = hashlib.sha256(artifacts[item["path"]]).hexdigest()
    return result


def _validate(
    package: dict[str, Any] | None = None,
    artifacts: dict[str, bytes] | None = None,
    changes: tuple[object, ...] | None = None,
    verifier: ModuleType | None = None,
) -> object:
    preparation = verifier if verifier is not None else _preparation()
    return preparation.validate_synthetic_preparation(
        manifest_bytes=_canonical(package if package is not None else _manifest()),
        artifacts=artifacts if artifacts is not None else _artifacts(preparation),
        candidate_changes=changes if changes is not None else _changes(preparation),
    )


def _categories(result: object) -> set[str]:
    return {item.category for item in result.diagnostics}


def test_static_gate_vets_candidate_before_any_candidate_import() -> None:
    inspection = static_gate.inspect_verifier_source(
        source=_VALIDATOR_PATH.read_bytes(),
        location="tests/support/task_4_8_synthetic_preparation.py",
    )

    assert inspection.status == "not_authorized"
    assert inspection.diagnostics == ()


def test_static_gate_pins_exact_verifier_bytes_before_structure_check() -> None:
    source = _VALIDATOR_PATH.read_bytes()
    tampered = source + b"\n# synthetic-only tamper\n"

    inspection = static_gate.inspect_verifier_source(
        source=tampered,
        location="tests/support/task_4_8_synthetic_preparation.py",
    )

    assert inspection.status == "invalid_local_package"
    assert inspection.diagnostics[0].category == "static_source_digest"


def test_package_is_synthetic_only_and_always_denies_execution() -> None:
    result = _validate()

    assert result.status == "not_authorized"
    assert result.execution_status == "not_authorized"
    assert result.diagnostics == ()


def test_manifest_has_closed_nine_gate_matrix_and_exact_operator_state() -> None:
    preparation = _preparation()
    package = _manifest()

    assert package["format"] == "preparation_manifest_v1"
    assert package["package_status"] == "synthetic_preparation_only"
    assert package["execution_supported"] is False
    assert package["execution_status"] == "not_authorized"
    assert package["data_scope"] == "synthetic_only"
    assert package["operator_banner"] == preparation.OPERATOR_BANNER
    assert package["owner_direction"] == {
        "package_preparation_consent": "recorded",
        "sealed_owner_runtime_authorization": "missing",
    }
    assert [row["gate_id"] for row in package["gate_matrix"]] == [
        row[0] for row in preparation.GATE_ROWS
    ]
    assert all(row["status"] == "unmet_external" for row in package["gate_matrix"])
    assert all(row["consequence"] == "runtime_blocked" for row in package["gate_matrix"])


@pytest.mark.parametrize(
    ("mutator", "expected_category"),
    [
        (lambda value: value.update({"unexpected": "field"}), "schema_root"),
        (
            lambda value: value["baseline"].update({"main_commit_sha": "a" * 40}),
            "baseline_commit",
        ),
        (
            lambda value: value["baseline"].update({"main_tree_sha": "b" * 40}),
            "baseline_tree",
        ),
        (
            lambda value: value.update({"execution_status": "authorized"}),
            "execution_state",
        ),
        (
            lambda value: value.update({"data_scope": "mixed"}),
            "data_scope",
        ),
        (
            lambda value: value["gate_matrix"][0].update({"status": "met"}),
            "gate_state",
        ),
        (
            lambda value: value["gate_matrix"][0].update({"external_ref": "x"}),
            "schema_gate",
        ),
    ],
)
def test_schema_and_state_conflicts_fail_closed(
    mutator: object, expected_category: str
) -> None:
    package = _manifest()
    mutator(package)

    result = _validate(package=package)

    assert result.status == "invalid_local_package"
    assert result.execution_status == "not_authorized"
    assert expected_category in _categories(result)


def test_duplicate_or_noncanonical_json_fails_closed() -> None:
    preparation = _preparation()
    duplicate = (
        b'{"format":"preparation_manifest_v1",'
        b'"format":"preparation_manifest_v1"}\n'
    )
    noncanonical = _canonical(_manifest()) + b" "

    duplicate_result = preparation.validate_synthetic_preparation(
        manifest_bytes=duplicate,
        artifacts=_artifacts(preparation),
        candidate_changes=_changes(preparation),
    )
    noncanonical_result = preparation.validate_synthetic_preparation(
        manifest_bytes=noncanonical,
        artifacts=_artifacts(preparation),
        candidate_changes=_changes(preparation),
    )

    assert duplicate_result.status == "invalid_local_package"
    assert "manifest_encoding" in _categories(duplicate_result)
    assert noncanonical_result.status == "invalid_local_package"
    assert "manifest_canonicalization" in _categories(noncanonical_result)


@pytest.mark.parametrize(
    "canary",
    [
        "https" + "://" + "synthetic-canary" + ".invalid/path",
        "wss" + "://" + "synthetic-canary" + ".invalid/socket",
        "212" + "-555-1212",
        "api" + ".example" + ".com",
        "provider" + "_ref" + "=synthetic-canary",
    ],
)
def test_sensitive_canaries_fail_without_echoing_payload(canary: str) -> None:
    preparation = _preparation()
    artifacts = _artifacts(preparation)
    artifacts[preparation.TEST_PATH] += canary.encode("utf-8")
    package = _with_artifact_digests(_manifest(), artifacts)

    result = _validate(
        package=package,
        artifacts=artifacts,
        verifier=preparation,
    )

    assert result.status == "invalid_local_package"
    assert "sensitive_literal" in _categories(result)
    assert canary not in str(result.diagnostics)


@pytest.mark.parametrize(
    "injection",
    [
        b"getattr(__builtins__, \"__import__\")(\"sub\" + \"process\")",
        b"__builtins__[\"__import__\"](\"sub\" + \"process\")",
        b"eval(\"1 + 1\")",
        b"re.compile = __import__\nre.compile = re.compile(\"os\").system",
        b"json.loads = open\njson.loads(\"synthetic\")",
    ],
)
def test_static_gate_rejects_indirect_dynamic_code(injection: bytes) -> None:
    source = _VALIDATOR_PATH.read_bytes().replace(
        b"from __future__ import annotations",
        b"from __future__ import annotations\n" + injection,
        1,
    )

    inspection = static_gate._inspect_structure(
        source=source,
        location="tests/support/task_4_8_synthetic_preparation.py",
    )

    assert inspection.status == "invalid_local_package"
    assert inspection.diagnostics[0].category in {
        "static_assignment",
        "static_call",
        "static_dynamic_code",
        "static_rebinding",
    }


def test_verifier_refuses_custom_input_containers_before_using_them() -> None:
    preparation = _preparation()

    class HostileArtifacts(dict[str, bytes]):
        def items(self) -> object:
            raise AssertionError("must not inspect subclass input")

    artifact_result = preparation.validate_synthetic_preparation(
        manifest_bytes=_MANIFEST_PATH.read_bytes(),
        artifacts=HostileArtifacts(),
        candidate_changes=_changes(preparation),
    )
    manifest_result = preparation.validate_synthetic_preparation(
        manifest_bytes=bytearray(_MANIFEST_PATH.read_bytes()),
        artifacts=_artifacts(preparation),
        candidate_changes=_changes(preparation),
    )
    change_result = preparation.validate_synthetic_preparation(
        manifest_bytes=_MANIFEST_PATH.read_bytes(),
        artifacts=_artifacts(preparation),
        candidate_changes=list(_changes(preparation)),
    )

    assert "artifact_input_type" in _categories(artifact_result)
    assert "manifest_input_type" in _categories(manifest_result)
    assert "candidate_input_type" in _categories(change_result)


@pytest.mark.parametrize(
    ("status", "mode", "file_kind"),
    [
        ("M", "100644", "regular"),
        ("A", "100755", "regular"),
        ("A", "100644", "symlink"),
    ],
)
def test_changed_path_mode_and_link_scope_fail_closed(
    status: str, mode: str, file_kind: str
) -> None:
    preparation = _preparation()
    changes = list(_changes(preparation))
    changes[1] = preparation.CandidateChange(
        status=status,
        path=preparation.OVERVIEW_PATH,
        old_path=None,
        mode=mode,
        file_kind=file_kind,
    )

    result = _validate(changes=tuple(changes), verifier=preparation)

    assert result.status == "invalid_local_package"
    assert "candidate_scope" in _categories(result)


def test_unallowlisted_canary_path_is_rejected_without_reading_it() -> None:
    preparation = _preparation()
    canary = "DONT" + "_ECHO_SYNTHETIC_CANARY"
    changes = (*_changes(preparation), preparation.CandidateChange(
        status="A",
        path="docs/security/private-canary.txt",
        old_path=None,
        mode="100644",
        file_kind="regular",
    ))

    result = _validate(changes=changes, verifier=preparation)

    assert result.status == "invalid_local_package"
    assert "candidate_scope" in _categories(result)
    assert canary not in str(result.diagnostics)


def test_existing_completion_validator_and_gate_report_refuse_new_manifest() -> None:
    package = _manifest()

    assert completion.validate_gate_package(package).verdict == "preparation_incomplete"
    with pytest.raises(GateReportError):
        build_task_4_8_gate_report(package=package, source_sha="a" * 40)


def test_operator_copy_is_plain_text_accessible_and_never_positive() -> None:
    preparation = _preparation()
    overview = _OVERVIEW_PATH.read_text(encoding="utf-8")
    plain_text = "\n".join(
        textwrap.fill(line, width=72) if line else ""
        for line in overview.splitlines()
    )

    assert overview.splitlines()[0] == preparation.OPERATOR_BANNER
    assert plain_text.startswith("Synthetic preparation only")
    assert "no runtime authorization" in plain_text
    assert "do not use for calls" in plain_text.replace("\n", " ")
    assert overview.index("No caller-facing app") < overview.index("Source identity")
    assert overview.count("`unmet_external`") >= len(preparation.GATE_ROWS)
    assert "invalid_local_package" in overview
    assert "not_authorized" in overview
    assert not re.search(r"\b(approved|valid|passed|ready|complete)\b", overview, re.I)


def test_every_changed_file_is_input_to_the_content_scan() -> None:
    preparation = _preparation()
    assert set(_artifacts(preparation)) == set(preparation.CHANGED_PATHS)
    assert set(preparation.DIGESTED_ARTIFACT_PATHS) == {
        preparation.OVERVIEW_PATH,
        preparation.STATIC_GATE_PATH,
        preparation.VALIDATOR_PATH,
        preparation.TEST_PATH,
    }
