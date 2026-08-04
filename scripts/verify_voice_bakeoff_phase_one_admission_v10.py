"""Verify the public, fail-closed V10 phase-one acceptance contract.

This verifier is intentionally incapable of materializing a payload. It imports
only the Python standard library, reads only public repository files, and creates
no network, subprocess, credential, provider, PSTN, staging, production, or
Task 4.8 capability.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_SHA = "2ed8ea7d1d7f338e84ddf08d5a50a714835e1533"
_CONTRACT_PATH = (
    "docs/security/"
    "voice-bakeoff-environment-reconciliation-phase-0-5-v10.json"
)
_SCHEMA_PATH = (
    "docs/security/"
    "voice-bakeoff-environment-reconciliation-phase-0-5-v10.schema.json"
)
_GUIDE_PATH = (
    "docs/security/"
    "voice-bakeoff-environment-reconciliation-phase-0-5-v10.md"
)
_EXPECTED_PREDECESSORS = (
    (
        "docs/security/"
        "voice-bakeoff-environment-reconciliation-phase-0-5-v9.json",
        "737236646d066ecc50149d89af94fdca83ec28006c8fee44ca37323a858dc7e1",
    ),
    (
        "docs/security/"
        "voice-bakeoff-environment-reconciliation-phase-0-5-v9.md",
        "7f718ac29e70bcb222a2ca847b1419ce0891f4956d21d446a27eeb273d021523",
    ),
    (
        "docs/security/"
        "voice-bakeoff-environment-reconciliation-phase-0-5-v9.predecessors.json",
        "24ad05b112b5f26cb7b8918a1e6a23a3a49fa82efdb687630b920efe02db3dd5",
    ),
    (
        "docs/security/"
        "voice-bakeoff-environment-reconciliation-phase-0-5-v9.schema.json",
        "a794dd4c78157a0287246b41410e6d05187eaf70627e443d512cff68d644a962",
    ),
    (
        "scripts/materialize_voice_bakeoff_phase_one_payload_v9.py",
        "f9087cfe7dc905bb409f9ba887ddecaaad25164690d40975a03c8bac096124e1",
    ),
    (
        "tests/unit/"
        "test_voice_bakeoff_environment_reconciliation_phase_0_5_v9.py",
        "c5112823b600372ff30e817171d0b9d75958cc771d70ef2b41a51e2271eca99e",
    ),
)
_ALLOWED_ACTIONS = [
    "inspect_public_contracts",
    "run_offline_static_verifier",
    "run_fixture_only_tests",
    "obtain_exact_hash_advisory_review",
]
_FORBIDDEN_ACTIONS = [
    "invoke_v9_materializer",
    "create_real_private_input",
    "create_real_materialization_authorization",
    "access_credentials",
    "access_provider_or_pstn",
    "access_staging_or_production",
    "execute_task_4_8",
]
_LOCAL_IDS = tuple(f"L{index:02d}" for index in range(1, 9))
_EXTERNAL_IDS = tuple(f"E{index:02d}" for index in range(1, 6))
_STATES = (
    "pre_admission_rejected",
    "consumed_no_payload",
    "consumed_with_residue_stop",
    "generated_one_payload",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_BYTES = 1_048_576


class VerificationError(Exception):
    """A public-contract verification failure."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VerificationError("canonicalization_failed") from exc


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: object) -> str:
    return _digest_bytes(_canonical(value))


def _read_public_file(repo_root: Path, relative_path: str) -> bytes:
    path = repo_root / relative_path
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise VerificationError("public_file_unreadable") from exc
    if len(value) > _MAX_BYTES:
        raise VerificationError("public_file_too_large")
    if not path.is_file() or path.is_symlink():
        raise VerificationError("public_file_not_regular")
    return value


def _load_object(repo_root: Path, relative_path: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_public_file(repo_root, relative_path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("public_json_invalid") from exc
    if not isinstance(value, dict):
        raise VerificationError("public_json_invalid")
    return value


def _verify_schema_shape(schema: dict[str, Any]) -> None:
    if (
        schema.get("$schema")
        != "https://json-schema.org/draft/2020-12/schema"
        or schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
    ):
        raise VerificationError("schema_not_closed")
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise VerificationError("schema_invalid")
    if set(properties) != set(required):
        raise VerificationError("schema_topology_invalid")
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise VerificationError("schema_definitions_missing")
    for definition in definitions.values():
        if isinstance(definition, dict) and definition.get("type") == "object":
            if definition.get("additionalProperties") is not False:
                raise VerificationError("schema_nested_object_open")


def _verify_requirements(
    requirements: object,
    *,
    expected_ids: tuple[str, ...],
) -> None:
    if not isinstance(requirements, list):
        raise VerificationError("requirements_invalid")
    observed_ids: list[str] = []
    for item in requirements:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "requirement",
            "status",
            "blocking",
        }:
            raise VerificationError("requirement_invalid")
        observed_ids.append(item["id"])
        if (
            not isinstance(item["requirement"], str)
            or not item["requirement"]
            or item["status"] != "not_satisfied"
            or item["blocking"] is not True
        ):
            raise VerificationError("requirement_not_fail_closed")
    if tuple(observed_ids) != expected_ids:
        raise VerificationError("requirement_ids_not_exact")


def _verify_contract(contract: dict[str, Any], repo_root: Path) -> None:
    expected_keys = {
        "schema_version",
        "contract_kind",
        "source_sha",
        "predecessor_exact_bundle",
        "predecessor_bundle_digest",
        "panel_consensus",
        "decision",
        "allowed_actions",
        "forbidden_actions",
        "local_candidate_requirements",
        "external_ceremony_requirements",
        "future_attempt_event_schema",
        "operator_decision_table",
        "promotion_rule",
    }
    if set(contract) != expected_keys:
        raise VerificationError("contract_keys_invalid")
    if (
        contract["schema_version"] != 10
        or contract["contract_kind"]
        != "voice_bakeoff_phase_0_5_fail_closed_acceptance"
        or contract["source_sha"] != _SOURCE_SHA
        or contract["allowed_actions"] != _ALLOWED_ACTIONS
        or contract["forbidden_actions"] != _FORBIDDEN_ACTIONS
    ):
        raise VerificationError("contract_identity_invalid")

    predecessors = contract["predecessor_exact_bundle"]
    expected_entries = [
        {"path": path, "sha256": digest}
        for path, digest in _EXPECTED_PREDECESSORS
    ]
    if predecessors != expected_entries:
        raise VerificationError("predecessor_bundle_not_exact")
    if contract["predecessor_bundle_digest"] != _digest(predecessors):
        raise VerificationError("predecessor_bundle_digest_invalid")
    for path, expected_digest in _EXPECTED_PREDECESSORS:
        if _digest_bytes(_read_public_file(repo_root, path)) != expected_digest:
            raise VerificationError("predecessor_file_digest_mismatch")

    if contract["panel_consensus"] != {
        "staff": "blocked_real_materialization",
        "security_privacy": "blocked_real_materialization",
        "operator": "blocked_real_materialization",
        "materialization_decision": "blocked",
        "evidence_class": "advisory_non_authorizing",
    }:
        raise VerificationError("panel_consensus_invalid")
    if contract["decision"] != {
        "materialization": "blocked",
        "v9_runner_invocation": "forbidden",
        "connected_actions": "forbidden",
        "product_evidence": "none",
        "v10_scope": "source_only_acceptance_and_status_verification",
    }:
        raise VerificationError("decision_not_fail_closed")

    _verify_requirements(
        contract["local_candidate_requirements"],
        expected_ids=_LOCAL_IDS,
    )
    _verify_requirements(
        contract["external_ceremony_requirements"],
        expected_ids=_EXTERNAL_IDS,
    )
    event_schema = contract["future_attempt_event_schema"]
    if (
        not isinstance(event_schema, dict)
        or event_schema.get("allowed_states") != list(_STATES)
        or event_schema.get("private_values_present") is not False
        or event_schema.get("connected_action_occurred") is not False
    ):
        raise VerificationError("attempt_event_schema_invalid")
    decision_table = contract["operator_decision_table"]
    if (
        not isinstance(decision_table, list)
        or [row.get("state") for row in decision_table] != list(_STATES)
        or any(
            row.get("payload_usable") is not (row["state"] == _STATES[-1])
            for row in decision_table
        )
    ):
        raise VerificationError("operator_decision_table_invalid")
    promotion = contract["promotion_rule"]
    if (
        not isinstance(promotion, dict)
        or promotion.get("all_local_requirements")
        != "independently_verified"
        or promotion.get("all_external_requirements")
        != "independently_attested"
        or promotion.get("exact_successor_review")
        != "staff_security_operator_acceptance"
        or promotion.get("separate_one_use_authorization") != "required"
        or promotion.get("automatic_promotion") is not False
    ):
        raise VerificationError("promotion_rule_invalid")


def verify(repo_root: Path = _REPO_ROOT) -> dict[str, object]:
    """Verify V10 and return only its payload-safe blocked status."""

    repo_root = repo_root.resolve(strict=True)
    contract = _load_object(repo_root, _CONTRACT_PATH)
    schema = _load_object(repo_root, _SCHEMA_PATH)
    _read_public_file(repo_root, _GUIDE_PATH)
    _verify_schema_shape(schema)
    _verify_contract(contract, repo_root)
    return {
        "status": "verified_blocked_contract",
        "materialization": "blocked",
        "v9_runner_invocation": "forbidden",
        "connected_actions": "forbidden",
        "product_evidence": "none",
        "local_requirements_satisfied": 0,
        "external_requirements_satisfied": 0,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        return 64
    try:
        result = verify()
    except VerificationError:
        return 65
    except Exception:
        return 70
    sys.stdout.buffer.write(_canonical(result) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
