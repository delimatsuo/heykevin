"""Tests for the non-executable Task 4.8 gate preparation package."""

from __future__ import annotations

import ast
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Any, get_type_hints

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _ROOT / "tests/fixtures/voice_architecture_bakeoff"
_TEMPLATE_PATH = _FIXTURES / "task_4_8_gate_package.template.json"
_SCHEMA_PATHS = (
    _FIXTURES / "task_4_8_resource_inventory.schema.json",
    _FIXTURES / "task_4_8_privacy_attestation.schema.json",
    _FIXTURES / "task_4_8_signature_envelope.schema.json",
)
_VALIDATOR_PATH = (
    _ROOT / "tests/support/voice_bakeoff_task_4_8_gate_validator.py"
)


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "voice_bakeoff_task_4_8_gate_validator", _VALIDATOR_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _template() -> dict[str, Any]:
    return _load_json(_TEMPLATE_PATH)


def _ref(*parts: object) -> str:
    return "ref_" + "_".join(str(part) for part in parts)


def _digest(label: str) -> str:
    return validator.canonical_digest({"label": label})


def _complete_review_package() -> dict[str, Any]:
    package = _template()
    package["package_status"] = "review_ready_external_gates_required"

    inventory = package["resource_inventory"]
    inventory["status"] = "review_ready_external_gates_required"
    inventory["production_denylist_digest"] = _digest("production-denylist")
    for resource in inventory["resources"]:
        resource_id = resource["resource_id"]
        resource["selection_status"] = "selected_external_review_required"
        for key in (
            "provider_ref",
            "product_ref",
            "api_version_ref",
            "account_region_ref",
            "workload_destination_allowlist_ref",
            "control_plane_destination_allowlist_ref",
            "credential_ref",
            "identity_ref",
            "isolation_proof_ref",
            "residue_procedure_ref",
        ):
            resource[key] = _ref("resource", resource_id, key)
    for contract in inventory["probe_contracts"]:
        arm = contract["arm"]
        fact = contract["fact"]
        contract["status"] = "specified_external_review_required"
        contract["synthetic_fixture_digest"] = _digest(
            f"synthetic-fixture:{arm}:{fact}"
        )
        contract["capability_mapping_ref"] = _ref("capability", arm, fact)
        contract["evidence_sink_ref"] = _ref("evidence", arm, fact)
        contract["correlation_contract_ref"] = _ref(
            "correlation_contract", arm, fact
        )

    privacy = package["privacy_attestations"]
    privacy["status"] = "attested_external_review_required"
    for index, attestation in enumerate(privacy["attestations"], start=1):
        resource_id = attestation["resource_id"]
        attestation["status"] = "attested_external_review_required"
        attestation["provider_api_ref"] = _ref("privacy_api", resource_id)
        attestation["account_region_ref"] = _ref("privacy_region", resource_id)
        attestation["evidence_source_class"] = "signed_configuration_export"
        attestation["evidence_ref"] = _ref("privacy_evidence", resource_id)
        attestation["evidence_digest"] = _digest(
            f"privacy-evidence:{resource_id}"
        )
        attestation["observed_at_ms"] = index * 1_000
        attestation["expires_at_ms"] = index * 1_000 + 500
        attestation["owner_ref"] = _ref("privacy_owner", resource_id)
        attestation["residue_verification_ref"] = _ref(
            "residue_verification", resource_id
        )
        attestation["deletion_procedure_ref"] = _ref(
            "deletion_procedure", resource_id
        )
        for setting_name, setting in attestation["settings"].items():
            setting["state"] = validator.PRIVACY_SETTINGS[setting_name]
            setting["evidence_ref"] = _ref(
                "privacy_setting", resource_id, setting_name
            )
            setting["evidence_digest"] = _digest(
                f"privacy-setting:{resource_id}:{setting_name}"
            )

    envelope = package["signature_envelope"]
    envelope["status"] = "signed_external_review_required"
    envelope["arm"] = "B1"
    envelope["source_sha"] = "b" * 40
    envelope["manifest_digest"] = _digest("manifest")
    envelope["resource_inventory_digest"] = validator.canonical_digest(
        inventory
    )
    envelope["privacy_attestations_digest"] = validator.canonical_digest(
        privacy
    )
    envelope["capability_contract_digest"] = validator.canonical_digest(
        inventory["probe_contracts"]
    )
    envelope["approved_pstn_source_digest"] = _digest(
        "approved-pstn-source"
    )
    envelope["approved_pstn_destination_digest"] = _digest(
        "approved-pstn-destination"
    )
    envelope["evidence_kms_key_version_ref"] = _ref(
        "evidence_kms_key_version"
    )
    envelope["nonce_consumption_policy_digest"] = _digest(
        "nonce-consumption-policy"
    )
    envelope["nonce_consumption_record_ref"] = _ref(
        "nonce_consumption_record"
    )
    envelope["active_execution_binding_digest"] = _digest(
        "active-execution-binding"
    )
    envelope["findings_digest"] = _digest("review-findings")
    envelope["unresolved_p1_count"] = 0
    for key in validator.ARTIFACT_DIGEST_KEYS:
        envelope["artifact_digests"][key] = _digest(f"artifact:{key}")
    for index, key in enumerate(validator.CAP_KEYS, start=1):
        envelope["caps"][key] = index
    for key in envelope["custody"]:
        envelope["custody"][key] = _ref("custody", key)
    trust_store = envelope["trust_store"]
    trust_store["version_ref"] = _ref("trust_store_version")
    trust_store["policy_digest"] = _digest("trust-policy")
    trust_store["rotation_status_ref"] = _ref("trust_rotation")
    trust_store["revocation_status_ref"] = _ref("trust_revocation")
    trust_store["immutable_custody_ref"] = _ref("trust_custody")
    envelope["approval_id"] = _ref("approval_id")
    envelope["nonce"] = _ref("nonce")
    envelope["issued_at_ms"] = 10_000
    envelope["expires_at_ms"] = 11_000
    for signer in envelope["signers"]:
        role = signer["role"]
        signer["identity_ref"] = _ref("signer_identity", role)
        signer["key_id"] = _ref("signer_key", role)
        signer["public_key_ref"] = _ref("signer_public_key", role)
        signer["revocation_status_ref"] = _ref(
            "signer_revocation", role
        )
        signer["detached_signature_ref"] = _ref(
            "signer_signature", role
        )
        signer["signature_digest"] = _digest(f"signature:{role}")
        signer["review_decision"] = "approved"
    envelope["envelope_self_digest"] = validator.signature_payload_digest(
        envelope
    )
    package["review_digest"] = validator.review_digest(package)
    return package


def _walk_leaf_paths(
    value: object, path: tuple[object, ...] = ()
) -> list[tuple[tuple[object, ...], object]]:
    leaves: list[tuple[tuple[object, ...], object]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            leaves.extend(_walk_leaf_paths(child, (*path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            leaves.extend(_walk_leaf_paths(child, (*path, index)))
    else:
        leaves.append((path, value))
    return leaves


def _set_path(value: object, path: tuple[object, ...], replacement: object) -> None:
    current = value
    for part in path[:-1]:
        current = current[part]  # type: ignore[index]
    current[path[-1]] = replacement  # type: ignore[index]


def _reverse_mapping_order(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _reverse_mapping_order(child)
            for key, child in reversed(tuple(value.items()))
        }
    if isinstance(value, list):
        return [_reverse_mapping_order(child) for child in value]
    return value


def _object_schemas(value: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        if value.get("type") == "object":
            found.append(value)
        for child in value.values():
            found.extend(_object_schemas(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_object_schemas(child))
    return found


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def _schema_errors(
    value: object,
    schema: dict[str, Any],
    *,
    root: dict[str, Any] | None = None,
    path: str = "$",
) -> list[str]:
    root = schema if root is None else root
    if "$ref" in schema:
        reference = schema["$ref"]
        assert isinstance(reference, str) and reference.startswith("#/")
        target: object = root
        for part in reference[2:].split("/"):
            assert isinstance(target, dict)
            target = target[part.replace("~1", "/").replace("~0", "~")]
        assert isinstance(target, dict)
        return _schema_errors(value, target, root=root, path=path)

    errors: list[str] = []
    for branch in schema.get("allOf", []):
        errors.extend(_schema_errors(value, branch, root=root, path=path))
    condition = schema.get("if")
    if isinstance(condition, dict):
        selected = (
            schema.get("then")
            if not _schema_errors(value, condition, root=root, path=path)
            else schema.get("else")
        )
        if isinstance(selected, dict):
            errors.extend(_schema_errors(value, selected, root=root, path=path))
    branches = schema.get("oneOf")
    if isinstance(branches, list):
        matches = sum(
            not _schema_errors(value, branch, root=root, path=path)
            for branch in branches
        )
        if matches != 1:
            errors.append(f"{path}:oneOf")

    if "const" in schema and not _json_equal(value, schema["const"]):
        errors.append(f"{path}:const")
    if "enum" in schema and not any(
        _json_equal(value, member) for member in schema["enum"]
    ):
        errors.append(f"{path}:enum")

    expected_type = schema.get("type")
    type_valid = True
    if expected_type == "object":
        type_valid = isinstance(value, dict)
    elif expected_type == "array":
        type_valid = isinstance(value, list)
    elif expected_type == "string":
        type_valid = isinstance(value, str)
    elif expected_type == "integer":
        type_valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected_type == "boolean":
        type_valid = isinstance(value, bool)
    elif expected_type == "null":
        type_valid = value is None
    if not type_valid:
        errors.append(f"{path}:type")
        return errors

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}:required")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, child in value.items():
                child_schema = properties.get(key)
                if isinstance(child_schema, dict):
                    errors.extend(
                        _schema_errors(
                            child,
                            child_schema,
                            root=root,
                            path=f"{path}.{key}",
                        )
                    )
                elif schema.get("additionalProperties") is False:
                    errors.append(f"{path}.{key}:additional")
    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{path}:minItems")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(f"{path}:maxItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                errors.extend(
                    _schema_errors(
                        child,
                        item_schema,
                        root=root,
                        path=f"{path}[{index}]",
                    )
                )
    if isinstance(value, str) and isinstance(schema.get("pattern"), str):
        if re.fullmatch(schema["pattern"], value) is None:
            errors.append(f"{path}:pattern")
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and isinstance(schema.get("minimum"), int)
        and value < schema["minimum"]
    ):
        errors.append(f"{path}:minimum")
    return errors


def test_template_is_explicitly_nonexecutable_and_incomplete():
    package = _template()
    result = validator.validate_gate_package(package)
    assert package["package_status"] == "preparation_only"
    assert package["execution_supported"] is False
    assert package["signature_envelope"]["status"] == "unsigned_review"
    assert result.verdict == "preparation_incomplete"
    assert result.errors
    verdict_type = get_type_hints(validator.PreparationResult)["verdict"]
    assert set(verdict_type.__args__) == {
        "preparation_incomplete",
        "preparation_complete_external_gates_required",
    }

    for path in _SCHEMA_PATHS:
        schema = _load_json(path)
        assert schema["x-execution"] == "unsupported"
    provider_schema = _load_json(
        _FIXTURES / "provider_approval.schema.json"
    )
    manifest = _load_json(_FIXTURES / "manifest.json")
    assert provider_schema["x-execution"] == "unsupported"
    assert manifest["authorization_status"] == "template_only"


def test_fully_populated_synthetic_review_package_still_requires_external_gates():
    result = validator.validate_gate_package(_complete_review_package())
    assert result.errors == ()
    assert result.verdict == "preparation_complete_external_gates_required"
    assert "execution" not in result.verdict.replace(
        "external_gates_required", ""
    )


def test_inventory_and_probe_obligations_are_exact_and_closed():
    package = _template()
    inventory = package["resource_inventory"]
    expected_resources = {
        "execution_principal",
        "telephony",
        "native_voice",
        "speech_to_text",
        "text_generation",
        "text_to_speech",
        "conversation_relay",
        "auth_token_store",
        "firestore",
        "rtdb",
        "logging_evidence",
        "kms",
        "immutable_custody",
        "nonce_store",
        "active_execution_store",
        "trust_store",
        "credential_broker",
        "residue_receipt_sink",
        "telephony_control_plane",
        "native_voice_control_plane",
        "speech_to_text_control_plane",
        "text_generation_control_plane",
        "text_to_speech_control_plane",
        "conversation_relay_control_plane",
    }
    assert set(validator.RESOURCE_IDS) == expected_resources
    assert {
        resource["resource_id"] for resource in inventory["resources"]
    } == expected_resources
    assert len(inventory["resources"]) == len(expected_resources) == 24

    common_execution_controls = {
        "telephony_control_plane",
        "nonce_store",
        "active_execution_store",
        "immutable_custody",
        "trust_store",
        "credential_broker",
        "residue_receipt_sink",
    }
    for dependencies in inventory["arm_dependencies"].values():
        assert common_execution_controls <= set(dependencies)
    assert "native_voice_control_plane" in inventory["arm_dependencies"]["A"]
    assert "native_voice_control_plane" in inventory["arm_dependencies"]["C"]
    assert {
        "speech_to_text_control_plane",
        "text_generation_control_plane",
        "text_to_speech_control_plane",
    } <= set(inventory["arm_dependencies"]["B1"])
    assert {
        "conversation_relay_control_plane",
        "text_generation_control_plane",
    } <= set(inventory["arm_dependencies"]["B2"])

    actual_contracts = {
        (contract["arm"], contract["fact"])
        for contract in inventory["probe_contracts"]
    }
    expected_contracts = {
        (arm, fact)
        for arm, facts in validator.PROBE_FACTS_BY_ARM.items()
        for fact in facts
    }
    assert actual_contracts == expected_contracts
    assert len(actual_contracts) == 69
    special = "response_correlated_normal_playback_receipt"
    assert ("B2", special) in actual_contracts
    assert all(
        (arm, special) not in actual_contracts for arm in ("A", "B1", "C")
    )
    for contract in inventory["probe_contracts"]:
        assert contract["caller_audio_correlation_required"] is (
            contract["fact"] in validator.CALLER_AUDIO_CORRELATED_FACTS
        )


def test_template_and_populated_sections_match_the_draft_2020_12_schemas():
    template = _template()
    complete = _complete_review_package()
    section_names = (
        "resource_inventory",
        "privacy_attestations",
        "signature_envelope",
    )
    for section_name, schema_path in zip(
        section_names, _SCHEMA_PATHS, strict=True
    ):
        schema = _load_json(schema_path)
        assert schema["$schema"] == (
            "https://json-schema.org/draft/2020-12/schema"
        )
        assert _schema_errors(template[section_name], schema) == []
        assert _schema_errors(complete[section_name], schema) == []


def test_draft_schemas_reject_drift_and_cross_arm_probe_claims():
    template = _template()
    for section_name, schema_path in zip(
        (
            "resource_inventory",
            "privacy_attestations",
            "signature_envelope",
        ),
        _SCHEMA_PATHS,
        strict=True,
    ):
        mutated = deepcopy(template[section_name])
        mutated["unexpected"] = None
        assert _schema_errors(mutated, _load_json(schema_path))

    inventory_schema = _load_json(_SCHEMA_PATHS[0])
    inventory = deepcopy(template["resource_inventory"])
    inventory["probe_contracts"][0]["fact"] = (
        "response_correlated_normal_playback_receipt"
    )
    assert _schema_errors(inventory, inventory_schema)

    inventory = deepcopy(template["resource_inventory"])
    transport = next(
        contract
        for contract in inventory["probe_contracts"]
        if contract["fact"] == "transport_resolved"
    )
    transport["caller_audio_correlation_required"] = False
    assert _schema_errors(inventory, inventory_schema)

    envelope = deepcopy(template["signature_envelope"])
    envelope["unresolved_p1_count"] = 1
    assert _schema_errors(envelope, _load_json(_SCHEMA_PATHS[2]))


def test_every_required_placeholder_is_independently_blocking():
    template = _template()
    complete = _complete_review_package()
    placeholder_paths = [
        path for path, value in _walk_leaf_paths(template) if value is None
    ]
    assert len(placeholder_paths) > 1_300
    for path in placeholder_paths:
        mutated = deepcopy(complete)
        _set_path(mutated, path, None)
        result = validator.validate_gate_package(mutated)
        assert result.verdict == "preparation_incomplete", path
        assert result.errors, path


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_resource",
        "missing_resource",
        "extra_resource_field",
        "extra_probe_contract",
        "extra_attestation",
        "reused_signer_identity",
        "reused_signer_key_id",
        "reused_signer_public_key",
        "duplicate_signer_role",
        "unresolved_p1",
    ],
)
def test_closed_inventories_and_distinct_approval_roles(mutation: str):
    package = _complete_review_package()
    inventory = package["resource_inventory"]
    privacy = package["privacy_attestations"]
    signers = package["signature_envelope"]["signers"]
    if mutation == "extra_resource":
        inventory["resources"].append(deepcopy(inventory["resources"][0]))
    elif mutation == "missing_resource":
        inventory["resources"].pop()
    elif mutation == "extra_resource_field":
        inventory["resources"][0]["unexpected"] = _ref("unexpected")
    elif mutation == "extra_probe_contract":
        inventory["probe_contracts"].append(
            deepcopy(inventory["probe_contracts"][0])
        )
    elif mutation == "extra_attestation":
        privacy["attestations"].append(deepcopy(privacy["attestations"][0]))
    elif mutation == "reused_signer_identity":
        signers[1]["identity_ref"] = signers[0]["identity_ref"]
    elif mutation == "reused_signer_key_id":
        signers[1]["key_id"] = signers[0]["key_id"]
    elif mutation == "reused_signer_public_key":
        signers[1]["public_key_ref"] = signers[0]["public_key_ref"]
    elif mutation == "duplicate_signer_role":
        signers[1]["role"] = signers[0]["role"]
    elif mutation == "unresolved_p1":
        package["signature_envelope"]["unresolved_p1_count"] = 1
    result = validator.validate_gate_package(package)
    assert result.verdict == "preparation_incomplete"
    assert result.errors


def test_canonical_digests_ignore_mapping_order_and_detect_mutation():
    package = _complete_review_package()
    reordered = _reverse_mapping_order(package)
    assert validator.canonical_digest(package) == validator.canonical_digest(
        reordered
    )
    result = validator.validate_gate_package(reordered)
    assert result.verdict == "preparation_complete_external_gates_required"

    package["signature_envelope"]["manifest_digest"] = _digest(
        "mutated-manifest"
    )
    mutated_result = validator.validate_gate_package(package)
    assert mutated_result.verdict == "preparation_incomplete"
    assert "package.review_digest" in mutated_result.errors


@pytest.mark.parametrize(
    "path",
    [
        ("resource_inventory", "resources", 0, "resource_id"),
        (
            "privacy_attestations",
            "attestations",
            0,
            "evidence_source_class",
        ),
        ("signature_envelope", "arm"),
        ("signature_envelope", "signers", 0, "role"),
        ("signature_envelope", "signers", 0, "identity_ref"),
    ],
)
def test_malformed_json_values_fail_closed_without_validator_exceptions(
    path: tuple[object, ...],
):
    package = _complete_review_package()
    _set_path(package, path, [])
    result = validator.validate_gate_package(package)
    assert result.verdict == "preparation_incomplete"
    assert result.errors


def test_template_contains_no_secret_raw_account_or_phone_material():
    package = _template()
    serialized = json.dumps(package, sort_keys=True)
    assert "-----BEGIN" not in serialized
    assert re.search(r"\+1[0-9]{10}", serialized) is None
    assert re.search(r"\bAC[0-9a-fA-F]{32}\b", serialized) is None
    assert re.search(r"\bAIza[0-9A-Za-z_-]{20,}\b", serialized) is None
    assert re.search(r"\bsk-[0-9A-Za-z_-]{12,}\b", serialized) is None

    prohibited_keys = {
        "api_key",
        "access_token",
        "auth_token",
        "private_key",
        "credential_value",
        "phone_number",
        "account_id",
    }
    for path, value in _walk_leaf_paths(package):
        assert not prohibited_keys.intersection(
            str(part) for part in path
        )
        leaf_key = path[-1]
        if isinstance(leaf_key, str) and (
            leaf_key.endswith("_ref") or leaf_key.endswith("_digest")
        ):
            assert value is None, path


def test_all_schema_objects_are_closed():
    for path in _SCHEMA_PATHS:
        schema = _load_json(path)
        objects = _object_schemas(schema)
        assert objects
        assert all(
            item.get("additionalProperties") is False for item in objects
        )


def test_validator_is_pure_and_unreachable_from_application_and_runner():
    source = _VALIDATOR_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(("", alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.update(
                (node.module or "", alias.name) for alias in node.names
            )
    assert imports == {
        ("__future__", "annotations"),
        ("collections.abc", "Mapping"),
        ("dataclasses", "dataclass"),
        ("", "hashlib"),
        ("", "json"),
        ("typing", "Literal"),
    }
    assert not any(
        isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.Yield))
        for node in ast.walk(tree)
    )
    forbidden_calls = {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "import_module",
        "getenv",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "run",
        "Popen",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else ""
            )
            assert name not in forbidden_calls
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "main"
        for node in ast.walk(tree)
    )
    assert "execution_authorized" not in source
    assert "execute_provider" not in source
    assert "provider_execution" not in source

    module_reference = "voice_bakeoff_task_4_8_gate_validator"
    for path in (
        _ROOT / "app/main.py",
        _ROOT / "scripts/run_voice_architecture_bakeoff.py",
        _ROOT / "scripts/voice_bakeoff_caller.py",
    ):
        assert module_reference not in path.read_text(encoding="utf-8")


def test_package_adds_no_provider_execution_component():
    runner_source = (
        _ROOT / "scripts/run_voice_architecture_bakeoff.py"
    ).read_text(encoding="utf-8")
    caller_source = (
        _ROOT / "scripts/voice_bakeoff_caller.py"
    ).read_text(encoding="utf-8")
    assert "--execute-provider" not in runner_source
    assert "task_4_8_gate_package" not in runner_source
    assert "task_4_8_gate_package" not in caller_source
    assert "task_4_8_signature_envelope" not in runner_source
    assert "task_4_8_signature_envelope" not in caller_source
