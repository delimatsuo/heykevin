"""Offline guards for the narrow Phase 0.5 v6 phase-one generation contract."""

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
    _canonical,
    _digest,
)


_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_PATH = _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v6.json"
_SCHEMA_PATH = (
    _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v6.schema.json"
)
_GUIDE_PATH = _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v6.md"
_V2_PATH = _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.json"
_PROJECT_IDS = (
    "kevin-491315",
    "kevin-staging-491315",
    "hk-voice-bakeoff-0724-iso",
    "hk-voice-bakeoff-preauth-iso",
)
_QUERY_IDENTITIES = {
    "kevin-491315": "organization_operator_identity",
    "kevin-staging-491315": "organization_operator_identity",
    "hk-voice-bakeoff-0724-iso": "isolated_bakeoff_operator_identity",
    "hk-voice-bakeoff-preauth-iso": "isolated_bakeoff_operator_identity",
}
_V5_HASHES = {
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v5.json": (
        "9a273365dddcc8e839d04c63fd9794726a17b4a4197743e81fbc0d897a25da59"
    ),
    (
        "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v5.schema.json"
    ): "3d083be600af252a6ea96cce0d58b5ed593f05425148b9c585e43bfa94bd4f85",
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v5.md": (
        "1bd1d382648f03a8711995ba2cd2e68dd0cdabc538c8f721720521238f70d72c"
    ),
    "tests/unit/test_voice_bakeoff_environment_reconciliation_phase_0_5_v5.py": (
        "9eb8295411b225fc4e40d90aff2bb95ff6c4dea9319734b504332940897f32a3"
    ),
}
_REQUIRED_INPUTS = {
    "owner_selected_inventory_session_uuidv4",
    "owner_public_key_digest",
    "organization_operator_identity_configuration_digest",
    "isolated_bakeoff_operator_identity_configuration_digest",
    "raw_evidence_custody_digest",
    "one_use_nonce_seed",
    "issued_at_ms",
    "expires_at_ms",
}


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _package() -> dict[str, Any]:
    return _load_json(_PACKAGE_PATH)


def _schema() -> dict[str, Any]:
    return _load_json(_SCHEMA_PATH)


def _private_fixture_inputs() -> dict[str, Any]:
    return {
        "owner_selected_inventory_session_uuidv4": ("12345678-1234-4234-9234-123456789abc"),
        "owner_public_key_digest": "0" * 64,
        "organization_operator_identity_configuration_digest": "1" * 64,
        "isolated_bakeoff_operator_identity_configuration_digest": "2" * 64,
        "raw_evidence_custody_digest": "3" * 64,
        "one_use_nonce_seed": "4" * 64,
        "issued_at_ms": 1_000,
        "expires_at_ms": 2_000,
    }


def _request(
    *,
    index: int,
    project_id: str,
    configs: dict[str, str],
) -> dict[str, Any]:
    identity_ref = _QUERY_IDENTITIES[project_id]
    request = {
        "request_index": index,
        "request_id": f"req-{index:03d}",
        "method_ref": "project_identity_get",
        "api_method": "cloudresourcemanager.projects.get",
        "identity_ref": identity_ref,
        "local_configuration_digest": configs[identity_ref],
        "target_project_id": project_id,
        "target_project_number": None,
        "quota_project_id": project_id,
        "endpoint": "cloudresourcemanager.googleapis.com",
        "http_method": "GET",
        "canonical_path_and_query": f"/v1/projects/{project_id}",
        "canonical_request_body": None,
        "request_body_digest": hashlib.sha256(b"null").hexdigest(),
        "response_field_mask": [
            "projectId",
            "projectNumber",
            "lifecycleState",
            "parent",
        ],
        "pagination": "not_applicable",
        "raw_evidence_class": "restricted_external_custody",
    }
    request["canonical_request_digest"] = _digest(request)
    return request


def _materialize(inputs: dict[str, Any]) -> dict[str, Any]:
    package = _package()
    if set(inputs) != _REQUIRED_INPUTS:
        raise ValueError("private_generation_inputs_not_exact")
    forbidden = set(package["phase_one_generation_contract"]["forbidden_generation_inputs"])
    if forbidden.intersection(inputs):
        raise ValueError("forbidden_generation_input")

    configs = {
        "organization_operator_identity": inputs[
            "organization_operator_identity_configuration_digest"
        ],
        "isolated_bakeoff_operator_identity": inputs[
            "isolated_bakeoff_operator_identity_configuration_digest"
        ],
    }
    requests = [
        _request(index=index, project_id=project_id, configs=configs)
        for index, project_id in enumerate(_PROJECT_IDS)
    ]
    return {
        "payload_version": 6,
        "payload_kind": "unsigned_owner_signing_payload",
        "inventory_session_id": inputs["owner_selected_inventory_session_uuidv4"],
        "phase": "project_id_binding",
        "phase_ordinal": 1,
        "predecessor": "GENESIS",
        "source_sha": package["source_binding"]["source_sha"],
        "v2_json_digest": package["normative_contract_binding"]["v2_json_sha256"],
        "v5_json_digest": package["normative_contract_binding"]["v5_json_sha256"],
        "v6_normative_contract_digest": package["normative_contract_binding"][
            "normative_contract_digest_sha256"
        ],
        "phase_one_method_contract_digest": package["normative_contract_binding"][
            "phase_one_method_contract_sha256"
        ],
        "owner_public_key_digest": inputs["owner_public_key_digest"],
        "identity_configuration_digests": configs,
        "raw_evidence_custody_digest": inputs["raw_evidence_custody_digest"],
        "one_use_nonce_seed": inputs["one_use_nonce_seed"],
        "request_count": 4,
        "request_set_digest": _digest(requests),
        "requests": requests,
        "issued_at_ms": inputs["issued_at_ms"],
        "expires_at_ms": inputs["expires_at_ms"],
        "audit_and_quota_effects_acknowledged": True,
    }


def _generation_errors(payload: dict[str, Any]) -> list[str]:
    schema = _schema()
    errors = _schema_errors(
        payload,
        schema["$defs"]["phase_one_signing_payload_v6"],
        root=schema,
    )
    requests = payload.get("requests")
    if not isinstance(requests, list):
        return errors + ["requests"]

    expected_projects = list(_PROJECT_IDS)
    observed_projects = [request.get("target_project_id") for request in requests]
    if observed_projects != expected_projects:
        errors.append("project_order")
    if payload.get("request_count") != len(requests) or len(requests) != 4:
        errors.append("request_count")
    if payload.get("request_set_digest") != _digest(requests):
        errors.append("request_set_digest")
    if not (
        isinstance(payload.get("issued_at_ms"), int)
        and isinstance(payload.get("expires_at_ms"), int)
        and payload["issued_at_ms"] < payload["expires_at_ms"]
    ):
        errors.append("time_bounds")

    ids: list[object] = []
    digests: list[object] = []
    configs = payload.get("identity_configuration_digests", {})
    method = _package()["phase_one_generation_contract"]["exact_method"]
    for position, request in enumerate(requests):
        project_id = _PROJECT_IDS[position]
        identity_ref = _QUERY_IDENTITIES[project_id]
        expected = {
            "request_index": position,
            "request_id": f"req-{position:03d}",
            "method_ref": method["method_ref"],
            "api_method": method["api_method"],
            "identity_ref": identity_ref,
            "local_configuration_digest": configs.get(identity_ref),
            "target_project_id": project_id,
            "target_project_number": None,
            "quota_project_id": project_id,
            "endpoint": method["endpoint"],
            "http_method": method["http_method"],
            "canonical_path_and_query": f"/v1/projects/{project_id}",
            "canonical_request_body": method["request_body"],
            "request_body_digest": hashlib.sha256(b"null").hexdigest(),
            "response_field_mask": method["response_field_mask"],
            "pagination": method["pagination"],
            "raw_evidence_class": method["raw_evidence_class"],
        }
        observed_unsigned = {
            key: value for key, value in request.items() if key != "canonical_request_digest"
        }
        if _canonical(observed_unsigned) != _canonical(expected):
            errors.append(f"request[{position}].exact")
        if request.get("canonical_request_digest") != _digest(observed_unsigned):
            errors.append(f"request[{position}].digest")
        ids.append(request.get("request_id"))
        digests.append(request.get("canonical_request_digest"))
    if len(ids) != len(set(ids)):
        errors.append("request_ids_unique")
    if len(digests) != len(set(digests)):
        errors.append("request_digests_unique")
    return errors


def _refresh_payload_digests(payload: dict[str, Any]) -> None:
    for request in payload["requests"]:
        unsigned = {
            key: value for key, value in request.items() if key != "canonical_request_digest"
        }
        request["canonical_request_digest"] = _digest(unsigned)
    payload["request_set_digest"] = _digest(payload["requests"])


def test_v6_root_schema_and_normative_digests_are_exact():
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
    assert (
        _digest(package["phase_one_generation_contract"]["exact_method"])
        == package["normative_contract_binding"]["phase_one_method_contract_sha256"]
    )
    for subtree in (
        "authority",
        "environment_recommendation",
        "phase_one_generation_contract",
        "unsigned_payload_contract",
        "dispatch_blockers",
        "transition_policy",
    ):
        changed = deepcopy(package)
        changed[subtree]["unexpected_mutation"] = True
        assert _schema_errors(changed, schema, root=schema)


def test_v6_phase_one_method_is_the_exact_v2_bodyless_method():
    package = _package()
    v2 = _load_json(_V2_PATH)
    method = package["phase_one_generation_contract"]["exact_method"]
    v2_method = v2["read_contract"]["methods"]["project_identity_get"]
    assert method == {
        "method_ref": "project_identity_get",
        "api_method": v2_method["api_method"],
        "endpoint": v2_method["endpoint"],
        "http_method": v2_method["http_method"],
        "path_template": v2_method["path"],
        "request_body": v2_method["request_body"],
        "response_field_mask": v2_method["response_field_mask"],
        "pagination": v2_method["pagination"],
        "raw_evidence_class": v2_method["raw_evidence_class"],
    }
    assert method["request_body"] is None


def test_exact_private_inputs_materialize_one_valid_unsigned_payload():
    payload = _materialize(_private_fixture_inputs())
    assert _generation_errors(payload) == []
    assert [request["target_project_id"] for request in payload["requests"]] == (list(_PROJECT_IDS))
    assert "owner_signature" not in payload
    assert "owner_authorization" not in payload
    assert "credential" not in _canonical(payload).decode().lower()


@pytest.mark.parametrize(
    ("attack", "expected_error"),
    (
        ("reorder", "project_order"),
        ("duplicate_identity", "request_ids_unique"),
        ("cross_project_path", "request[0].exact"),
        ("non_null_body", "request[0].exact"),
        ("mutating_method", "request[0].exact"),
        ("wrong_identity_configuration", "request[0].exact"),
        ("extra_signature", "additional"),
        ("invalid_time", "time_bounds"),
    ),
)
def test_generation_attacks_are_rejected(
    attack: str,
    expected_error: str,
):
    payload = _materialize(_private_fixture_inputs())
    if attack == "reorder":
        payload["requests"][0], payload["requests"][1] = (
            payload["requests"][1],
            payload["requests"][0],
        )
    elif attack == "duplicate_identity":
        payload["requests"][1]["request_index"] = 0
        payload["requests"][1]["request_id"] = "req-000"
    elif attack == "cross_project_path":
        payload["requests"][0]["canonical_path_and_query"] = "/v1/projects/kevin-staging-491315"
    elif attack == "non_null_body":
        payload["requests"][0]["canonical_request_body"] = {"unexpected": True}
        payload["requests"][0]["request_body_digest"] = _digest({"unexpected": True})
    elif attack == "mutating_method":
        payload["requests"][0]["http_method"] = "POST"
        payload["requests"][0]["api_method"] = "cloudresourcemanager.projects.setIamPolicy"
    elif attack == "wrong_identity_configuration":
        payload["requests"][0]["local_configuration_digest"] = "2" * 64
    elif attack == "extra_signature":
        payload["owner_signature"] = "not-allowed"
    elif attack == "invalid_time":
        payload["expires_at_ms"] = payload["issued_at_ms"]
    else:
        raise AssertionError(f"unknown attack: {attack}")
    _refresh_payload_digests(payload)
    errors = _generation_errors(payload)
    assert any(expected_error in error for error in errors)


def test_materializer_rejects_missing_extra_and_forbidden_inputs():
    missing = _private_fixture_inputs()
    missing.pop("owner_public_key_digest")
    with pytest.raises(ValueError, match="not_exact"):
        _materialize(missing)

    extra = _private_fixture_inputs()
    extra["project_numbers"] = ["100000000001"]
    with pytest.raises(ValueError, match="not_exact"):
        _materialize(extra)

    credential = _private_fixture_inputs()
    credential["credentials"] = "forbidden"
    with pytest.raises(ValueError, match="not_exact|forbidden"):
        _materialize(credential)


def test_v6_preserves_v5_and_explicitly_blocks_every_unqualified_action():
    for relative_path, expected_hash in _V5_HASHES.items():
        assert hashlib.sha256((_ROOT / relative_path).read_bytes()).hexdigest() == (expected_hash)

    package = _package()
    authority = package["authority"]
    assert authority["source_only_payload_generation_status"] == "not_generated"
    assert authority["owner_signature_status"] == "not_recorded"
    assert authority["owner_authorization_status"] == "not_recorded"
    assert authority["connected_inventory_status"] == "not_authorized"
    assert authority["mutation_status"] == "not_authorized"
    assert authority["execution_status"] == "not_authorized"
    assert authority["task_4_8_status"] == "sealed"

    assert package["dispatch_blockers"]["dispatch_eligibility"] is False
    assert package["dispatch_blockers"]["owner_signature_alone_would_authorize_dispatch"] is False
    assert package["dispatch_blockers"]["phase_two_through_six_action"] == (
        "generation_and_dispatch_not_authorized"
    )
    assert set(package["provenance"]["v5_findings_disposition"].values()) == {
        "unimplemented_blocks_every_dispatch",
        "unqualified_blocks_phases_two_through_six",
        "unimplemented_blocks_every_successor_phase",
        "unqualified_and_sealed",
    }

    guide = _GUIDE_PATH.read_text(encoding="utf-8")
    assert "Create no Google Cloud project" in guide
    assert "Do not authorize V6 itself" in guide
    assert "An owner signature alone would not make it dispatchable" in guide
    assert "keep Task 4.8 sealed" in guide
