"""Pure, fail-closed validation for the synthetic-only Task 4.8 package.

This module accepts bytes and source-identity facts from its caller.  It never
opens files, reads configuration, starts a process, or creates a client.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal


OPERATOR_BANNER = (
    "Synthetic preparation only — no runtime authorization — do not use for calls."
)
BASELINE_COMMIT_SHA = "13e105cb533ef611d4a9e5df0e30bb2c9c06e5b3"
BASELINE_TREE_SHA = "8656b9ed41b2ee4df7c149d865695c4c17a0309d"
MANIFEST_PATH = "docs/security/task-4-8-synthetic-preparation.manifest.json"
OVERVIEW_PATH = "docs/security/task-4-8-synthetic-preparation.md"
VALIDATOR_PATH = "tests/support/task_4_8_synthetic_preparation.py"
STATIC_GATE_PATH = "tests/support/task_4_8_synthetic_preparation_static_gate.py"
TEST_PATH = "tests/unit/test_task_4_8_synthetic_preparation.py"
DIGESTED_ARTIFACT_PATHS = (
    OVERVIEW_PATH,
    STATIC_GATE_PATH,
    VALIDATOR_PATH,
    TEST_PATH,
)
CHANGED_PATHS = (MANIFEST_PATH, *DIGESTED_ARTIFACT_PATHS)

GATE_ROWS = (
    (
        "sealed_owner_runtime_authorization",
        "owner_runtime_authorizer",
        "sealed_runtime_record",
    ),
    (
        "independent_technical_review",
        "independent_staff_engineer",
        "independent_technical_disposition",
    ),
    (
        "physically_separate_preauth_store",
        "separate_preauth_system_owner",
        "separate_preauth_store_attestation",
    ),
    (
        "identity_and_credential_broker",
        "identity_security_owner",
        "credential_broker_attestation",
    ),
    (
        "durable_trust_and_revocation_store",
        "trust_and_revocation_owner",
        "trust_and_revocation_attestation",
    ),
    (
        "provider_privacy_and_region_attestations",
        "privacy_and_provider_owner",
        "provider_privacy_region_attestation",
    ),
    (
        "complete_production_denylist",
        "production_safety_owner",
        "production_denylist_attestation",
    ),
    (
        "immutable_custody_and_residue_routing",
        "custody_and_residue_owner",
        "custody_and_residue_attestation",
    ),
    (
        "one_use_runtime_envelope",
        "one_use_envelope_issuer",
        "sealed_one_use_envelope",
    ),
)

_ROOT_KEYS = {
    "artifact_digests",
    "baseline",
    "data_scope",
    "execution_status",
    "execution_supported",
    "format",
    "gate_matrix",
    "operator_banner",
    "owner_direction",
    "package_status",
}
_BASELINE_KEYS = {"main_commit_sha", "main_tree_sha"}
_OWNER_DIRECTION_KEYS = {
    "package_preparation_consent",
    "sealed_owner_runtime_authorization",
}
_ARTIFACT_KEYS = {"path", "sha256"}
_GATE_KEYS = {
    "consequence",
    "gate_id",
    "missing_evidence",
    "required_external_authority",
    "status",
}
_SENSITIVE_PATTERNS = (
    re.compile(r"(?:https?|wss?)://", re.IGNORECASE),
    re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])"),
    re.compile(r"(?<![0-9+])\+[1-9][0-9]{7,14}(?![0-9])"),
    re.compile(r"(?<![0-9])\(?[2-9][0-9]{2}\)?[-. ][0-9]{3}[-. ][0-9]{4}(?![0-9])"),
    re.compile(r"\b(?:[0-9a-f]{0,4}:){2,}[0-9a-f:]+\b", re.IGNORECASE),
    re.compile(r"\b(?:[a-z0-9-]+\.)+(?:ai|app|cloud|com|dev|invalid|io|net|org)\b", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b(?:sk|rk|pk)_[A-Za-z0-9]{16,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[^\s]+",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:AC|CA|CH)[0-9a-f]{32}\b", re.IGNORECASE),
    re.compile(r"\bprojects/[A-Za-z0-9_-]+", re.IGNORECASE),
    re.compile(
        r"\b(?:account|credential|endpoint|identity|provider|resource)[_-]?(?:id|key|name|ref)\s*[:=]\s*[^\s]+",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True, slots=True)
class CandidateChange:
    """One caller-supplied Git change fact; the validator does not invoke Git."""

    status: str
    path: str
    old_path: str | None
    mode: str
    file_kind: str


@dataclass(frozen=True, slots=True)
class PackageDiagnostic:
    """Payload-safe failure information: category and location, never content."""

    category: str
    location: str


@dataclass(frozen=True, slots=True)
class PackageValidation:
    status: Literal["invalid_local_package", "not_authorized"]
    execution_status: Literal["not_authorized"]
    diagnostics: tuple[PackageDiagnostic, ...]


class _DuplicateJsonKey(ValueError):
    pass


def _no_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value.update({key: item})
    return value


def _canonical_json(value: object) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return encoded.encode("utf-8") + b"\n"


def _sha256(value: bytes) -> str:
    digest = hashlib.sha256(value)
    return digest.hexdigest()


def _diagnostic(category: str, location: str) -> PackageDiagnostic:
    return PackageDiagnostic(category=category, location=location)


def _parse_manifest(raw: bytes) -> tuple[object | None, tuple[PackageDiagnostic, ...]]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey):
        return None, (_diagnostic("manifest_encoding", MANIFEST_PATH),)
    if raw != _canonical_json(value):
        return None, (_diagnostic("manifest_canonicalization", MANIFEST_PATH),)
    return value, ()


def _is_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _has_exact_keys(value: object, keys: set[str]) -> bool:
    return type(value) is dict and set(value) == keys


def _validate_schema(value: object) -> tuple[PackageDiagnostic, ...]:
    errors: list[PackageDiagnostic] = []
    if not _has_exact_keys(value, _ROOT_KEYS):
        return (_diagnostic("schema_root", MANIFEST_PATH),)
    assert type(value) is dict
    if value["format"] != "preparation_manifest_v1":
        errors.append(_diagnostic("schema_format", "format"))
    if value["package_status"] != "synthetic_preparation_only":
        errors.append(_diagnostic("package_state", "package_status"))
    if value["execution_supported"] is not False:
        errors.append(_diagnostic("execution_capability", "execution_supported"))
    if value["execution_status"] != "not_authorized":
        errors.append(_diagnostic("execution_state", "execution_status"))
    if value["data_scope"] != "synthetic_only":
        errors.append(_diagnostic("data_scope", "data_scope"))
    if value["operator_banner"] != OPERATOR_BANNER:
        errors.append(_diagnostic("operator_banner", "operator_banner"))

    baseline = value["baseline"]
    if not _has_exact_keys(baseline, _BASELINE_KEYS):
        errors.append(_diagnostic("schema_baseline", "baseline"))
    else:
        assert type(baseline) is dict
        if baseline["main_commit_sha"] != BASELINE_COMMIT_SHA:
            errors.append(_diagnostic("baseline_commit", "baseline.main_commit_sha"))
        if baseline["main_tree_sha"] != BASELINE_TREE_SHA:
            errors.append(_diagnostic("baseline_tree", "baseline.main_tree_sha"))
        if not _is_hex(baseline["main_commit_sha"], 40):
            errors.append(_diagnostic("baseline_commit", "baseline.main_commit_sha"))
        if not _is_hex(baseline["main_tree_sha"], 40):
            errors.append(_diagnostic("baseline_tree", "baseline.main_tree_sha"))

    owner_direction = value["owner_direction"]
    if not _has_exact_keys(owner_direction, _OWNER_DIRECTION_KEYS):
        errors.append(_diagnostic("schema_owner_direction", "owner_direction"))
    else:
        assert type(owner_direction) is dict
        if owner_direction["package_preparation_consent"] != "recorded":
            errors.append(
                _diagnostic(
                    "owner_preparation_consent",
                    "owner_direction.package_preparation_consent",
                )
            )
        if owner_direction["sealed_owner_runtime_authorization"] != "missing":
            errors.append(
                _diagnostic(
                    "runtime_authorization",
                    "owner_direction.sealed_owner_runtime_authorization",
                )
            )

    artifacts = value["artifact_digests"]
    if not isinstance(artifacts, list) or len(artifacts) != len(DIGESTED_ARTIFACT_PATHS):
        errors.append(_diagnostic("schema_artifact_digests", "artifact_digests"))
    else:
        paths: list[str] = []
        for index, artifact in enumerate(artifacts):
            location = f"artifact_digests[{index}]"
            if not _has_exact_keys(artifact, _ARTIFACT_KEYS):
                errors.append(_diagnostic("schema_artifact", location))
                continue
            assert type(artifact) is dict
            path = artifact["path"]
            digest = artifact["sha256"]
            if not isinstance(path, str) or path not in DIGESTED_ARTIFACT_PATHS:
                errors.append(_diagnostic("artifact_path", f"{location}.path"))
                continue
            paths.append(path)
            if not _is_hex(digest, 64):
                errors.append(_diagnostic("artifact_digest", f"{location}.sha256"))
        if tuple(paths) != DIGESTED_ARTIFACT_PATHS:
            errors.append(_diagnostic("artifact_order", "artifact_digests"))

    gates = value["gate_matrix"]
    if not isinstance(gates, list) or len(gates) != len(GATE_ROWS):
        errors.append(_diagnostic("schema_gate_matrix", "gate_matrix"))
    else:
        for index, (gate, expected) in enumerate(zip(gates, GATE_ROWS, strict=True)):
            location = f"gate_matrix[{index}]"
            if not _has_exact_keys(gate, _GATE_KEYS):
                errors.append(_diagnostic("schema_gate", location))
                continue
            assert type(gate) is dict
            gate_id, authority, evidence = expected
            if gate["gate_id"] != gate_id:
                errors.append(_diagnostic("gate_identity", f"{location}.gate_id"))
            if gate["status"] != "unmet_external":
                errors.append(_diagnostic("gate_state", f"{location}.status"))
            if gate["required_external_authority"] != authority:
                errors.append(_diagnostic("gate_authority", location))
            if gate["missing_evidence"] != evidence:
                errors.append(_diagnostic("gate_evidence", location))
            if gate["consequence"] != "runtime_blocked":
                errors.append(_diagnostic("gate_consequence", location))
    return tuple(errors)


def _source_diagnostics(path: str, data: bytes) -> tuple[PackageDiagnostic, ...]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return (_diagnostic("artifact_encoding", path),)
    for line_number, line in enumerate(text.splitlines(), start=1):
        if any(pattern.search(line) for pattern in _SENSITIVE_PATTERNS):
            return (_diagnostic("sensitive_literal", f"{path}:{line_number}"),)
    return ()


def _validate_artifacts(
    manifest: dict[str, object],
    artifacts: object,
    manifest_bytes: bytes,
) -> tuple[PackageDiagnostic, ...]:
    if type(artifacts) is not dict:
        return (_diagnostic("artifact_input_type", "artifacts"),)
    if any(type(path) is not str or type(data) is not bytes for path, data in artifacts.items()):
        return (_diagnostic("artifact_input_type", "artifacts"),)
    if set(artifacts) != set(CHANGED_PATHS):
        return (_diagnostic("artifact_allowlist", "artifacts"),)
    declared = manifest["artifact_digests"]
    if not isinstance(declared, list):
        return (_diagnostic("schema_artifact_digests", "artifact_digests"),)
    errors: list[PackageDiagnostic] = []
    if artifacts[MANIFEST_PATH] != manifest_bytes:
        errors.append(_diagnostic("manifest_artifact_binding", MANIFEST_PATH))
    for path in CHANGED_PATHS:
        errors.extend(_source_diagnostics(path, artifacts[path]))
    for artifact in declared:
        if type(artifact) is not dict:
            continue
        path = artifact["path"]
        digest = artifact["sha256"]
        if not isinstance(path, str) or not isinstance(digest, str):
            continue
        if path in artifacts and _sha256(artifacts[path]) != digest:
            errors.append(_diagnostic("artifact_digest", path))
    return tuple(errors)


def _validate_candidate_changes(
    changes: object,
) -> tuple[PackageDiagnostic, ...]:
    if type(changes) is not tuple:
        return (_diagnostic("candidate_input_type", "candidate_diff"),)
    if len(changes) != len(CHANGED_PATHS):
        return (_diagnostic("candidate_scope", "candidate_diff"),)
    paths: list[str] = []
    for change in changes:
        if type(change) is not CandidateChange:
            return (_diagnostic("candidate_input_type", "candidate_diff"),)
        paths.append(change.path)
        if (
            type(change.status) is not str
            or type(change.path) is not str
            or (change.old_path is not None and type(change.old_path) is not str)
            or type(change.mode) is not str
            or type(change.file_kind) is not str
            or change.status != "A"
            or change.old_path is not None
            or change.mode != "100644"
            or change.file_kind != "regular"
            or change.path not in CHANGED_PATHS
        ):
            return (_diagnostic("candidate_scope", change.path),)
    if tuple(sorted(paths)) != tuple(sorted(CHANGED_PATHS)):
        return (_diagnostic("candidate_scope", "candidate_diff"),)
    return ()


def validate_synthetic_preparation(
    *,
    manifest_bytes: object,
    artifacts: object,
    candidate_changes: object,
) -> PackageValidation:
    """Validate only local preparation facts; this always denies execution."""
    if type(manifest_bytes) is not bytes:
        return PackageValidation(
            status="invalid_local_package",
            execution_status="not_authorized",
            diagnostics=(_diagnostic("manifest_input_type", MANIFEST_PATH),),
        )
    manifest, errors = _parse_manifest(manifest_bytes)
    if manifest is not None:
        errors += _validate_schema(manifest)
        if type(manifest) is dict:
            errors += _validate_artifacts(manifest, artifacts, manifest_bytes)
    errors += _validate_candidate_changes(candidate_changes)
    diagnostics = tuple(sorted(set(errors), key=lambda item: (item.category, item.location)))
    return PackageValidation(
        status="invalid_local_package" if diagnostics else "not_authorized",
        execution_status="not_authorized",
        diagnostics=diagnostics,
    )


__all__ = [
    "BASELINE_COMMIT_SHA",
    "BASELINE_TREE_SHA",
    "CHANGED_PATHS",
    "CandidateChange",
    "DIGESTED_ARTIFACT_PATHS",
    "GATE_ROWS",
    "MANIFEST_PATH",
    "OPERATOR_BANNER",
    "OVERVIEW_PATH",
    "PackageDiagnostic",
    "PackageValidation",
    "TEST_PATH",
    "VALIDATOR_PATH",
    "STATIC_GATE_PATH",
    "validate_synthetic_preparation",
]
