"""Offline guards for fail-closed Phase 0.5 v7 payload materialization."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import pytest

from tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v3 import (
    _schema_errors,
)
from tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v4 import (
    _digest,
)
from tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v6 import (
    _PROJECT_IDS,
    _REQUIRED_INPUTS,
    _generation_errors as _v6_generation_errors,
    _private_fixture_inputs,
    _request,
)


_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_PATH = _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v7.json"
_SCHEMA_PATH = (
    _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v7.schema.json"
)
_GUIDE_PATH = _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v7.md"
_V6_PACKAGE_PATH = (
    _ROOT / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v6.json"
)
_V6_HASHES = {
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v6.json": (
        "be8f7edc86705ac26d09184c05a846e724a389860bc9660fc43e191c55bdd8c7"
    ),
    (
        "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v6.schema.json"
    ): "8857b4948859c85573d2f42f927861af8790de4690274c0c12777c12a0ad8abd",
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v6.md": (
        "f218456cd14cdb329e913dffdc1a0bc81028649ca43f33045051c9b66db1b9e9"
    ),
    "tests/unit/test_voice_bakeoff_environment_reconciliation_phase_0_5_v6.py": (
        "e4fa5f16da3fc14bb9480df3e945fccb22f9308c8079b33b22c13d3441e6ed2f"
    ),
}
_DIGEST_INPUTS = (
    "owner_public_key_digest",
    "organization_operator_identity_configuration_digest",
    "isolated_bakeoff_operator_identity_configuration_digest",
    "raw_evidence_custody_digest",
    "one_use_nonce_seed",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _package() -> dict[str, Any]:
    return _load_json(_PACKAGE_PATH)


def _schema() -> dict[str, Any]:
    return _load_json(_SCHEMA_PATH)


def _candidate_errors(payload: dict[str, Any]) -> list[str]:
    schema = _schema()
    errors = _schema_errors(
        payload,
        schema["$defs"]["phase_one_signing_payload_v7"],
        root=schema,
    )

    v6_projection = deepcopy(payload)
    v6_projection["payload_version"] = 6
    v6_projection["v6_normative_contract_digest"] = _load_json(_V6_PACKAGE_PATH)[
        "normative_contract_binding"
    ]["normative_contract_digest_sha256"]
    v6_projection.pop("v6_json_digest")
    v6_projection.pop("v7_normative_contract_digest")
    errors.extend(_v6_generation_errors(v6_projection))
    return errors


def materialize_validated_phase_one_payload(
    inputs: dict[str, Any],
    *,
    validation_time_ms: int,
) -> dict[str, Any]:
    """Return a complete validated candidate or raise without returning one."""
    package = _package()
    if set(inputs) != _REQUIRED_INPUTS:
        raise ValueError("private_generation_inputs_not_exact")
    if type(validation_time_ms) is not int:
        raise ValueError("validation_time_ms_not_exact_integer")
    if not _UUID4_RE.fullmatch(str(inputs["owner_selected_inventory_session_uuidv4"])):
        raise ValueError("inventory_session_not_uuidv4")
    for field in _DIGEST_INPUTS:
        value = inputs[field]
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise ValueError(f"{field}_not_lowercase_sha256")

    issued = inputs["issued_at_ms"]
    expires = inputs["expires_at_ms"]
    if type(issued) is not int or type(expires) is not int:
        raise ValueError("payload_time_not_exact_integer")
    if not issued <= validation_time_ms < expires:
        raise ValueError("payload_not_fresh_at_validation_time")
    maximum_lifetime = package["validated_materialization_contract"]["maximum_payload_lifetime_ms"]
    if expires - issued > maximum_lifetime:
        raise ValueError("payload_lifetime_exceeds_cap")

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
    payload = {
        "payload_version": 7,
        "payload_kind": "unsigned_owner_signing_payload",
        "inventory_session_id": inputs["owner_selected_inventory_session_uuidv4"],
        "phase": "project_id_binding",
        "phase_ordinal": 1,
        "predecessor": "GENESIS",
        "source_sha": package["source_binding"]["source_sha"],
        "v2_json_digest": package["normative_contract_binding"]["v2_json_sha256"],
        "v5_json_digest": package["normative_contract_binding"]["v5_json_sha256"],
        "v6_json_digest": package["normative_contract_binding"]["v6_json_sha256"],
        "v7_normative_contract_digest": package["normative_contract_binding"][
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
        "issued_at_ms": issued,
        "expires_at_ms": expires,
        "audit_and_quota_effects_acknowledged": True,
    }
    errors = _candidate_errors(payload)
    if errors:
        raise ValueError("candidate_invalid:" + ",".join(sorted(set(errors))))
    return payload


def test_v7_root_schema_and_normative_digest_are_exact():
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

    for subtree in (
        "authority",
        "environment_recommendation",
        "validated_materialization_contract",
        "phase_one_payload_contract",
        "dispatch_and_later_phase_blockers",
        "transition_policy",
    ):
        changed = deepcopy(package)
        changed[subtree]["unexpected_mutation"] = True
        assert _schema_errors(changed, schema, root=schema)


def test_validated_materializer_returns_only_one_complete_unsigned_payload():
    payload = materialize_validated_phase_one_payload(
        _private_fixture_inputs(),
        validation_time_ms=1_500,
    )
    assert _candidate_errors(payload) == []
    assert [request["target_project_id"] for request in payload["requests"]] == (list(_PROJECT_IDS))
    assert "owner_signature" not in payload
    assert "owner_authorization" not in payload


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        (
            {"owner_selected_inventory_session_uuidv4": "not-a-uuid"},
            "uuidv4",
        ),
        ({"owner_public_key_digest": "A" * 64}, "sha256"),
        ({"owner_public_key_digest": "0" * 63}, "sha256"),
        ({"issued_at_ms": True}, "exact_integer"),
        ({"expires_at_ms": 2_000.0}, "exact_integer"),
        ({"issued_at_ms": 1_600}, "not_fresh"),
        ({"expires_at_ms": 1_500}, "not_fresh"),
        ({"expires_at_ms": 1_000}, "not_fresh"),
        ({"expires_at_ms": 901_001}, "lifetime"),
    ),
)
def test_invalid_private_inputs_raise_before_any_payload_is_returned(
    mutation: dict[str, Any],
    error: str,
):
    inputs = _private_fixture_inputs()
    inputs.update(mutation)
    with pytest.raises(ValueError, match=error):
        materialize_validated_phase_one_payload(
            inputs,
            validation_time_ms=1_500,
        )


@pytest.mark.parametrize("validation_time_ms", (True, 1_500.0, 2_000))
def test_invalid_validation_context_raises_without_payload(
    validation_time_ms: Any,
):
    with pytest.raises(ValueError):
        materialize_validated_phase_one_payload(
            _private_fixture_inputs(),
            validation_time_ms=validation_time_ms,
        )


def test_missing_extra_and_credential_inputs_raise_without_payload():
    for mutation in ("missing", "extra", "credential"):
        inputs = _private_fixture_inputs()
        if mutation == "missing":
            inputs.pop("owner_public_key_digest")
        elif mutation == "extra":
            inputs["unreviewed"] = True
        else:
            inputs["credentials"] = "forbidden"
        with pytest.raises(ValueError, match="not_exact"):
            materialize_validated_phase_one_payload(
                inputs,
                validation_time_ms=1_500,
            )


def test_schema_and_relational_failure_cannot_escape_materializer(monkeypatch):
    original_request = _request

    def invalid_request(
        *,
        index: int,
        project_id: str,
        configs: dict[str, str],
    ) -> dict[str, Any]:
        request = original_request(
            index=index,
            project_id=project_id,
            configs=configs,
        )
        request["http_method"] = "POST"
        request["canonical_request_digest"] = _digest(
            {key: value for key, value in request.items() if key != "canonical_request_digest"}
        )
        return request

    monkeypatch.setattr(
        "tests.unit.test_voice_bakeoff_environment_reconciliation_phase_0_5_v7._request",
        invalid_request,
    )
    with pytest.raises(ValueError, match="candidate_invalid"):
        materialize_validated_phase_one_payload(
            _private_fixture_inputs(),
            validation_time_ms=1_500,
        )


def test_v7_preserves_v6_and_keeps_every_connected_action_sealed():
    for relative_path, expected_hash in _V6_HASHES.items():
        assert hashlib.sha256((_ROOT / relative_path).read_bytes()).hexdigest() == (expected_hash)

    package = _package()
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
    assert "Do not authorize V7 itself" in guide
    assert "error with no payload" in guide
    assert "task_4_8_status              sealed" in guide
