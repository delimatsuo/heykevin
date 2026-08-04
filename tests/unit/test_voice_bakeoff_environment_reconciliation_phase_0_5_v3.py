"""Offline guards for the Phase 0.5 v3 environment-reconciliation amendment."""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v3.json"
)
_SCHEMA_PATH = (
    _ROOT
    / "docs/security/"
    "voice-bakeoff-environment-reconciliation-phase-0-5-v3.schema.json"
)
_GUIDE_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v3.md"
)
_SOURCE_SHA = "2ed8ea7d1d7f338e84ddf08d5a50a714835e1533"
_V2_HASHES = {
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.json": (
        "508b06d75f6f585c513183199074125bc6fc3fdb9987dec8a7deb26e5bc1027f"
    ),
    (
        "docs/security/"
        "voice-bakeoff-environment-reconciliation-phase-0-5-v2.schema.json"
    ): "1c694b2879714a254e1b933131c5dcdf347f1cd5afbed660e03563417064d92d",
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.md": (
        "b63eaaac7be760748d36765ebba5a04b5ef86b8e966d6199c37946889d7bfe4c"
    ),
    "tests/unit/test_voice_bakeoff_environment_reconciliation_phase_0_5_v2.py": (
        "f6e5376ef9d0aaa87f54a1a1aa5b198e22fb0977e8a7fbb45ebf4536914dc415"
    ),
}
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
_TEST_SIGNING_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _package() -> dict[str, Any]:
    return _load_json(_PACKAGE_PATH)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    """Canonical form for these string/integer/boolean-only test fixtures."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _subtree_digest(value: object) -> str:
    return _sha256_bytes(_canonical(value))


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def _type_matches(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise AssertionError(f"unsupported schema type: {expected}")


def _schema_errors(
    value: object,
    schema: dict[str, Any],
    *,
    root: dict[str, Any] | None = None,
    path: str = "$",
) -> list[str]:
    """Validate the JSON Schema subset used by this source-only package."""

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

    if "const" in schema and not _json_equal(value, schema["const"]):
        errors.append(f"{path}:const")
    if "enum" in schema and not any(
        _json_equal(value, member) for member in schema["enum"]
    ):
        errors.append(f"{path}:enum")

    expected_type = schema.get("type")
    if isinstance(expected_type, str):
        type_valid = _type_matches(value, expected_type)
    elif isinstance(expected_type, list):
        type_valid = any(
            isinstance(member, str) and _type_matches(value, member)
            for member in expected_type
        )
    else:
        type_valid = True
    if not type_valid:
        errors.append(f"{path}:type")
        return errors

    if isinstance(value, dict):
        for key in schema.get("required", []):
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
        if schema.get("uniqueItems") is True:
            normalized = [_canonical(item) for item in value]
            if len(normalized) != len(set(normalized)):
                errors.append(f"{path}:uniqueItems")
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

    if isinstance(value, str):
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            errors.append(f"{path}:pattern")
    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, int) and value < minimum:
            errors.append(f"{path}:minimum")
        if isinstance(maximum, int) and value > maximum:
            errors.append(f"{path}:maximum")
    return errors


def _public_key_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _sign_receipt(receipt: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_signature"}
    signature = _TEST_SIGNING_KEY.sign(_canonical(unsigned))
    receipt["receipt_signature"] = base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _sign_envelope(envelope: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in envelope.items() if key != "owner_signature"}
    signature = _TEST_SIGNING_KEY.sign(_canonical(unsigned))
    envelope["owner_signature"] = base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _derived_nonce(
    domain: str,
    envelope_nonce_seed: str,
    request_index: int,
    request_digest: str,
) -> str:
    payload = (
        domain.encode()
        + bytes.fromhex(envelope_nonce_seed)
        + request_index.to_bytes(4, "big")
        + bytes.fromhex(request_digest)
    )
    return _sha256_bytes(payload)


def _manifest_fixture(
    *,
    index: int = 0,
    project_id: str = "kevin-491315",
    method_ref: str = "project_identity_get",
) -> dict[str, Any]:
    project_number = str(123456 + _PROJECT_IDS.index(project_id))
    method_values = {
        "project_identity_get": {
            "endpoint": "cloudresourcemanager.googleapis.com",
            "http_method": "GET",
            "path": f"/v1/projects/{project_id}",
            "fields": ["projectId", "projectNumber", "lifecycleState", "parent"],
            "project_number": None,
        },
        "project_provenance_get": {
            "endpoint": "cloudresourcemanager.googleapis.com",
            "http_method": "GET",
            "path": f"/v3/projects/{project_number}",
            "fields": ["createTime", "name", "parent", "projectId", "state"],
            "project_number": project_number,
        },
        "project_ancestry_get": {
            "endpoint": "cloudresourcemanager.googleapis.com",
            "http_method": "POST",
            "path": f"/v1/projects/{project_id}:getAncestry",
            "fields": ["ancestor.resourceId.id", "ancestor.resourceId.type"],
            "project_number": project_number,
        },
        "project_billing_get": {
            "endpoint": "cloudbilling.googleapis.com",
            "http_method": "GET",
            "path": f"/v1/projects/{project_id}/billingInfo",
            "fields": ["billingAccountName", "billingEnabled", "name", "projectId"],
            "project_number": project_number,
        },
    }[method_ref]
    manifest = {
        "request_index": index,
        "request_id": f"req-{index:03d}",
        "method_ref": method_ref,
        "identity_ref": _QUERY_IDENTITIES[project_id],
        "local_configuration_digest": (
            "3" * 64
            if _QUERY_IDENTITIES[project_id] == "organization_operator_identity"
            else "8" * 64
        ),
        "target_project_id": project_id,
        "target_project_number": method_values["project_number"],
        "quota_project_id": project_id,
        "endpoint": method_values["endpoint"],
        "http_method": method_values["http_method"],
        "canonical_path_and_query": method_values["path"],
        "request_body_digest": _sha256_bytes(b"null"),
        "response_field_mask": method_values["fields"],
        "raw_evidence_class": "restricted_external_custody",
    }
    manifest["canonical_request_digest"] = _subtree_digest(manifest)
    return manifest


def _request_fixture(
    manifest: dict[str, Any],
    *,
    envelope_digest: str,
    envelope_nonce_seed: str,
) -> dict[str, Any]:
    request = {
        "request_index": manifest["request_index"],
        "request_id": manifest["request_id"],
        "method_ref": manifest["method_ref"],
        "identity_ref": manifest["identity_ref"],
        "target_project_id": manifest["target_project_id"],
        "target_project_number": manifest["target_project_number"],
        "canonical_request_digest": manifest["canonical_request_digest"],
        "authorization_digest": envelope_digest,
        "derived_nonce": _derived_nonce(
            _package()["bounded_inventory_envelope_contract"]["domain_separator"],
            envelope_nonce_seed,
            int(manifest["request_index"]),
            str(manifest["canonical_request_digest"]),
        ),
        "nonce_consumed_before_dispatch": True,
        "response_field_mask_enforced": True,
        "raw_response_gate_passed": True,
        "status_code_class": "2xx",
        "response_projection_digest": "b" * 64,
        "private_custody_record_digest": "c" * 64,
        "completed_at_ms": 1_200,
        "error_codes": [],
        "result": "pass",
    }
    request["receipt_digest"] = _subtree_digest(request)
    return request


def _envelope_fixture() -> dict[str, Any]:
    package = _package()
    public_key = _TEST_SIGNING_KEY.public_key()
    manifests = [
        _manifest_fixture(index=index, project_id=project_id, method_ref=method_ref)
        for index, (project_id, method_ref) in enumerate(
            (project_id, method_ref)
            for project_id in _PROJECT_IDS
            for method_ref in (
                "project_identity_get",
                "project_provenance_get",
                "project_ancestry_get",
                "project_billing_get",
            )
        )
    ]
    envelope = {
        "envelope_version": 3,
        "phase": "identity_binding",
        "source_sha": _SOURCE_SHA,
        "v2_json_digest": _V2_HASHES[
            "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.json"
        ],
        "v3_contract_digest": package["bounded_inventory_envelope_contract"][
            "contract_digest_sha256"
        ],
        "coverage_matrix_digest": package["effective_access_contract"][
            "coverage_matrix_digest_sha256"
        ],
        "owner_public_key_digest": _sha256_bytes(_public_key_bytes(public_key)),
        "identity_configuration_digests": {
            "organization_operator_identity": "3" * 64,
            "isolated_bakeoff_operator_identity": "8" * 64,
        },
        "raw_evidence_custody_digest": "9" * 64,
        "envelope_nonce_seed": "a" * 64,
        "request_count": len(manifests),
        "request_set_digest": _subtree_digest(manifests),
        "requests": manifests,
        "issued_at_ms": 1_000,
        "expires_at_ms": 2_000,
        "audit_and_quota_effects_acknowledged": True,
        "owner_signature": "",
    }
    _sign_envelope(envelope)
    return envelope


def _receipt_fixture() -> dict[str, Any]:
    package = _package()
    public_key = _TEST_SIGNING_KEY.public_key()
    envelope = _envelope_fixture()
    envelope_digest = _subtree_digest(envelope)
    requests = [
        _request_fixture(
            manifest,
            envelope_digest=envelope_digest,
            envelope_nonce_seed=envelope["envelope_nonce_seed"],
        )
        for manifest in envelope["requests"]
    ]
    receipt = {
        "receipt_schema_version": 3,
        "source_sha": _SOURCE_SHA,
        "v2_json_digest": _V2_HASHES[
            "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.json"
        ],
        "v3_contract_digest": package["bounded_inventory_envelope_contract"][
            "contract_digest_sha256"
        ],
        "coverage_matrix_digest": package["effective_access_contract"][
            "coverage_matrix_digest_sha256"
        ],
        "phase": "identity_binding",
        "phase_envelope_digest": envelope_digest,
        "authorization_digest": envelope_digest,
        "request_set_digest": envelope["request_set_digest"],
        "request_count": len(requests),
        "request_receipts": requests,
        "visibility_claims": [],
        "pass_predicates": {
            "contract_binding_valid": True,
            "authorization_signature_valid": True,
            "receipt_signature_valid": True,
            "request_set_complete": True,
            "request_receipts_authenticated": True,
            "request_results_and_projections_bound": True,
            "nonces_consumed_once": True,
            "project_identity_mapping_valid": True,
            "timestamps_ordered_and_fresh": True,
            "response_filters_enforced": True,
            "raw_response_gates_passed": True,
            "no_unexpected_fields": True,
            "no_free_text_or_raw_evidence": True,
        },
        "error_codes": [],
        "issued_at_ms": 1_000,
        "completed_at_ms": 1_300,
        "expires_at_ms": 2_000,
        "receipt_public_key_digest": _sha256_bytes(_public_key_bytes(public_key)),
        "receipt_signature": "",
        "result": "pass",
    }
    _sign_receipt(receipt)
    return receipt


def _envelope_semantic_errors(
    envelope: dict[str, Any],
    *,
    verification_time_ms: int,
    trusted_owner_key: Ed25519PublicKey | None = None,
) -> list[str]:
    package = _package()
    schema = _load_json(_SCHEMA_PATH)
    errors = _schema_errors(
        envelope,
        schema["$defs"]["phase_envelope"],
        root=schema,
    )
    requests = envelope.get("requests")
    if not isinstance(requests, list):
        return errors + ["requests"]
    if envelope.get("request_count") != len(requests):
        errors.append("request_count")
    if envelope.get("request_set_digest") != _subtree_digest(requests):
        errors.append("request_set_digest")

    phases = {
        phase["phase"]: phase
        for phase in package["bounded_inventory_envelope_contract"]["phases"]
    }
    phase = phases.get(str(envelope.get("phase")))
    if phase is None:
        errors.append("phase")
    else:
        if len(requests) < phase["minimum_requests"]:
            errors.append("phase_minimum")
        if len(requests) > phase["maximum_requests"]:
            errors.append("phase_cap")
        allowed = set(phase["allowed_method_refs"])
        if any(request.get("method_ref") not in allowed for request in requests):
            errors.append("phase_method")
        if phase["phase"] == "identity_binding":
            observed = {
                (request.get("target_project_id"), request.get("method_ref"))
                for request in requests
            }
            expected = {
                (project_id, method_ref)
                for project_id in _PROJECT_IDS
                for method_ref in phase["allowed_method_refs"]
            }
            if observed != expected:
                errors.append("identity_phase_coverage")

    for index, request in enumerate(requests):
        unsigned_request = {
            key: value
            for key, value in request.items()
            if key != "canonical_request_digest"
        }
        if request.get("canonical_request_digest") != _subtree_digest(
            unsigned_request
        ):
            errors.append(f"request[{index}].canonical_request_digest")
        if request.get("quota_project_id") != request.get("target_project_id"):
            errors.append(f"request[{index}].quota_project")
        identity = request.get("identity_ref")
        if identity != _QUERY_IDENTITIES.get(str(request.get("target_project_id"))):
            errors.append(f"request[{index}].identity")
        configs = envelope.get("identity_configuration_digests")
        if (
            not isinstance(configs, dict)
            or request.get("local_configuration_digest") != configs.get(identity)
        ):
            errors.append(f"request[{index}].configuration")
        v2 = _load_json(
            _ROOT
            / "docs/security/"
            "voice-bakeoff-environment-reconciliation-phase-0-5-v2.json"
        )
        method_ref = str(request.get("method_ref"))
        method = v2["read_contract"]["methods"].get(method_ref)
        if isinstance(method, dict):
            expected_path = (
                method["path"]
                .replace(
                    "{EXACT_TARGET_PROJECT_ID}",
                    str(request.get("target_project_id")),
                )
                .replace(
                    "{EXACT_TARGET_PROJECT_NUMBER}",
                    str(request.get("target_project_number")),
                )
            )
            expected_body_digest = _subtree_digest(method["request_body"])
            exact_method_values = {
                "endpoint": method["endpoint"],
                "http_method": method["http_method"],
                "canonical_path_and_query": expected_path,
                "request_body_digest": expected_body_digest,
                "response_field_mask": method["response_field_mask"],
                "raw_evidence_class": method["raw_evidence_class"],
            }
            if any(
                request.get(field) != expected
                for field, expected in exact_method_values.items()
            ):
                errors.append(f"request[{index}].method_contract")

    issued = envelope.get("issued_at_ms")
    expires = envelope.get("expires_at_ms")
    if not (
        isinstance(issued, int)
        and isinstance(expires, int)
        and issued < verification_time_ms < expires
    ):
        errors.append("timestamp_order")

    key = trusted_owner_key or _TEST_SIGNING_KEY.public_key()
    if envelope.get("owner_public_key_digest") != _sha256_bytes(
        _public_key_bytes(key)
    ):
        errors.append("owner_key_digest")
    signature_text = envelope.get("owner_signature")
    if isinstance(signature_text, str):
        try:
            signature = base64.urlsafe_b64decode(signature_text + "==")
            unsigned = {
                key_name: value
                for key_name, value in envelope.items()
                if key_name != "owner_signature"
            }
            key.verify(signature, _canonical(unsigned))
        except (InvalidSignature, ValueError):
            errors.append("owner_signature")
    else:
        errors.append("owner_signature")
    return errors


def _semantic_errors(
    receipt: dict[str, Any],
    *,
    expected_manifest: list[dict[str, Any]],
    envelope_nonce_seed: str,
    verification_time_ms: int,
    authorization_signature_valid: bool = True,
    trusted_receipt_key: Ed25519PublicKey | None = None,
) -> list[str]:
    package = _package()
    errors: list[str] = []
    schema = _load_json(_SCHEMA_PATH)
    receipt_schema = schema["$defs"]["payload_safe_phase_receipt"]
    errors.extend(_schema_errors(receipt, receipt_schema, root=schema))

    expected_contract = package["bounded_inventory_envelope_contract"][
        "contract_digest_sha256"
    ]
    expected_coverage = package["effective_access_contract"][
        "coverage_matrix_digest_sha256"
    ]
    if receipt.get("v3_contract_digest") != expected_contract:
        errors.append("contract_digest")
    if receipt.get("coverage_matrix_digest") != expected_coverage:
        errors.append("coverage_digest")
    if not authorization_signature_valid:
        errors.append("authorization_signature")

    request_receipts = receipt.get("request_receipts")
    if not isinstance(request_receipts, list):
        return errors + ["request_receipts"]
    if receipt.get("request_count") != len(expected_manifest):
        errors.append("request_count_manifest")
    if receipt.get("request_count") != len(request_receipts):
        errors.append("request_count_receipts")
    if receipt.get("request_set_digest") != _subtree_digest(expected_manifest):
        errors.append("request_set_digest")

    phase_caps = {
        phase["phase"]: (
            phase["minimum_requests"],
            phase["maximum_requests"],
            set(phase["allowed_method_refs"]),
        )
        for phase in package["bounded_inventory_envelope_contract"]["phases"]
    }
    phase_values = phase_caps.get(str(receipt.get("phase")))
    if phase_values is None:
        errors.append("phase")
    elif not (phase_values[0] <= len(request_receipts) <= phase_values[1]):
        errors.append("phase_cap")
    elif any(
        request.get("method_ref") not in phase_values[2]
        for request in request_receipts
    ):
        errors.append("phase_method")

    seen_nonces: set[str] = set()
    for index, (actual, expected) in enumerate(
        zip(request_receipts, expected_manifest, strict=False)
    ):
        for field in (
            "request_index",
            "request_id",
            "method_ref",
            "identity_ref",
            "target_project_id",
            "target_project_number",
            "canonical_request_digest",
        ):
            if actual.get(field) != expected.get(field):
                errors.append(f"request[{index}].{field}")
        expected_identity = _QUERY_IDENTITIES.get(str(actual.get("target_project_id")))
        if actual.get("identity_ref") != expected_identity:
            errors.append(f"request[{index}].identity_mapping")
        expected_nonce = _derived_nonce(
            package["bounded_inventory_envelope_contract"]["domain_separator"],
            envelope_nonce_seed,
            int(actual.get("request_index", -1)),
            str(actual.get("canonical_request_digest")),
        )
        if actual.get("derived_nonce") != expected_nonce:
            errors.append(f"request[{index}].nonce")
        if actual.get("authorization_digest") != receipt.get(
            "phase_envelope_digest"
        ):
            errors.append(f"request[{index}].authorization_digest")
        nonce = str(actual.get("derived_nonce"))
        if nonce in seen_nonces:
            errors.append(f"request[{index}].nonce_duplicate")
        seen_nonces.add(nonce)
        if actual.get("nonce_consumed_before_dispatch") is not True:
            errors.append(f"request[{index}].nonce_unconsumed")
        if actual.get("response_field_mask_enforced") is not True:
            errors.append(f"request[{index}].field_mask")
        if actual.get("raw_response_gate_passed") is not True:
            errors.append(f"request[{index}].raw_gate")
        if actual.get("status_code_class") != "2xx":
            errors.append(f"request[{index}].status")
        if actual.get("result") != "pass":
            errors.append(f"request[{index}].result")
        for digest_field in (
            "response_projection_digest",
            "private_custody_record_digest",
        ):
            digest_value = actual.get(digest_field)
            if not isinstance(digest_value, str) or re.fullmatch(
                r"[0-9a-f]{64}",
                digest_value,
            ) is None:
                errors.append(f"request[{index}].{digest_field}")
        unsigned_receipt = {
            key: value for key, value in actual.items() if key != "receipt_digest"
        }
        if actual.get("receipt_digest") != _subtree_digest(unsigned_receipt):
            errors.append(f"request[{index}].receipt_digest")
        if actual.get("error_codes"):
            errors.append(f"request[{index}].error_codes")

    issued = receipt.get("issued_at_ms")
    completed = receipt.get("completed_at_ms")
    expires = receipt.get("expires_at_ms")
    if not (
        isinstance(issued, int)
        and isinstance(completed, int)
        and isinstance(expires, int)
        and issued <= completed < expires
        and verification_time_ms < expires
    ):
        errors.append("timestamp_order")

    key = trusted_receipt_key or _TEST_SIGNING_KEY.public_key()
    expected_key_digest = _sha256_bytes(_public_key_bytes(key))
    if receipt.get("receipt_public_key_digest") != expected_key_digest:
        errors.append("receipt_key_digest")
    signature_text = receipt.get("receipt_signature")
    if isinstance(signature_text, str):
        try:
            signature = base64.urlsafe_b64decode(signature_text + "==")
            unsigned = {
                key_name: value
                for key_name, value in receipt.items()
                if key_name != "receipt_signature"
            }
            key.verify(signature, _canonical(unsigned))
        except (InvalidSignature, ValueError):
            errors.append("receipt_signature")
    else:
        errors.append("receipt_signature")

    if receipt.get("result") == "pass":
        predicates = receipt.get("pass_predicates")
        if not isinstance(predicates, dict) or not all(
            value is True for value in predicates.values()
        ):
            errors.append("pass_predicates")
        if receipt.get("error_codes"):
            errors.append("top_level_error_codes")
    return errors


def test_v3_package_matches_closed_top_level_schema():
    package = _package()
    schema = _load_json(_SCHEMA_PATH)

    assert _schema_errors(package, schema) == []
    extra = deepcopy(package)
    extra["connected_executor"] = {}
    assert any(error.endswith(":additional") for error in _schema_errors(extra, schema))


def test_v3_preserves_exact_v2_and_remains_non_authorizing():
    package = _package()

    for path, expected in _V2_HASHES.items():
        assert _sha256_bytes((_ROOT / path).read_bytes()) == expected
    assert package["package_status"] == "source_only_exact_review_candidate"
    assert package["source_binding"]["source_sha"] == _SOURCE_SHA
    assert package["source_binding"]["package_is_authorization"] is False
    assert package["authority"] == {
        "authorized_scope": "source_only_phase_0_5_v3_amendment",
        "connected_inventory_status": "not_authorized",
        "mutation_status": "not_authorized",
        "execution_status": "not_authorized",
        "task_4_8_status": "sealed",
        "owner_signable_package_status": "not_generated",
        "advisory_review_is_owner_authorization": False,
        "reviewed_contract_is_owner_authorization": False,
    }


def test_v3_subtree_digests_are_exact_and_schema_bound():
    package = _package()
    schema = _load_json(_SCHEMA_PATH)
    envelope = deepcopy(package["bounded_inventory_envelope_contract"])
    expected_contract = envelope.pop("contract_digest_sha256")
    access = deepcopy(package["effective_access_contract"])
    expected_coverage = access.pop("coverage_matrix_digest_sha256")

    assert _subtree_digest(envelope) == expected_contract
    assert _subtree_digest(access) == expected_coverage
    receipt_schema = schema["$defs"]["payload_safe_phase_receipt"]["properties"]
    assert receipt_schema["v3_contract_digest"]["const"] == expected_contract
    assert receipt_schema["coverage_matrix_digest"]["const"] == expected_coverage


def test_environment_recommendation_freezes_four_and_forbids_duplication():
    recommendation = _package()["environment_recommendation"]

    assert recommendation["governance_status"] == "undecided"
    assert recommendation["default_disposition"] == "retain_frozen"
    assert recommendation["project_creation_cap"] == 0
    assert recommendation["duplicate_project_recommendation"] == "forbidden"
    assert tuple(recommendation["exact_project_ids"]) == _PROJECT_IDS
    assert "many_to_many" in recommendation["multiple_account_visibility_rule"]


def test_phase_envelopes_have_exact_total_cap_and_no_pagination_followup():
    contract = _package()["bounded_inventory_envelope_contract"]
    phases = contract["phases"]
    caps = contract["caps"]

    assert [phase["phase"] for phase in phases] == [
        "identity_binding",
        "metadata_discovery",
        "metadata_detail",
        "access_control_discovery",
        "access_control_detail",
    ]
    assert sum(phase["maximum_requests"] for phase in phases) == 256
    assert caps["owner_signed_phase_envelopes"] == 5
    assert caps["total_connected_requests"] == 256
    assert caps["pages_per_list_or_search"] == 1
    assert caps["next_page_token_action"] == (
        "incomplete_abort_without_followup_request"
    )
    assert caps["retries"] == 0
    assert caps["concurrency"] == 1
    assert all(
        caps[key] == 0
        for key in (
            "mutating_operations",
            "project_creation",
            "api_enablement_operations",
            "credential_or_token_operations",
            "service_account_impersonations",
            "workload_operations",
            "provider_or_pstn_requests",
            "firestore_document_reads",
            "firebase_record_reads",
            "secret_payload_reads",
            "log_entry_reads",
            "cloud_run_invocations",
        )
    )


def test_phase_method_allowlists_cover_exact_composite_method_set():
    package = _package()
    v2 = _load_json(
        _ROOT
        / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.json"
    )
    schema = _load_json(_SCHEMA_PATH)
    replaced = {
        value["replaces_v2_method_ref"]
        for value in package["request_safety_amendments"]["method_overrides"].values()
    }
    composite = (
        set(v2["read_contract"]["methods"])
        - replaced
        | set(package["request_safety_amendments"]["method_overrides"])
        | set(package["request_safety_amendments"]["method_additions"])
    )
    phased = {
        method_ref
        for phase in package["bounded_inventory_envelope_contract"]["phases"]
        for method_ref in phase["allowed_method_refs"]
    }
    schema_methods = set(schema["$defs"]["method_ref"]["enum"])

    assert len(composite) == 32
    assert phased == composite
    assert schema_methods == composite


def test_direct_reads_const_pin_server_side_fields_and_pre_custody_gate():
    safety = _package()["request_safety_amendments"]
    overrides = safety["method_overrides"]

    assert safety["server_side_response_filter"]["system_parameter"] == "fields"
    assert safety["raw_response_gate"]["pre_gate_raw_body_destinations"] == []
    assert safety["raw_response_gate"]["pre_gate_logging_allowed"] is False
    assert safety["raw_response_gate"]["sequence"][-2].startswith(
        "reject_any_unredacted_exception"
    )
    assert safety["raw_response_gate"]["sequence"][-1].startswith(
        "only_then_project"
    )

    cloud_run = overrides["cloud_run_service_get_v3"]
    assert cloud_run["path"].endswith(
        "?fields=createTime,etag,generation,name,observedGeneration,uid,updateTime"
    )
    assert {
        "template",
        "buildConfig",
        "traffic",
        "uri",
        "urls",
    } <= set(cloud_run["forbidden_response_paths"])
    assert set(cloud_run["exact_response_field_mask"]).isdisjoint(
        cloud_run["forbidden_response_paths"]
    )

    rtdb_list = overrides["firebase_rtdb_instance_list_v3"]
    rtdb_get = overrides["firebase_rtdb_instance_get_v3"]
    assert "fields=instances(name,project,state,type),nextPageToken" in rtdb_list["path"]
    assert "fields=name,project,state,type" in rtdb_get["path"]
    assert rtdb_list["forbidden_response_paths"] == ["instances.databaseUrl"]
    assert rtdb_get["forbidden_response_paths"] == ["databaseUrl"]


def test_coverage_statements_are_narrow_and_ancestor_sinks_are_explicit():
    amendments = _package()["request_safety_amendments"]
    scope = amendments["coverage_scope"]
    sink = amendments["method_additions"]["ancestor_log_sink_asset_search"]

    assert "not_all_enabled_services" in scope["required_api_state"]
    assert "not_key_configuration" in scope["kms_evidence"]
    assert "bound_ancestor" in scope["audit_sink_evidence"]
    assert scope["scope_overstatement_forbidden"] is True
    assert "EACH_EXACT_BOUND_PROJECT_FOLDER_OR_ORGANIZATION_SCOPE" in sink["path"]
    assert sink["pagination"] == "single_page_or_incomplete"


def test_effective_access_covers_both_principals_and_all_isolation_edges():
    access = _package()["effective_access_contract"]
    data_tuples = {tuple(item) for item in access["data_resource_tuples"]}
    run_tuples = {tuple(item) for item in access["cloud_run_tuples"]}

    assert set(access["principals"]) == {"control_principal", "preauth_principal"}
    assert len(data_tuples) == 8
    assert len(run_tuples) == 4
    assert (
        "control_principal",
        "//firestore.googleapis.com/projects/hk-voice-bakeoff-preauth-iso/"
        "databases/voice-bakeoff-preauth",
        "NOT_GRANTED",
    ) in data_tuples
    assert (
        "preauth_principal",
        "//firestore.googleapis.com/projects/hk-voice-bakeoff-0724-iso/"
        "databases/voice-bakeoff-control",
        "NOT_GRANTED",
    ) in data_tuples
    assert all(
        tuple_item[2] == "NOT_GRANTED"
        for tuple_item in run_tuples
    )
    assert {
        "resourcemanager.projects.setIamPolicy",
        "run.services.create",
        "serviceusage.services.enable",
    } == set(access["project_level_permissions"])
    assert {
        "iam.serviceAccounts.actAs",
        "iam.serviceAccounts.getAccessToken",
        "iam.serviceAccounts.implicitDelegation",
        "iam.serviceAccounts.signBlob",
        "iam.serviceAccounts.signJwt",
    } == set(access["dynamic_service_account_permissions"])
    assert "secretmanager.versions.access" in access["enumerated_secret_tuple_rule"]
    assert "cloudkms.cryptoKeyVersions.useToDecrypt" in (
        access["enumerated_kms_tuple_rule"]
    )
    assert access["unknown_denied_partial_unsupported_or_over_cap_result"] == (
        "incomplete"
    )


def test_all_operator_examples_render_the_same_complete_field_set():
    operator = _package()["operator_targeting_contract"]
    required = set(operator["required_banner_fields"])

    assert len(required) == 19
    assert "response_field_mask" in required
    assert "request_field_mask" not in required
    assert len(operator["status_examples"]) == 4
    assert all(
        set(example["fields"]) == required
        for example in operator["status_examples"]
    )
    blocked = next(
        example
        for example in operator["status_examples"]
        if example["example"] == "blocked_incomplete"
    )
    assert blocked["fields"]["connected_read_status"] == "not_authorized"
    assert blocked["fields"]["execution_status"] == "not_authorized"


def test_authenticated_receipt_passes_schema_and_semantic_verifier():
    receipt = _receipt_fixture()
    envelope = _envelope_fixture()
    schema = _load_json(_SCHEMA_PATH)

    assert _schema_errors(
        receipt,
        schema["$defs"]["payload_safe_phase_receipt"],
        root=schema,
    ) == []
    assert _semantic_errors(
        receipt,
        expected_manifest=envelope["requests"],
        envelope_nonce_seed=envelope["envelope_nonce_seed"],
        verification_time_ms=1_500,
    ) == []


def test_owner_phase_envelope_is_schema_closed_signed_bounded_and_exact():
    envelope = _envelope_fixture()
    schema = _load_json(_SCHEMA_PATH)

    assert _schema_errors(
        envelope,
        schema["$defs"]["phase_envelope"],
        root=schema,
    ) == []
    assert _envelope_semantic_errors(
        envelope,
        verification_time_ms=1_500,
    ) == []

    wrong_phase_method = deepcopy(envelope)
    wrong_phase_method["requests"][0]["method_ref"] = "policy_troubleshooter"
    unsigned_request = {
        key: value
        for key, value in wrong_phase_method["requests"][0].items()
        if key != "canonical_request_digest"
    }
    wrong_phase_method["requests"][0]["canonical_request_digest"] = (
        _subtree_digest(unsigned_request)
    )
    wrong_phase_method["request_set_digest"] = _subtree_digest(
        wrong_phase_method["requests"]
    )
    _sign_envelope(wrong_phase_method)
    assert "phase_method" in _envelope_semantic_errors(
        wrong_phase_method,
        verification_time_ms=1_500,
    )


def test_receipt_schema_enforces_project_specific_query_identity():
    receipt = _receipt_fixture()
    schema = _load_json(_SCHEMA_PATH)
    receipt["request_receipts"][0]["identity_ref"] = (
        "isolated_bakeoff_operator_identity"
    )
    _sign_receipt(receipt)

    errors = _schema_errors(
        receipt,
        schema["$defs"]["payload_safe_phase_receipt"],
        root=schema,
    )
    assert any("identity_ref:const" in error for error in errors)


def test_semantic_verifier_rejects_false_pass_relations_and_untrusted_data():
    base = _receipt_fixture()
    cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    stale = deepcopy(base)
    stale["expires_at_ms"] = stale["completed_at_ms"] - 1
    _sign_receipt(stale)
    cases.append(("timestamp_order", stale, {}))

    wrong_contract = deepcopy(base)
    wrong_contract["v3_contract_digest"] = "f" * 64
    _sign_receipt(wrong_contract)
    cases.append(("contract_digest", wrong_contract, {}))

    wrong_count = deepcopy(base)
    wrong_count["request_count"] = 2
    _sign_receipt(wrong_count)
    cases.append(("request_count_receipts", wrong_count, {}))

    unconsumed = deepcopy(base)
    unconsumed["request_receipts"][0]["nonce_consumed_before_dispatch"] = False
    _sign_receipt(unconsumed)
    cases.append(("nonce_unconsumed", unconsumed, {}))

    tampered = deepcopy(base)
    tampered["completed_at_ms"] += 1
    cases.append(("receipt_signature", tampered, {}))

    cases.append(("authorization_signature", deepcopy(base), {
        "authorization_signature_valid": False
    }))

    for expected_error, receipt, kwargs in cases:
        envelope = _envelope_fixture()
        errors = _semantic_errors(
            receipt,
            expected_manifest=envelope["requests"],
            envelope_nonce_seed=envelope["envelope_nonce_seed"],
            verification_time_ms=1_500,
            **kwargs,
        )
        assert any(expected_error in error for error in errors), (
            expected_error,
            errors,
        )


def test_receipts_forbid_free_text_errors_and_accept_many_to_many_claims():
    receipt = _receipt_fixture()
    schema = _load_json(_SCHEMA_PATH)
    receipt["result"] = "incomplete"
    receipt["error_codes"] = ["raw member user@example.com"]
    _sign_receipt(receipt)
    errors = _schema_errors(
        receipt,
        schema["$defs"]["payload_safe_phase_receipt"],
        root=schema,
    )
    assert any("error_codes" in error and error.endswith(":enum") for error in errors)

    many_to_many = _receipt_fixture()
    claim_base = {
        "local_configuration_digest": "8" * 64,
        "project_id": "kevin-491315",
        "project_number": "123456",
        "ancestry_digest": "9" * 64,
        "visibility_result": "visible",
        "evidence_digest": "a" * 64,
        "observed_at_ms": 1_100,
        "expires_at_ms": 1_900,
    }
    many_to_many["visibility_claims"] = [
        {**claim_base, "identity_ref": "organization_operator_identity"},
        {
            **claim_base,
            "identity_ref": "isolated_bakeoff_operator_identity",
            "local_configuration_digest": "b" * 64,
            "evidence_digest": "c" * 64,
        },
    ]
    _sign_receipt(many_to_many)
    assert _schema_errors(
        many_to_many,
        schema["$defs"]["payload_safe_phase_receipt"],
        root=schema,
    ) == []


def test_transition_and_guide_keep_every_connected_or_mutating_action_sealed():
    package = _package()
    guide = _GUIDE_PATH.read_text(encoding="utf-8")
    transition = package["transition_policy"]

    assert transition["current_action"] == "retain_all_four_projects_frozen"
    assert transition["new_project_action"] == "forbidden"
    assert transition["after_v3_review"].startswith(
        "generate_but_do_not_execute"
    )
    assert transition["mutation_execution_and_task_4_8"].startswith(
        "remain_sealed"
    )
    for phrase in (
        "Do not create another Google Cloud project.",
        "V3 defines the contract only.",
        "Do not authorize V3 itself.",
        "keep Task 4.8 sealed",
    ):
        assert phrase in guide
