"""Offline guards for the Phase 0.5 v5 environment-reconciliation amendment."""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
import pytest

from tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v3 import (
    _schema_errors,
)
from tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v4 import (
    _PROJECT_IDS,
    _QUERY_IDENTITIES,
    _SESSION_ID,
    _SOURCE_SHA,
    _V2_PATH,
    _canonical,
    _digest,
    _manifest,
    _method_errors,
    _public_key_bytes,
)


_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_PATH = _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v5.json"
_SCHEMA_PATH = (
    _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v5.schema.json"
)
_GUIDE_PATH = _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v5.md"
_V4_PATH = _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v4.json"
_V4_HASHES = {
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v4.json": (
        "883246c06309b575f5276be2974ab3b34ac9790bf3511c7ac8abacf0133f731d"
    ),
    (
        "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v4.schema.json"
    ): "4837d628aaead05bd16e3783cfd722f1741a7704a3980fd5ba2aaf20171f62ad",
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v4.md": (
        "f3c9d4af70a874248e069416f348ea22d05e5cc88b917edb4e01d2c6fc5d0c75"
    ),
    "tests/unit/test_voice_bakeoff_environment_reconciliation_phase_0_5_v4.py": (
        "f037027d5ca31ac699fc9a551cf4f240e8d6805d9bb81765d28d3a216fd0d47f"
    ),
}
_PHASES = (
    "project_id_binding",
    "project_number_binding",
    "metadata_discovery",
    "metadata_detail",
    "access_control_discovery",
    "access_control_detail",
)
_NUMBERS = {
    project_id: str(100_000_000_001 + index) for index, project_id in enumerate(_PROJECT_IDS)
}
_OWNER_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(65, 97)))
_RECEIPT_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(97, 129)))
_ENVELOPE_DOMAIN = b"hey-kevin/voice-bakeoff/environment-inventory/v5/envelope"
_RECEIPT_DOMAIN = b"hey-kevin/voice-bakeoff/environment-inventory/v5/receipt"


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _package() -> dict[str, Any]:
    return _load_json(_PACKAGE_PATH)


def _schema() -> dict[str, Any]:
    return _load_json(_SCHEMA_PATH)


def _bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sign(
    value: dict[str, Any],
    *,
    key: Ed25519PrivateKey,
    signature_field: str,
    domain: bytes,
) -> None:
    unsigned = {
        field: field_value for field, field_value in value.items() if field != signature_field
    }
    signature = key.sign(domain + b"\0" + _canonical(unsigned))
    value[signature_field] = base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _signature_is_valid(
    value: dict[str, Any],
    *,
    key: Ed25519PublicKey,
    signature_field: str,
    domain: bytes,
) -> bool:
    signature_text = value.get(signature_field)
    if not isinstance(signature_text, str):
        return False
    unsigned = {
        field: field_value for field, field_value in value.items() if field != signature_field
    }
    try:
        signature = base64.urlsafe_b64decode(signature_text + "==")
        key.verify(signature, domain + b"\0" + _canonical(unsigned))
    except (InvalidSignature, ValueError):
        return False
    return True


def _parameter_entry(project_id: str, value: str) -> dict[str, str]:
    return {"project_id": project_id, "value": value}


def _private_allowlist() -> dict[str, Any]:
    package = _package()
    v2 = _load_json(_V2_PATH)
    placeholders = package["private_parameter_allowlist_contract"]["exact_placeholder_keys"]
    parameters: dict[str, list[dict[str, Any]]] = {placeholder: [] for placeholder in placeholders}
    parameters["EXACT_TARGET_PROJECT_ID"] = [
        _parameter_entry(project_id, project_id) for project_id in _PROJECT_IDS
    ]
    parameters["EXACT_TARGET_PROJECT_NUMBER"] = [
        _parameter_entry(project_id, _NUMBERS[project_id]) for project_id in _PROJECT_IDS
    ]
    parameters["EACH_EXACT_ALLOWLISTED_SERVICE_NAME"] = [
        _parameter_entry(project_id, service_name)
        for project_id in _PROJECT_IDS
        for service_name in v2["read_contract"]["exact_service_name_allowlist"]
    ]
    parameters["EXACT_DETAILED_ASSET_TYPE_ALLOWLIST"] = [
        _parameter_entry(project_id, asset_type)
        for project_id in _PROJECT_IDS
        for asset_type in v2["read_contract"]["exact_detailed_asset_type_allowlist"]
    ]
    parameters["EACH_EXACT_SOURCE_DECLARED_CLOUD_RUN_SERVICE"] = [
        _parameter_entry("kevin-491315", service)
        for service in v2["read_contract"]["exact_source_declared_cloud_run_services"]
    ]
    return {
        "allowlist_version": 5,
        "inventory_session_id": _SESSION_ID,
        "producing_phase": "project_id_binding",
        "producing_phase_ordinal": 1,
        "source_sha": _SOURCE_SHA,
        "v5_normative_contract_digest": package["normative_contract_binding"][
            "normative_contract_digest_sha256"
        ],
        "project_bindings": [
            {
                "project_id": project_id,
                "project_number": _NUMBERS[project_id],
                "query_identity_ref": _QUERY_IDENTITIES[project_id],
                "local_configuration_digest": (
                    "1" * 64
                    if _QUERY_IDENTITIES[project_id] == "organization_operator_identity"
                    else "2" * 64
                ),
                "immediate_parent_digest": _digest({"project_id": project_id, "parent": "fixture"}),
                "ancestry_digest": None,
            }
            for project_id in _PROJECT_IDS
        ],
        "parameters_by_placeholder": parameters,
    }


def _allowlist_semantic_errors(allowlist: dict[str, Any]) -> list[str]:
    errors = _schema_errors(
        allowlist,
        _schema()["$defs"]["private_parameter_allowlist_v5"],
        root=_schema(),
    )
    bindings = allowlist.get("project_bindings")
    parameters = allowlist.get("parameters_by_placeholder")
    if not isinstance(bindings, list) or not isinstance(parameters, dict):
        return errors + ["allowlist_structure"]

    binding_ids = [binding.get("project_id") for binding in bindings]
    binding_numbers = [binding.get("project_number") for binding in bindings]
    if tuple(binding_ids) != _PROJECT_IDS or len(set(binding_ids)) != 4:
        errors.append("project_bindings")
    if len(set(binding_numbers)) != 4:
        errors.append("project_numbers")

    expected_ids = [_parameter_entry(project_id, project_id) for project_id in _PROJECT_IDS]
    expected_numbers = [
        _parameter_entry(binding["project_id"], binding["project_number"]) for binding in bindings
    ]
    if parameters.get("EXACT_TARGET_PROJECT_ID") != expected_ids:
        errors.append("target_project_id_entries")
    if parameters.get("EXACT_TARGET_PROJECT_NUMBER") != expected_numbers:
        errors.append("target_project_number_entries")

    v2 = _load_json(_V2_PATH)
    expected_services = [
        _parameter_entry(project_id, service_name)
        for project_id in _PROJECT_IDS
        for service_name in v2["read_contract"]["exact_service_name_allowlist"]
    ]
    if parameters.get("EACH_EXACT_ALLOWLISTED_SERVICE_NAME") != expected_services:
        errors.append("service_name_entries")
    expected_asset_types = [
        _parameter_entry(project_id, asset_type)
        for project_id in _PROJECT_IDS
        for asset_type in v2["read_contract"]["exact_detailed_asset_type_allowlist"]
    ]
    if parameters.get("EXACT_DETAILED_ASSET_TYPE_ALLOWLIST") != expected_asset_types:
        errors.append("asset_type_entries")
    expected_cloud_run = [
        _parameter_entry("kevin-491315", service)
        for service in v2["read_contract"]["exact_source_declared_cloud_run_services"]
    ]
    if parameters.get("EACH_EXACT_SOURCE_DECLARED_CLOUD_RUN_SERVICE") != expected_cloud_run:
        errors.append("cloud_run_entries")

    for placeholder, entries in parameters.items():
        if not isinstance(entries, list):
            errors.append(f"{placeholder}.type")
            continue
        canonical_entries = [_canonical(entry) for entry in entries]
        if len(canonical_entries) != len(set(canonical_entries)):
            errors.append(f"{placeholder}.duplicate")
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("project_id") not in _PROJECT_IDS:
                errors.append(f"{placeholder}.project")
    return errors


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


def _project_number_manifests(
    allowlist: dict[str, Any],
) -> list[dict[str, Any]]:
    numbers = {
        entry["project_id"]: entry["value"]
        for entry in allowlist["parameters_by_placeholder"]["EXACT_TARGET_PROJECT_NUMBER"]
    }
    manifests: list[dict[str, Any]] = []
    for project_id in _PROJECT_IDS:
        for method_ref in (
            "project_provenance_get",
            "project_ancestry_get",
            "project_billing_get",
        ):
            manifests.append(
                _manifest(
                    index=len(manifests),
                    method_ref=method_ref,
                    project_id=project_id,
                    project_number=numbers[project_id],
                )
            )
    return manifests


def _envelope(
    *,
    phase: str = "project_id_binding",
    allowlist: dict[str, Any] | None = None,
    predecessor_receipt_digest: str = "GENESIS",
    cumulative_prior: int = 0,
) -> dict[str, Any]:
    package = _package()
    if phase == "project_id_binding":
        ordinal = 1
        requests = _project_id_manifests()
        predecessor_allowlist_digest = "GENESIS"
    else:
        assert phase == "project_number_binding"
        assert allowlist is not None
        ordinal = 2
        requests = _project_number_manifests(allowlist)
        predecessor_allowlist_digest = _digest(allowlist)

    envelope = {
        "envelope_version": 5,
        "inventory_session_id": _SESSION_ID,
        "phase": phase,
        "phase_ordinal": ordinal,
        "predecessor_phase_receipt_digest": predecessor_receipt_digest,
        "predecessor_parameter_allowlist_digest": (predecessor_allowlist_digest),
        "cumulative_prior_request_count": cumulative_prior,
        "cumulative_request_count": cumulative_prior + len(requests),
        "source_sha": _SOURCE_SHA,
        "v2_json_digest": package["normative_contract_binding"]["v2_json_sha256"],
        "v3_json_digest": package["normative_contract_binding"]["v3_json_sha256"],
        "v4_json_digest": package["normative_contract_binding"]["v4_json_sha256"],
        "v5_normative_contract_digest": package["normative_contract_binding"][
            "normative_contract_digest_sha256"
        ],
        "composite_method_table_digest": package["normative_contract_binding"][
            "composite_method_table_sha256"
        ],
        "effective_access_contract_digest": package["normative_contract_binding"][
            "effective_access_contract_sha256"
        ],
        "owner_public_key_digest": _bytes_digest(_public_key_bytes(_OWNER_KEY.public_key())),
        "identity_configuration_digests": {
            "organization_operator_identity": "1" * 64,
            "isolated_bakeoff_operator_identity": "2" * 64,
        },
        "raw_evidence_custody_digest": "3" * 64,
        "envelope_nonce_seed": "4" * 64,
        "request_count": len(requests),
        "request_set_digest": _digest(requests),
        "requests": requests,
        "issued_at_ms": 2_000 if ordinal == 2 else 1_000,
        "expires_at_ms": 4_000 if ordinal == 2 else 2_000,
        "audit_and_quota_effects_acknowledged": True,
        "owner_signature": "",
    }
    _sign(
        envelope,
        key=_OWNER_KEY,
        signature_field="owner_signature",
        domain=_ENVELOPE_DOMAIN,
    )
    return envelope


def _receipt(
    envelope: dict[str, Any],
    *,
    derived_allowlist: dict[str, Any],
) -> dict[str, Any]:
    package = _package()
    public_key = _RECEIPT_KEY.public_key()
    receipt = {
        "receipt_schema_version": 5,
        "inventory_session_id": envelope["inventory_session_id"],
        "phase": envelope["phase"],
        "phase_ordinal": envelope["phase_ordinal"],
        "predecessor_phase_receipt_digest": envelope["predecessor_phase_receipt_digest"],
        "predecessor_parameter_allowlist_digest": envelope[
            "predecessor_parameter_allowlist_digest"
        ],
        "derived_parameter_allowlist_digest": _digest(derived_allowlist),
        "cumulative_request_count": envelope["cumulative_request_count"],
        "source_sha": _SOURCE_SHA,
        "v2_json_digest": package["normative_contract_binding"]["v2_json_sha256"],
        "v3_json_digest": package["normative_contract_binding"]["v3_json_sha256"],
        "v4_json_digest": package["normative_contract_binding"]["v4_json_sha256"],
        "v5_normative_contract_digest": package["normative_contract_binding"][
            "normative_contract_digest_sha256"
        ],
        "composite_method_table_digest": package["normative_contract_binding"][
            "composite_method_table_sha256"
        ],
        "effective_access_contract_digest": package["normative_contract_binding"][
            "effective_access_contract_sha256"
        ],
        "phase_envelope_digest": _digest(envelope),
        "request_count": envelope["request_count"],
        "request_set_digest": envelope["request_set_digest"],
        "request_receipt_set_digest": _digest(
            [
                {"request_id": request["request_id"], "result": "pass"}
                for request in envelope["requests"]
            ]
        ),
        "phase_once_ledger_entry_digest": _digest(
            {
                "inventory_session_id": envelope["inventory_session_id"],
                "phase_ordinal": envelope["phase_ordinal"],
                "consumed": True,
            }
        ),
        "issued_at_ms": 1_000,
        "completed_at_ms": 1_500,
        "expires_at_ms": 5_000,
        "error_codes": [],
        "result": "pass",
        "receipt_public_key_digest": _bytes_digest(_public_key_bytes(public_key)),
        "receipt_signature": "",
    }
    _sign(
        receipt,
        key=_RECEIPT_KEY,
        signature_field="receipt_signature",
        domain=_RECEIPT_DOMAIN,
    )
    return receipt


def _ledger_snapshot(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "inventory_session_id": receipt["inventory_session_id"],
            "phase_ordinal": receipt["phase_ordinal"],
            "receipt_digest": _digest(receipt),
            "consumed": True,
        }
    ]


def _resign_envelope(envelope: dict[str, Any]) -> None:
    envelope["request_count"] = len(envelope["requests"])
    envelope["request_set_digest"] = _digest(envelope["requests"])
    envelope["cumulative_request_count"] = (
        envelope["cumulative_prior_request_count"] + envelope["request_count"]
    )
    _sign(
        envelope,
        key=_OWNER_KEY,
        signature_field="owner_signature",
        domain=_ENVELOPE_DOMAIN,
    )


def _verification_errors(
    envelope: dict[str, Any],
    *,
    verification_time_ms: int,
    predecessor_receipt: dict[str, Any] | None = None,
    predecessor_allowlist: dict[str, Any] | None = None,
    ledger_snapshot: list[dict[str, Any]] | None = None,
) -> list[str]:
    schema = _schema()
    errors = _schema_errors(
        envelope,
        schema["$defs"]["phase_envelope_v5"],
        root=schema,
    )
    phase = envelope.get("phase")
    ordinal = envelope.get("phase_ordinal")
    requests = envelope.get("requests")
    if phase not in _PHASES or not isinstance(ordinal, int):
        return errors + ["phase"]
    if not isinstance(requests, list):
        return errors + ["requests"]
    if ordinal != _PHASES.index(phase) + 1:
        errors.append("phase_ordinal")

    if not _signature_is_valid(
        envelope,
        key=_OWNER_KEY.public_key(),
        signature_field="owner_signature",
        domain=_ENVELOPE_DOMAIN,
    ):
        errors.append("owner_signature")
    if envelope.get("owner_public_key_digest") != _bytes_digest(
        _public_key_bytes(_OWNER_KEY.public_key())
    ):
        errors.append("owner_key")
    if not (
        isinstance(envelope.get("issued_at_ms"), int)
        and isinstance(envelope.get("expires_at_ms"), int)
        and envelope["issued_at_ms"] < verification_time_ms < envelope["expires_at_ms"]
    ):
        errors.append("envelope_time")

    if phase == "project_id_binding":
        expected = _project_id_manifests()
        if any(
            value != "GENESIS"
            for value in (
                envelope.get("predecessor_phase_receipt_digest"),
                envelope.get("predecessor_parameter_allowlist_digest"),
            )
        ):
            errors.append("genesis")
        if envelope.get("cumulative_prior_request_count") != 0:
            errors.append("cumulative_prior")
    else:
        if predecessor_receipt is None or predecessor_allowlist is None or ledger_snapshot is None:
            return errors + ["predecessor_inputs"]
        errors.extend(_allowlist_semantic_errors(predecessor_allowlist))
        errors.extend(
            _schema_errors(
                predecessor_receipt,
                schema["$defs"]["phase_receipt_v5"],
                root=schema,
            )
        )
        if predecessor_receipt.get("receipt_public_key_digest") != (
            _bytes_digest(_public_key_bytes(_RECEIPT_KEY.public_key()))
        ):
            errors.append("receipt_key")
        if not _signature_is_valid(
            predecessor_receipt,
            key=_RECEIPT_KEY.public_key(),
            signature_field="receipt_signature",
            domain=_RECEIPT_DOMAIN,
        ):
            errors.append("receipt_signature")
        if _digest(predecessor_receipt) != envelope.get("predecessor_phase_receipt_digest"):
            errors.append("predecessor_receipt_digest")
        if (
            predecessor_receipt.get("result") != "pass"
            or predecessor_receipt.get("error_codes") != []
        ):
            errors.append("predecessor_result")
        if predecessor_receipt.get("inventory_session_id") != envelope.get("inventory_session_id"):
            errors.append("predecessor_session")
        if predecessor_receipt.get("phase_ordinal") != ordinal - 1:
            errors.append("predecessor_ordinal")
        if predecessor_receipt.get("phase") != _PHASES[ordinal - 2]:
            errors.append("predecessor_phase")
        for field in (
            "source_sha",
            "v2_json_digest",
            "v3_json_digest",
            "v4_json_digest",
            "v5_normative_contract_digest",
            "composite_method_table_digest",
            "effective_access_contract_digest",
        ):
            if predecessor_receipt.get(field) != envelope.get(field):
                errors.append(f"predecessor_{field}")
        if not (
            predecessor_receipt.get("issued_at_ms")
            <= predecessor_receipt.get("completed_at_ms")
            < predecessor_receipt.get("expires_at_ms")
            and verification_time_ms < predecessor_receipt.get("expires_at_ms")
        ):
            errors.append("predecessor_time")
        if predecessor_receipt.get("cumulative_request_count") != envelope.get(
            "cumulative_prior_request_count"
        ):
            errors.append("predecessor_cumulative")

        allowlist_digest = _digest(predecessor_allowlist)
        if allowlist_digest != predecessor_receipt.get("derived_parameter_allowlist_digest"):
            errors.append("receipt_allowlist_digest")
        if allowlist_digest != envelope.get("predecessor_parameter_allowlist_digest"):
            errors.append("envelope_allowlist_digest")
        if (
            predecessor_allowlist.get("inventory_session_id")
            != predecessor_receipt.get("inventory_session_id")
            or predecessor_allowlist.get("producing_phase") != predecessor_receipt.get("phase")
            or predecessor_allowlist.get("producing_phase_ordinal")
            != predecessor_receipt.get("phase_ordinal")
            or predecessor_allowlist.get("source_sha") != predecessor_receipt.get("source_sha")
            or predecessor_allowlist.get("v5_normative_contract_digest")
            != predecessor_receipt.get("v5_normative_contract_digest")
        ):
            errors.append("allowlist_binding")

        matching_ledger_entries = [
            entry
            for entry in ledger_snapshot
            if entry.get("inventory_session_id") == predecessor_receipt.get("inventory_session_id")
            and entry.get("phase_ordinal") == predecessor_receipt.get("phase_ordinal")
            and entry.get("receipt_digest") == _digest(predecessor_receipt)
            and entry.get("consumed") is True
        ]
        if len(matching_ledger_entries) != 1 or len(ledger_snapshot) != 1:
            errors.append("predecessor_ledger")

        if phase == "project_number_binding":
            expected = _project_number_manifests(predecessor_allowlist)
        else:
            return errors + ["unimplemented_fixture_phase"]

    if _canonical(requests) != _canonical(expected):
        errors.append("expected_manifest")
    if envelope.get("request_count") != len(requests):
        errors.append("request_count")
    if envelope.get("request_set_digest") != _digest(requests):
        errors.append("request_set_digest")
    if envelope.get("cumulative_request_count") != (
        envelope.get("cumulative_prior_request_count", -1) + len(requests)
    ):
        errors.append("cumulative_count")

    request_ids: list[object] = []
    request_digests: list[object] = []
    bindings_by_id = (
        {binding["project_id"]: binding for binding in predecessor_allowlist["project_bindings"]}
        if predecessor_allowlist is not None
        else {}
    )
    for position, request in enumerate(requests):
        if request.get("request_index") != position:
            errors.append(f"request[{position}].index")
        if request.get("request_id") != f"req-{position:03d}":
            errors.append(f"request[{position}].id")
        request_ids.append(request.get("request_id"))
        request_digests.append(request.get("canonical_request_digest"))
        if "{" in str(request.get("canonical_path_and_query")) or "}" in str(
            request.get("canonical_path_and_query")
        ):
            errors.append(f"request[{position}].placeholder")
        errors.extend(f"request[{position}].{error}" for error in _method_errors(request))
        project_id = request.get("target_project_id")
        identity_ref = _QUERY_IDENTITIES.get(str(project_id))
        if request.get("quota_project_id") != project_id:
            errors.append(f"request[{position}].quota")
        if request.get("identity_ref") != identity_ref:
            errors.append(f"request[{position}].identity")
        configs = envelope.get("identity_configuration_digests", {})
        if request.get("local_configuration_digest") != configs.get(identity_ref):
            errors.append(f"request[{position}].configuration")
        if ordinal > 1:
            binding = bindings_by_id.get(project_id)
            if (
                binding is None
                or request.get("target_project_number") != binding.get("project_number")
                or request.get("identity_ref") != binding.get("query_identity_ref")
                or request.get("local_configuration_digest")
                != binding.get("local_configuration_digest")
            ):
                errors.append(f"request[{position}].project_binding")

    if len(request_ids) != len(set(request_ids)):
        errors.append("request_ids_unique")
    if len(request_digests) != len(set(request_digests)):
        errors.append("request_digests_unique")
    return errors


def _phase_two_fixture() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
]:
    allowlist = _private_allowlist()
    phase_one = _envelope()
    receipt = _receipt(phase_one, derived_allowlist=allowlist)
    phase_two = _envelope(
        phase="project_number_binding",
        allowlist=allowlist,
        predecessor_receipt_digest=_digest(receipt),
        cumulative_prior=4,
    )
    return phase_two, receipt, allowlist, _ledger_snapshot(receipt)


def test_v5_root_schema_digest_and_critical_subtrees_are_exact():
    package = _package()
    schema = _schema()
    assert _schema_errors(package, schema, root=schema) == []

    digest_input = deepcopy(package)
    digest_input["normative_contract_binding"].pop("normative_contract_digest_sha256")
    assert (
        _digest(digest_input)
        == package["normative_contract_binding"]["normative_contract_digest_sha256"]
    )
    assert schema["const"] == package

    for subtree in (
        "provenance",
        "authority",
        "normative_contract_binding",
        "private_parameter_allowlist_contract",
        "predecessor_receipt_verification_contract",
        "manifest_relational_verification_contract",
        "phase_coverage_algorithms",
        "v5_envelope_and_receipt_binding",
        "transition_policy",
    ):
        changed = deepcopy(package)
        changed[subtree]["unexpected_mutation"] = True
        assert _schema_errors(changed, schema, root=schema)


def test_v5_preserves_v4_and_keeps_all_connected_authority_sealed():
    for relative_path, expected in _V4_HASHES.items():
        assert hashlib.sha256((_ROOT / relative_path).read_bytes()).hexdigest() == (expected)

    package = _package()
    authority = package["authority"]
    assert authority["connected_inventory_status"] == "not_authorized"
    assert authority["mutation_status"] == "not_authorized"
    assert authority["execution_status"] == "not_authorized"
    assert authority["task_4_8_status"] == "sealed"
    assert authority["owner_signable_package_status"] == "not_generated"
    assert package["transition_policy"]["environment_recommendation"] == (
        "create_no_project_retain_all_four_exact_projects_frozen"
    )

    guide = _GUIDE_PATH.read_text(encoding="utf-8")
    assert "Create no Google Cloud project" in guide
    assert "Do not authorize V5 itself" in guide
    assert "make no connected inventory request" in guide
    assert "keep Task 4.8 sealed" in guide


def test_private_allowlist_is_closed_target_scoped_and_semantically_exact():
    allowlist = _private_allowlist()
    assert _allowlist_semantic_errors(allowlist) == []

    wrong_identity = deepcopy(allowlist)
    wrong_identity["project_bindings"][0]["query_identity_ref"] = (
        "isolated_bakeoff_operator_identity"
    )
    assert _allowlist_semantic_errors(wrong_identity)

    duplicate_binding = deepcopy(allowlist)
    duplicate_binding["project_bindings"][3]["project_id"] = _PROJECT_IDS[0]
    assert "project_bindings" in _allowlist_semantic_errors(duplicate_binding)

    wrong_target_pair = deepcopy(allowlist)
    wrong_target_pair["parameters_by_placeholder"]["EXACT_TARGET_PROJECT_NUMBER"][0][
        "project_id"
    ] = _PROJECT_IDS[1]
    assert "target_project_number_entries" in _allowlist_semantic_errors(wrong_target_pair)

    missing_key = deepcopy(allowlist)
    missing_key["parameters_by_placeholder"].pop("EXACT_DATABASE_NAME_RETURNED_BY_ALLOWLISTED_LIST")
    assert _allowlist_semantic_errors(missing_key)

    extra_key = deepcopy(allowlist)
    extra_key["parameters_by_placeholder"]["UNREVIEWED_VALUE"] = []
    assert _allowlist_semantic_errors(extra_key)


def test_phase_one_manifest_is_exact_position_bound_and_domain_signed():
    envelope = _envelope()
    assert _verification_errors(envelope, verification_time_ms=1_500) == []

    reordered = deepcopy(envelope)
    reordered["requests"][0], reordered["requests"][1] = (
        reordered["requests"][1],
        reordered["requests"][0],
    )
    _resign_envelope(reordered)
    assert "expected_manifest" in _verification_errors(reordered, verification_time_ms=1_500)

    duplicate_index = deepcopy(envelope)
    duplicate_index["requests"][1]["request_index"] = 0
    duplicate_index["requests"][1]["request_id"] = "req-000"
    unsigned_request = {
        key: value
        for key, value in duplicate_index["requests"][1].items()
        if key != "canonical_request_digest"
    }
    duplicate_index["requests"][1]["canonical_request_digest"] = _digest(unsigned_request)
    _resign_envelope(duplicate_index)
    errors = _verification_errors(duplicate_index, verification_time_ms=1_500)
    assert "request[1].index" in errors
    assert "request[1].id" in errors
    assert "request_ids_unique" in errors


def test_phase_two_accepts_only_authenticated_immediate_predecessor_chain():
    envelope, receipt, allowlist, ledger = _phase_two_fixture()
    assert (
        _verification_errors(
            envelope,
            verification_time_ms=2_500,
            predecessor_receipt=receipt,
            predecessor_allowlist=allowlist,
            ledger_snapshot=ledger,
        )
        == []
    )

    assert envelope["predecessor_phase_receipt_digest"] == _digest(receipt)
    assert (
        envelope["predecessor_parameter_allowlist_digest"]
        == receipt["derived_parameter_allowlist_digest"]
        == _digest(allowlist)
    )
    assert envelope["request_count"] == 12
    assert {
        (
            request["target_project_id"],
            request["method_ref"],
            request["target_project_number"],
        )
        for request in envelope["requests"]
    } == {
        (project_id, method_ref, _NUMBERS[project_id])
        for project_id in _PROJECT_IDS
        for method_ref in (
            "project_provenance_get",
            "project_ancestry_get",
            "project_billing_get",
        )
    }


@pytest.mark.parametrize(
    "attack, expected_error",
    (
        ("arbitrary_predecessor_digest", "predecessor_receipt_digest"),
        ("counter_reset", "predecessor_cumulative"),
        ("changed_allowlist_digest", "envelope_allowlist_digest"),
        ("duplicate_ledger_entry", "predecessor_ledger"),
        ("one_project_twelve_times", "expected_manifest"),
        ("cross_project_number", "expected_manifest"),
        ("literal_placeholder", "expected_manifest"),
    ),
)
def test_phase_two_adversarial_chains_fail_closed(
    attack: str,
    expected_error: str,
):
    envelope, receipt, allowlist, ledger = _phase_two_fixture()

    if attack == "arbitrary_predecessor_digest":
        envelope["predecessor_phase_receipt_digest"] = "a" * 64
    elif attack == "counter_reset":
        envelope["cumulative_prior_request_count"] = 500
    elif attack == "changed_allowlist_digest":
        envelope["predecessor_parameter_allowlist_digest"] = "b" * 64
    elif attack == "duplicate_ledger_entry":
        ledger.append(deepcopy(ledger[0]))
    elif attack == "one_project_twelve_times":
        envelope["requests"] = [
            _manifest(
                index=index,
                method_ref="project_provenance_get",
                project_id=_PROJECT_IDS[0],
                project_number=_NUMBERS[_PROJECT_IDS[0]],
            )
            for index in range(12)
        ]
    elif attack == "cross_project_number":
        request = envelope["requests"][3]
        request["target_project_number"] = _NUMBERS[_PROJECT_IDS[0]]
        request["canonical_path_and_query"] = request["canonical_path_and_query"].replace(
            _NUMBERS[_PROJECT_IDS[1]], _NUMBERS[_PROJECT_IDS[0]]
        )
        unsigned = {
            key: value for key, value in request.items() if key != "canonical_request_digest"
        }
        request["canonical_request_digest"] = _digest(unsigned)
    elif attack == "literal_placeholder":
        request = envelope["requests"][0]
        request["canonical_path_and_query"] = "/v3/projects/{EXACT_TARGET_PROJECT_NUMBER}"
        unsigned = {
            key: value for key, value in request.items() if key != "canonical_request_digest"
        }
        request["canonical_request_digest"] = _digest(unsigned)
    else:
        raise AssertionError(f"unknown attack: {attack}")

    if attack != "duplicate_ledger_entry":
        _resign_envelope(envelope)
    errors = _verification_errors(
        envelope,
        verification_time_ms=2_500,
        predecessor_receipt=receipt,
        predecessor_allowlist=allowlist,
        ledger_snapshot=ledger,
    )
    assert expected_error in errors


def test_receipt_signature_result_freshness_and_allowlist_binding_fail_closed():
    envelope, receipt, allowlist, ledger = _phase_two_fixture()

    bad_signature = deepcopy(receipt)
    bad_signature["receipt_signature"] = "A" * 86
    assert "receipt_signature" in _verification_errors(
        envelope,
        verification_time_ms=2_500,
        predecessor_receipt=bad_signature,
        predecessor_allowlist=allowlist,
        ledger_snapshot=ledger,
    )

    failed_receipt = deepcopy(receipt)
    failed_receipt["result"] = "incomplete"
    failed_receipt["error_codes"] = ["MANIFEST_COVERAGE_INCOMPLETE"]
    _sign(
        failed_receipt,
        key=_RECEIPT_KEY,
        signature_field="receipt_signature",
        domain=_RECEIPT_DOMAIN,
    )
    failed_envelope = deepcopy(envelope)
    failed_envelope["predecessor_phase_receipt_digest"] = _digest(failed_receipt)
    _resign_envelope(failed_envelope)
    assert "predecessor_result" in _verification_errors(
        failed_envelope,
        verification_time_ms=2_500,
        predecessor_receipt=failed_receipt,
        predecessor_allowlist=allowlist,
        ledger_snapshot=_ledger_snapshot(failed_receipt),
    )

    changed_allowlist = deepcopy(allowlist)
    changed_allowlist["project_bindings"][0]["immediate_parent_digest"] = "f" * 64
    errors = _verification_errors(
        envelope,
        verification_time_ms=2_500,
        predecessor_receipt=receipt,
        predecessor_allowlist=changed_allowlist,
        ledger_snapshot=ledger,
    )
    assert "receipt_allowlist_digest" in errors
    assert "envelope_allowlist_digest" in errors

    assert "predecessor_time" in _verification_errors(
        envelope,
        verification_time_ms=5_000,
        predecessor_receipt=receipt,
        predecessor_allowlist=allowlist,
        ledger_snapshot=ledger,
    )


def test_v5_contains_no_executor_credential_or_connected_fixture():
    package = _package()
    assert package["source_binding"]["package_is_authorization"] is False
    assert package["provenance"]["composite_rule"]["composite_is_authorization"] is False
    assert package["private_parameter_allowlist_contract"]["repository_storage_allowed"] is False
    assert package["private_parameter_allowlist_contract"]["chat_storage_allowed"] is False
    assert package["transition_policy"]["after_v5_review"] == (
        "generate_but_do_not_execute_one_source_only_owner_signable_project_id_binding_envelope"
    )
    assert "execute" not in package["authority"]["authorized_scope"]
