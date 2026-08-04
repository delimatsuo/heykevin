"""Offline guards for the Phase 0.5 v4 environment-reconciliation amendment."""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v3 import (
    _schema_errors,
)


_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v4.json"
)
_SCHEMA_PATH = (
    _ROOT
    / "docs/security/"
    "voice-bakeoff-environment-reconciliation-phase-0-5-v4.schema.json"
)
_GUIDE_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v4.md"
)
_V2_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.json"
)
_V3_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v3.json"
)
_SOURCE_SHA = "2ed8ea7d1d7f338e84ddf08d5a50a714835e1533"
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
_V3_HASHES = {
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v3.json": (
        "a4ab352779fc6eb72c098622c7c462d5a7767358ed9663c31e53b52108b83b3d"
    ),
    (
        "docs/security/"
        "voice-bakeoff-environment-reconciliation-phase-0-5-v3.schema.json"
    ): "094b0ffdf3da572989630e516d1a708f545b46e5ca520ed48f1d9f4103375746",
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v3.md": (
        "1c95fb0d957e92ed5fae2103bf4a9756a58df4b4052be2e9cc3589457429073a"
    ),
    "tests/unit/test_voice_bakeoff_environment_reconciliation_phase_0_5_v3.py": (
        "bc6693455e4a21b3ba2287e73213294c08470bd48f3ef561ab5e887083dcb254"
    ),
}
_TEST_SIGNING_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
_SESSION_ID = "12345678-1234-4234-9234-123456789abc"


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _package() -> dict[str, Any]:
    return _load_json(_PACKAGE_PATH)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _public_key_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _sign_envelope(envelope: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in envelope.items() if key != "owner_signature"}
    signature = _TEST_SIGNING_KEY.sign(_canonical(unsigned))
    envelope["owner_signature"] = base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _composite_method_table() -> dict[str, dict[str, Any]]:
    v2 = _load_json(_V2_PATH)
    v3 = _load_json(_V3_PATH)
    table = deepcopy(v2["read_contract"]["methods"])
    for method_ref, method in v3["request_safety_amendments"][
        "method_overrides"
    ].items():
        table.pop(method["replaces_v2_method_ref"])
        table[method_ref] = deepcopy(method)
    for method_ref, method in v3["request_safety_amendments"][
        "method_additions"
    ].items():
        table[method_ref] = deepcopy(method)
    return table


def _effective_method(method_ref: str) -> dict[str, Any]:
    package = _package()
    method = _composite_method_table()[method_ref]
    response_fields = method.get(
        "exact_response_field_mask",
        method.get("response_field_mask"),
    )
    assert isinstance(response_fields, list)
    pagination = package["method_validation_contract"]["pagination_mapping"][
        method["pagination"]
    ]
    return {
        "api_method": method["api_method"],
        "endpoint": method["endpoint"],
        "http_method": method["http_method"],
        "path": method["path"],
        "request_body": method["request_body"],
        "response_field_mask": response_fields,
        "pagination": pagination,
        "raw_evidence_class": method["raw_evidence_class"],
    }


def _resolved_paths(
    path_template: str,
    *,
    project_id: str,
    project_number: str | None,
) -> set[str]:
    v2 = _load_json(_V2_PATH)
    path = path_template.replace("{EXACT_TARGET_PROJECT_ID}", project_id)
    if project_number is not None:
        path = path.replace("{EXACT_TARGET_PROJECT_NUMBER}", project_number)
    cloud_run_placeholder = "{EACH_EXACT_SOURCE_DECLARED_CLOUD_RUN_SERVICE}"
    if cloud_run_placeholder in path:
        return {
            path.replace(cloud_run_placeholder, resource_name)
            for resource_name in v2["read_contract"][
                "exact_source_declared_cloud_run_services"
            ]
        }
    return {path}


def _manifest(
    *,
    index: int,
    method_ref: str,
    project_id: str,
    project_number: str | None,
    resolved_path: str | None = None,
) -> dict[str, Any]:
    method = _effective_method(method_ref)
    paths = _resolved_paths(
        method["path"],
        project_id=project_id,
        project_number=project_number,
    )
    path = resolved_path or sorted(paths)[0]
    identity_ref = _QUERY_IDENTITIES[project_id]
    manifest = {
        "request_index": index,
        "request_id": f"req-{index:03d}",
        "method_ref": method_ref,
        "api_method": method["api_method"],
        "identity_ref": identity_ref,
        "local_configuration_digest": (
            "1" * 64
            if identity_ref == "organization_operator_identity"
            else "2" * 64
        ),
        "target_project_id": project_id,
        "target_project_number": project_number,
        "quota_project_id": project_id,
        "endpoint": method["endpoint"],
        "http_method": method["http_method"],
        "canonical_path_and_query": path,
        "request_body_digest": _digest(method["request_body"]),
        "response_field_mask": method["response_field_mask"],
        "pagination": method["pagination"],
        "raw_evidence_class": method["raw_evidence_class"],
    }
    manifest["canonical_request_digest"] = _digest(manifest)
    return manifest


def _project_id_manifests() -> list[dict[str, Any]]:
    return [
        _manifest(
            index=index,
            method_ref="project_identity_get",
            project_id=project_id,
            project_number=None,
        )
        for index, project_id in enumerate(_PROJECT_IDS)
    ]


def _envelope(
    *,
    phase: str = "project_id_binding",
    ordinal: int = 1,
    manifests: list[dict[str, Any]] | None = None,
    predecessor_receipt: str = "GENESIS",
    predecessor_allowlist: str = "GENESIS",
    cumulative_prior: int = 0,
) -> dict[str, Any]:
    package = _package()
    requests = manifests or _project_id_manifests()
    public_key = _TEST_SIGNING_KEY.public_key()
    envelope = {
        "envelope_version": 4,
        "inventory_session_id": _SESSION_ID,
        "phase": phase,
        "phase_ordinal": ordinal,
        "predecessor_phase_receipt_digest": predecessor_receipt,
        "predecessor_parameter_allowlist_digest": predecessor_allowlist,
        "cumulative_prior_request_count": cumulative_prior,
        "cumulative_request_count": cumulative_prior + len(requests),
        "source_sha": _SOURCE_SHA,
        "v2_json_digest": package["normative_contract_binding"]["v2_json_sha256"],
        "v3_json_digest": package["normative_contract_binding"]["v3_json_sha256"],
        "v4_normative_contract_digest": package["normative_contract_binding"][
            "normative_contract_digest_sha256"
        ],
        "composite_method_table_digest": package["normative_contract_binding"][
            "composite_method_table_sha256"
        ],
        "effective_access_contract_digest": package[
            "normative_contract_binding"
        ]["v3_effective_access_contract_sha256"],
        "owner_public_key_digest": _bytes_digest(_public_key_bytes(public_key)),
        "identity_configuration_digests": {
            "organization_operator_identity": "1" * 64,
            "isolated_bakeoff_operator_identity": "2" * 64,
        },
        "raw_evidence_custody_digest": "3" * 64,
        "envelope_nonce_seed": "4" * 64,
        "request_count": len(requests),
        "request_set_digest": _digest(requests),
        "requests": requests,
        "issued_at_ms": 1_000,
        "expires_at_ms": 2_000,
        "audit_and_quota_effects_acknowledged": True,
        "owner_signature": "",
    }
    _sign_envelope(envelope)
    return envelope


def _method_errors(request: dict[str, Any]) -> list[str]:
    method_ref = str(request.get("method_ref"))
    table = _composite_method_table()
    if method_ref not in table:
        return ["method_unknown"]
    method = _effective_method(method_ref)
    errors: list[str] = []
    exact_values = {
        "api_method": method["api_method"],
        "endpoint": method["endpoint"],
        "http_method": method["http_method"],
        "request_body_digest": _digest(method["request_body"]),
        "response_field_mask": method["response_field_mask"],
        "pagination": method["pagination"],
        "raw_evidence_class": method["raw_evidence_class"],
    }
    for field, expected in exact_values.items():
        if request.get(field) != expected:
            errors.append(f"method.{field}")
    paths = _resolved_paths(
        method["path"],
        project_id=str(request.get("target_project_id")),
        project_number=(
            str(request["target_project_number"])
            if request.get("target_project_number") is not None
            else None
        ),
    )
    if request.get("canonical_path_and_query") not in paths:
        errors.append("method.path")
    unsigned = {
        key: value
        for key, value in request.items()
        if key != "canonical_request_digest"
    }
    if request.get("canonical_request_digest") != _digest(unsigned):
        errors.append("method.request_digest")
    return errors


def _envelope_errors(
    envelope: dict[str, Any],
    *,
    verification_time_ms: int,
    seen_phase_keys: set[tuple[str, int]] | None = None,
) -> list[str]:
    package = _package()
    schema = _load_json(_SCHEMA_PATH)
    errors = _schema_errors(
        envelope,
        schema["$defs"]["phase_envelope_v4"],
        root=schema,
    )
    phases = {
        phase["phase"]: phase for phase in package["phase_contract"]["phases"]
    }
    phase = phases.get(str(envelope.get("phase")))
    requests = envelope.get("requests")
    if not isinstance(requests, list):
        return errors + ["requests"]
    if phase is None:
        errors.append("phase")
    else:
        if envelope.get("phase_ordinal") != phase["ordinal"]:
            errors.append("phase_ordinal")
        if not (
            phase["minimum_requests"]
            <= len(requests)
            <= phase["maximum_requests"]
        ):
            errors.append("phase_request_cap")
        if any(
            request.get("method_ref") not in phase["allowed_method_refs"]
            for request in requests
        ):
            errors.append("phase_method")
        if phase["ordinal"] == 1:
            if envelope.get("predecessor_phase_receipt_digest") != "GENESIS":
                errors.append("predecessor_receipt")
            if envelope.get("predecessor_parameter_allowlist_digest") != "GENESIS":
                errors.append("predecessor_allowlist")
            if envelope.get("cumulative_prior_request_count") != 0:
                errors.append("cumulative_prior")
            observed = {
                (request.get("target_project_id"), request.get("method_ref"))
                for request in requests
            }
            expected = {
                (project_id, "project_identity_get")
                for project_id in _PROJECT_IDS
            }
            if observed != expected:
                errors.append("phase_coverage")
        else:
            for field in (
                "predecessor_phase_receipt_digest",
                "predecessor_parameter_allowlist_digest",
            ):
                value = envelope.get(field)
                if not isinstance(value, str) or value == "GENESIS":
                    errors.append(field)

    if envelope.get("request_count") != len(requests):
        errors.append("request_count")
    if envelope.get("request_set_digest") != _digest(requests):
        errors.append("request_set_digest")
    prior = envelope.get("cumulative_prior_request_count")
    cumulative = envelope.get("cumulative_request_count")
    if not (
        isinstance(prior, int)
        and isinstance(cumulative, int)
        and cumulative == prior + len(requests)
        and cumulative <= package["phase_contract"]["caps"][
            "total_connected_requests"
        ]
    ):
        errors.append("cumulative_count")
    for index, request in enumerate(requests):
        errors.extend(
            f"request[{index}].{error}" for error in _method_errors(request)
        )
        if request.get("quota_project_id") != request.get("target_project_id"):
            errors.append(f"request[{index}].quota_project")
        identity_ref = _QUERY_IDENTITIES.get(str(request.get("target_project_id")))
        if request.get("identity_ref") != identity_ref:
            errors.append(f"request[{index}].identity")
        configs = envelope.get("identity_configuration_digests")
        if (
            not isinstance(configs, dict)
            or request.get("local_configuration_digest")
            != configs.get(identity_ref)
        ):
            errors.append(f"request[{index}].configuration")

    key = (
        str(envelope.get("inventory_session_id")),
        int(envelope.get("phase_ordinal", -1)),
    )
    if seen_phase_keys is not None and key in seen_phase_keys:
        errors.append("phase_duplicate")
    issued = envelope.get("issued_at_ms")
    expires = envelope.get("expires_at_ms")
    if not (
        isinstance(issued, int)
        and isinstance(expires, int)
        and issued < verification_time_ms < expires
    ):
        errors.append("timestamp")

    public_key = _TEST_SIGNING_KEY.public_key()
    if envelope.get("owner_public_key_digest") != _bytes_digest(
        _public_key_bytes(public_key)
    ):
        errors.append("owner_key")
    signature_text = envelope.get("owner_signature")
    if isinstance(signature_text, str):
        try:
            signature = base64.urlsafe_b64decode(signature_text + "==")
            unsigned = {
                key_name: value
                for key_name, value in envelope.items()
                if key_name != "owner_signature"
            }
            public_key.verify(signature, _canonical(unsigned))
        except (InvalidSignature, ValueError):
            errors.append("owner_signature")
    else:
        errors.append("owner_signature")
    return errors


def test_v4_root_schema_const_pins_every_amendment_subtree():
    package = _package()
    schema = _load_json(_SCHEMA_PATH)

    assert _schema_errors(package, schema) == []
    for subtree, replacement in (
        ("provenance", {}),
        ("status_model", {}),
        ("method_validation_contract", {}),
        ("phase_contract", {}),
        ("inventory_session_contract", {}),
        ("operator_targeting_contract", {}),
        ("transition_policy", {"connected_read_requirement": "authorized"}),
    ):
        changed = deepcopy(package)
        changed[subtree] = replacement
        assert "$:const" in _schema_errors(changed, schema)


def test_v4_preserves_exact_v3_and_remains_non_authorizing():
    package = _package()

    for path, expected in _V3_HASHES.items():
        assert _bytes_digest((_ROOT / path).read_bytes()) == expected
    assert package["source_binding"]["source_sha"] == _SOURCE_SHA
    assert package["source_binding"]["package_is_authorization"] is False
    assert package["authority"]["connected_inventory_status"] == "not_authorized"
    assert package["authority"]["mutation_status"] == "not_authorized"
    assert package["authority"]["execution_status"] == "not_authorized"
    assert package["authority"]["task_4_8_status"] == "sealed"
    assert package["authority"]["owner_signable_package_status"] == "not_generated"


def test_normative_contract_digest_covers_complete_v4_and_schema_binds_it():
    package = _package()
    schema = _load_json(_SCHEMA_PATH)
    digest_input = deepcopy(package)
    expected = digest_input["normative_contract_binding"].pop(
        "normative_contract_digest_sha256"
    )

    assert _digest(digest_input) == expected
    assert schema["const"] == package
    envelope_props = schema["$defs"]["phase_envelope_v4"]["properties"]
    assert envelope_props["v4_normative_contract_digest"]["const"] == expected


def test_composite_method_table_is_exact_complete_and_phase_partitioned():
    package = _package()
    table = _composite_method_table()
    phased = [
        method_ref
        for phase in package["phase_contract"]["phases"]
        for method_ref in phase["allowed_method_refs"]
    ]

    assert len(table) == 32
    assert _digest(table) == package["normative_contract_binding"][
        "composite_method_table_sha256"
    ]
    assert len(phased) == len(set(phased)) == 32
    assert set(phased) == set(table)


def test_six_phases_remove_bootstrap_and_same_phase_name_dependencies():
    phases = {
        phase["phase"]: phase for phase in _package()["phase_contract"]["phases"]
    }

    assert phases["project_id_binding"]["allowed_method_refs"] == [
        "project_identity_get"
    ]
    assert phases["project_id_binding"]["minimum_requests"] == 4
    assert phases["project_number_binding"]["predecessor"] == (
        "project_id_binding"
    )
    assert phases["project_number_binding"]["allowed_method_refs"] == [
        "project_provenance_get",
        "project_ancestry_get",
        "project_billing_get",
    ]
    assert "pab_policy_list" in phases["access_control_discovery"][
        "allowed_method_refs"
    ]
    assert "pab_policy_binding_search" not in phases[
        "access_control_discovery"
    ]["allowed_method_refs"]
    assert "pab_policy_binding_search" in phases["access_control_detail"][
        "allowed_method_refs"
    ]


def test_access_detail_static_floor_and_worst_case_fit_phase_cap():
    contract = _package()["phase_contract"]
    phases = {phase["phase"]: phase for phase in contract["phases"]}
    floor = contract["static_effective_access_request_floor"]
    budget = contract["access_detail_worst_case_budget"]
    discovery_budget = contract["access_discovery_worst_case_budget"]

    assert floor == {
        "data_tuples": 48,
        "cloud_run_tuples": 16,
        "project_level_tuples": 24,
        "total": 88,
    }
    assert phases["access_control_detail"]["minimum_requests"] == 88
    assert budget["total"] == 320
    assert budget["phase_maximum"] == 384
    assert budget["total"] <= phases["access_control_detail"]["maximum_requests"]
    assert discovery_budget["total"] == 52
    assert discovery_budget["total"] <= phases["access_control_discovery"][
        "maximum_requests"
    ]
    assert sum(
        phase["maximum_requests"] for phase in contract["phases"]
    ) == contract["caps"]["total_connected_requests"] == 592


def test_project_id_envelope_is_closed_exact_signed_and_semantically_valid():
    envelope = _envelope()
    schema = _load_json(_SCHEMA_PATH)

    assert _schema_errors(
        envelope,
        schema["$defs"]["phase_envelope_v4"],
        root=schema,
    ) == []
    assert _envelope_errors(envelope, verification_time_ms=1_500) == []
    assert all(
        request["target_project_number"] is None
        for request in envelope["requests"]
    )
    number_bound_too_early = deepcopy(envelope)
    number_bound_too_early["requests"][0]["target_project_number"] = "123456"
    assert any(
        "target_project_number:const" in error
        for error in _schema_errors(
            number_bound_too_early,
            schema["$defs"]["phase_envelope_v4"],
            root=schema,
        )
    )
    wrong_method = deepcopy(envelope)
    wrong_method["requests"][0]["method_ref"] = "project_provenance_get"
    assert any(
        "method_ref:enum" in error
        for error in _schema_errors(
            wrong_method,
            schema["$defs"]["phase_envelope_v4"],
            root=schema,
        )
    )


def test_v4_method_validator_rejects_signed_v3_label_with_mutating_request():
    v2 = _load_json(_V2_PATH)
    services = v2["read_contract"]["exact_source_declared_cloud_run_services"]
    manifests = [
        _manifest(
            index=index,
            method_ref="cloud_run_service_get_v3",
            project_id="kevin-491315",
            project_number="123456",
            resolved_path=(
                "/v2/"
                f"{service}?fields=createTime,etag,generation,name,"
                "observedGeneration,uid,updateTime"
            ),
        )
        for index, service in enumerate(services)
    ]
    malicious = deepcopy(manifests)
    malicious[0].update(
        {
            "api_method": "cloudresourcemanager.projects.setIamPolicy",
            "endpoint": "cloudresourcemanager.googleapis.com",
            "http_method": "POST",
            "canonical_path_and_query": (
                "/v1/projects/kevin-491315:setIamPolicy"
            ),
            "request_body_digest": "f" * 64,
            "response_field_mask": ["bindings"],
            "raw_evidence_class": "payload_safe",
        }
    )
    unsigned_request = {
        key: value
        for key, value in malicious[0].items()
        if key != "canonical_request_digest"
    }
    malicious[0]["canonical_request_digest"] = _digest(unsigned_request)
    envelope = _envelope(
        phase="metadata_detail",
        ordinal=4,
        manifests=malicious,
        predecessor_receipt="5" * 64,
        predecessor_allowlist="6" * 64,
        cumulative_prior=112,
    )

    errors = _envelope_errors(envelope, verification_time_ms=1_500)
    assert any("method.api_method" in error for error in errors)
    assert any("method.endpoint" in error for error in errors)
    assert any("method.http_method" in error for error in errors)
    assert any("method.path" in error for error in errors)
    assert any("method.response_field_mask" in error for error in errors)
    assert any("method.raw_evidence_class" in error for error in errors)


def test_session_sequence_rejects_wrong_ordinal_predecessor_counter_and_replay():
    base = _envelope()
    cases: list[tuple[str, dict[str, Any], set[tuple[str, int]] | None]] = []

    wrong_ordinal = deepcopy(base)
    wrong_ordinal["phase_ordinal"] = 2
    _sign_envelope(wrong_ordinal)
    cases.append(("phase_ordinal", wrong_ordinal, None))

    wrong_predecessor = deepcopy(base)
    wrong_predecessor["predecessor_phase_receipt_digest"] = "7" * 64
    _sign_envelope(wrong_predecessor)
    cases.append(("predecessor_receipt", wrong_predecessor, None))

    wrong_counter = deepcopy(base)
    wrong_counter["cumulative_request_count"] = 5
    _sign_envelope(wrong_counter)
    cases.append(("cumulative_count", wrong_counter, None))

    replay = deepcopy(base)
    cases.append(("phase_duplicate", replay, {(_SESSION_ID, 1)}))

    for expected, envelope, seen in cases:
        errors = _envelope_errors(
            envelope,
            verification_time_ms=1_500,
            seen_phase_keys=seen,
        )
        assert expected in errors


def test_status_model_and_all_operator_fixtures_are_canonical_and_dominant():
    package = _package()
    status = package["status_model"]
    operator = package["operator_targeting_contract"]
    required = set(operator["required_banner_fields"])
    precedence = status["dominant_display_precedence"]

    assert len(required) == 25
    assert "response_field_mask" in required
    assert "request_field_mask" not in required
    assert len(operator["status_examples"]) == 4
    for example in operator["status_examples"]:
        fields = example["fields"]
        assert set(fields) == required
        assert fields["inventory_status"] in status["vocabulary"][
            "inventory_status"
        ]
        assert fields["governance_status"] in status["vocabulary"][
            "governance_status"
        ]
        assert fields["review_status"] in status["vocabulary"]["review_status"]
        assert fields["connected_read_status"] in status["vocabulary"][
            "connected_read_status"
        ]
        assert fields["mutation_status"] in status["vocabulary"][
            "mutation_status"
        ]
        assert fields["execution_status"] in status["vocabulary"][
            "execution_status"
        ]
        present = {
            fields["inventory_status"],
            fields["governance_status"],
            fields["review_status"],
            fields["connected_read_status"],
            fields["mutation_status"],
            fields["execution_status"],
        }
        computed = next(value for value in precedence if value in present)
        assert fields["dominant_status"] == computed == "execution_not_authorized"
        assert "EXECUTION SEALED" in example["dominant_banner"]


def test_phase_receipt_schema_is_closed_digest_bound_and_code_only():
    package = _package()
    schema = _load_json(_SCHEMA_PATH)
    receipt = {
        "receipt_schema_version": 4,
        "inventory_session_id": _SESSION_ID,
        "phase": "project_id_binding",
        "phase_ordinal": 1,
        "predecessor_phase_receipt_digest": "GENESIS",
        "predecessor_parameter_allowlist_digest": "GENESIS",
        "derived_parameter_allowlist_digest": "1" * 64,
        "cumulative_request_count": 4,
        "source_sha": _SOURCE_SHA,
        "v2_json_digest": package["normative_contract_binding"]["v2_json_sha256"],
        "v3_json_digest": package["normative_contract_binding"]["v3_json_sha256"],
        "v4_normative_contract_digest": package["normative_contract_binding"][
            "normative_contract_digest_sha256"
        ],
        "composite_method_table_digest": package["normative_contract_binding"][
            "composite_method_table_sha256"
        ],
        "effective_access_contract_digest": package[
            "normative_contract_binding"
        ]["v3_effective_access_contract_sha256"],
        "phase_envelope_digest": "2" * 64,
        "request_count": 4,
        "request_set_digest": "3" * 64,
        "request_receipt_set_digest": "4" * 64,
        "phase_once_ledger_entry_digest": "5" * 64,
        "issued_at_ms": 1_000,
        "completed_at_ms": 1_100,
        "expires_at_ms": 2_000,
        "error_codes": [],
        "result": "pass",
        "receipt_public_key_digest": "6" * 64,
        "receipt_signature": "A" * 86,
    }

    receipt_schema = schema["$defs"]["phase_receipt_v4"]
    assert _schema_errors(receipt, receipt_schema, root=schema) == []
    extra = deepcopy(receipt)
    extra["raw_error"] = "user@example.com"
    assert any(
        error.endswith(":additional")
        for error in _schema_errors(extra, receipt_schema, root=schema)
    )
    raw_error = deepcopy(receipt)
    raw_error["result"] = "incomplete"
    raw_error["error_codes"] = ["raw user@example.com"]
    assert any(
        error.endswith(":enum")
        for error in _schema_errors(raw_error, receipt_schema, root=schema)
    )


def test_transition_and_guide_keep_projects_and_connected_actions_sealed():
    package = _package()
    guide = _GUIDE_PATH.read_text(encoding="utf-8")
    transition = package["transition_policy"]

    assert transition["environment_recommendation"] == (
        "create_no_project_retain_all_four_exact_projects_frozen"
    )
    assert transition["after_v4_review"].startswith(
        "generate_but_do_not_execute"
    )
    assert transition["incomplete_action"] == (
        "stop_retain_frozen_no_duplicate_no_fallback"
    )
    assert transition["mutation_execution_and_task_4_8"].startswith(
        "remain_sealed"
    )
    for phrase in (
        "Create no Google Cloud project.",
        "V4 defines these checks; it does not implement",
        "Do not authorize V4 itself.",
        "keep Task 4.8 sealed",
    ):
        assert phrase in guide
