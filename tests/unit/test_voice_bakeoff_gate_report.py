"""Tests for the read-only Task 4.8 operator gate-status report."""

from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.services.voice_bakeoff_gate_report import (
    GateReportError,
    build_task_4_8_gate_report,
)
from scripts import report_voice_bakeoff_gate


_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = (
    _ROOT
    / "tests/fixtures/voice_architecture_bakeoff"
    / "task_4_8_gate_package.template.json"
)


def _template() -> dict[str, object]:
    value = json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_report_states_all_blocking_controls_without_authorizing_execution() -> None:
    report = build_task_4_8_gate_report(
        package=_template(),
        source_sha="a" * 40,
    )

    assert report.report_source_sha == "a" * 40
    assert report.package_source_binding == "unbound_template"
    assert report.package_status == "preparation_only"
    assert report.execution_status == "not_authorized"
    assert report.advisory_review_status == "advisory_only"
    assert report.owner_approval_status == "not_recorded"
    assert report.required_pre_network_controls == (
        "credential_resolution_must_remain_blocked",
        "networking_must_remain_blocked",
        "provider_and_pstn_must_remain_blocked",
    )
    assert [gate.gate_id for gate in report.blocking_gates] == [
        "independent_signature_quorum",
        "physically_separate_preauth_store",
        "identity_and_credential_broker",
        "durable_trust_and_revocation_store",
        "provider_privacy_and_region_attestations",
        "complete_production_denylist",
        "immutable_custody_and_residue_routing",
        "one_use_runtime_envelope",
    ]
    assert report.as_dict()["execution_status"] == "not_authorized"


@pytest.mark.parametrize(
    ("source_sha", "package_mutation"),
    [
        ("not-a-sha", None),
        ("b" * 40, ("execution_supported", True)),
        ("c" * 40, ("package_status", "execution_authorized")),
    ],
)
def test_report_rejects_invalid_or_execution_capable_package_state(
    source_sha: str,
    package_mutation: tuple[str, object] | None,
) -> None:
    package = deepcopy(_template())
    if package_mutation is not None:
        key, value = package_mutation
        package[key] = value

    with pytest.raises(GateReportError):
        build_task_4_8_gate_report(package=package, source_sha=source_sha)


def test_cli_is_read_only_and_reports_current_source(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        report_voice_bakeoff_gate,
        "_current_source_sha",
        lambda root: "d" * 40,
    )
    monkeypatch.setattr(
        report_voice_bakeoff_gate,
        "_assert_report_sources_clean",
        lambda root: None,
    )

    assert report_voice_bakeoff_gate.main(["--package", str(_TEMPLATE_PATH)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["report_source_sha"] == "d" * 40
    assert payload["execution_status"] == "not_authorized"
    assert payload["owner_approval_status"] == "not_recorded"


def test_cli_rejects_invalid_local_package_without_exposing_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_path = tmp_path / "package.json"
    package_path.write_text('{"execution_supported": true}', encoding="utf-8")
    monkeypatch.setattr(
        report_voice_bakeoff_gate,
        "_current_source_sha",
        lambda root: "e" * 40,
    )

    assert report_voice_bakeoff_gate.main(["--package", str(package_path)]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"report_status": "invalid_local_package"}


@pytest.mark.parametrize(
    "status_output",
    [
        " M app/services/voice_bakeoff_gate_report.py\\n",
        "?? scripts/report_voice_bakeoff_gate.py\\n",
    ],
)
def test_cli_source_binding_refuses_modified_or_untracked_helpers(
    monkeypatch: pytest.MonkeyPatch,
    status_output: str,
) -> None:
    def check_output(command: list[str], **kwargs: object) -> str:
        if "ls-files" in command:
            return (
                "app/services/voice_bakeoff_gate_report.py\\n"
                "scripts/report_voice_bakeoff_gate.py\\n"
            )
        if "status" in command:
            return status_output
        raise AssertionError(command)

    monkeypatch.setattr(
        report_voice_bakeoff_gate.subprocess,
        "check_output",
        check_output,
    )

    with pytest.raises(GateReportError):
        report_voice_bakeoff_gate._assert_report_sources_clean(_ROOT)


def test_report_refuses_specific_package_with_mismatched_source() -> None:
    package = deepcopy(_template())
    envelope = package["signature_envelope"]
    assert isinstance(envelope, dict)
    envelope["source_sha"] = "f" * 40

    with pytest.raises(GateReportError):
        build_task_4_8_gate_report(package=package, source_sha="a" * 40)


def test_report_module_and_cli_have_no_execution_or_network_capability() -> None:
    report_path = _ROOT / "app/services/voice_bakeoff_gate_report.py"
    cli_path = _ROOT / "scripts/report_voice_bakeoff_gate.py"
    report_source = report_path.read_text(encoding="utf-8")
    cli_source = cli_path.read_text(encoding="utf-8")

    for source in (report_source, cli_source):
        assert "--execute-provider" not in source
        assert "provider_execution" not in source
        assert "socket" not in source
        assert "requests" not in source
        assert "http" not in source
        assert "gcloud" not in source

    tree = ast.parse(cli_source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported_modules == {"argparse", "json", "subprocess"}
    assert "voice_bakeoff_gate_report" not in (
        _ROOT / "app/main.py"
    ).read_text(encoding="utf-8")
