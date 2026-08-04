from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = (
    ROOT
    / "docs/security/"
    "voice-bakeoff-environment-reconciliation-phase-0-5-v10.json"
)
SCHEMA_PATH = (
    ROOT
    / "docs/security/"
    "voice-bakeoff-environment-reconciliation-phase-0-5-v10.schema.json"
)
GUIDE_PATH = (
    ROOT
    / "docs/security/"
    "voice-bakeoff-environment-reconciliation-phase-0-5-v10.md"
)
SCRIPT_PATH = ROOT / "scripts/verify_voice_bakeoff_phase_one_admission_v10.py"


def _load_module():
    specification = importlib.util.spec_from_file_location(
        "voice_bakeoff_v10_verifier",
        SCRIPT_PATH,
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def test_v10_public_files_parse_and_schema_is_closed():
    contract = json.loads(CONTRACT_PATH.read_text())
    schema = json.loads(SCHEMA_PATH.read_text())

    assert contract["schema_version"] == 10
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(schema["required"])
    assert "materialize" not in SCRIPT_PATH.name


def test_v10_verifier_imports_only_stdlib_and_no_connected_modules():
    tree = ast.parse(SCRIPT_PATH.read_text())
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imports <= {
        "__future__",
        "hashlib",
        "json",
        "pathlib",
        "re",
        "sys",
        "typing",
    }
    forbidden = {
        "socket",
        "subprocess",
        "requests",
        "httpx",
        "google",
        "twilio",
        "app",
    }
    assert not imports & forbidden


def test_v10_predecessor_bundle_is_exact_and_digest_bound():
    contract = json.loads(CONTRACT_PATH.read_text())
    entries = contract["predecessor_exact_bundle"]

    assert entries == sorted(entries, key=lambda entry: entry["path"])
    assert contract["predecessor_bundle_digest"] == hashlib.sha256(
        _canonical(entries)
    ).hexdigest()
    for entry in entries:
        assert hashlib.sha256((ROOT / entry["path"]).read_bytes()).hexdigest() == entry[
            "sha256"
        ]


def test_v10_is_unanimously_blocked_and_non_authorizing():
    contract = json.loads(CONTRACT_PATH.read_text())

    assert set(contract["panel_consensus"].values()) >= {
        "blocked_real_materialization",
        "blocked",
        "advisory_non_authorizing",
    }
    assert contract["decision"] == {
        "materialization": "blocked",
        "v9_runner_invocation": "forbidden",
        "connected_actions": "forbidden",
        "product_evidence": "none",
        "v10_scope": "source_only_acceptance_and_status_verification",
    }
    assert "invoke_v9_materializer" in contract["forbidden_actions"]
    assert "execute_task_4_8" in contract["forbidden_actions"]


def test_v10_all_local_and_external_requirements_fail_closed():
    contract = json.loads(CONTRACT_PATH.read_text())

    local = contract["local_candidate_requirements"]
    external = contract["external_ceremony_requirements"]
    assert [item["id"] for item in local] == [
        f"L{index:02d}" for index in range(1, 9)
    ]
    assert [item["id"] for item in external] == [
        f"E{index:02d}" for index in range(1, 6)
    ]
    assert all(
        item["status"] == "not_satisfied" and item["blocking"] is True
        for item in [*local, *external]
    )


def test_v10_operator_table_distinguishes_every_state():
    contract = json.loads(CONTRACT_PATH.read_text())
    rows = contract["operator_decision_table"]

    assert [row["state"] for row in rows] == [
        "pre_admission_rejected",
        "consumed_no_payload",
        "consumed_with_residue_stop",
        "generated_one_payload",
    ]
    assert rows[0]["attempt_consumed"] is False
    assert all(row["attempt_consumed"] is True for row in rows[1:])
    assert [row["payload_usable"] for row in rows] == [False, False, False, True]
    assert all(
        row["retry"] == "never_same_authorization" for row in rows[1:]
    )


def test_v10_attempt_event_schema_is_closed_by_contract():
    contract = json.loads(CONTRACT_PATH.read_text())
    event_schema = contract["future_attempt_event_schema"]

    assert event_schema["allowed_states"] == [
        row["state"] for row in contract["operator_decision_table"]
    ]
    assert event_schema["private_values_present"] is False
    assert event_schema["connected_action_occurred"] is False
    assert len(event_schema["required_fields"]) == len(
        set(event_schema["required_fields"])
    )


def test_v10_verifier_returns_only_blocked_public_status(capsys):
    module = _load_module()

    result = module.verify()

    assert result == {
        "status": "verified_blocked_contract",
        "materialization": "blocked",
        "v9_runner_invocation": "forbidden",
        "connected_actions": "forbidden",
        "product_evidence": "none",
        "local_requirements_satisfied": 0,
        "external_requirements_satisfied": 0,
    }
    assert capsys.readouterr().out == ""


def test_v10_cli_is_payload_safe_and_accepts_no_arguments():
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(SCRIPT_PATH)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    status = json.loads(result.stdout)

    assert result.returncode == 0
    assert result.stderr == ""
    assert status["status"] == "verified_blocked_contract"
    assert status["materialization"] == "blocked"
    assert status["product_evidence"] == "none"

    rejected = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(SCRIPT_PATH), "/tmp/secret"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 64
    assert rejected.stdout == ""
    assert rejected.stderr == ""


def test_v10_tampering_fails_without_output(tmp_path: Path):
    module = _load_module()
    mirror = tmp_path / "repo"
    for path in (
        CONTRACT_PATH,
        SCHEMA_PATH,
        GUIDE_PATH,
        *(
            ROOT / entry["path"]
            for entry in json.loads(CONTRACT_PATH.read_text())[
                "predecessor_exact_bundle"
            ]
        ),
    ):
        destination = mirror / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
    contract = json.loads((mirror / CONTRACT_PATH.relative_to(ROOT)).read_text())
    contract["decision"]["materialization"] = "ready"
    (mirror / CONTRACT_PATH.relative_to(ROOT)).write_text(
        json.dumps(contract)
    )

    with pytest.raises(module.VerificationError):
        module.verify(mirror)


def test_v10_guide_has_cold_read_safety_language():
    guide = GUIDE_PATH.read_text()

    for phrase in (
        "Real payload materialization is blocked",
        "Do not invoke the V9 materializer",
        "must never contain sensitive information",
        "payload file by itself is always residue",
        "There is no automatic promotion",
    ):
        assert phrase in guide


def test_v10_contains_no_real_private_or_authorization_record():
    text = "\n".join(
        (
            CONTRACT_PATH.read_text(),
            SCHEMA_PATH.read_text(),
            GUIDE_PATH.read_text(),
            SCRIPT_PATH.read_text(),
        )
    )

    assert "one_use_nonce_seed" not in text
    assert "owner_public_key_digest" not in text
    assert "materialization_authorization.json" not in text
    assert "private-input.json" not in text
