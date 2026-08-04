"""Standalone, one-attempt offline V9 payload materialization runner.

This module intentionally imports only the Python standard library and never
imports or executes predecessor project modules. It creates no network client,
credential, signature, connected-read authority, or Task 4.8 capability.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_SHA = "2ed8ea7d1d7f338e84ddf08d5a50a714835e1533"
_V8_JSON_SHA = "4153bf3764600198f583d627ad93e540ce0423cd25fab2c04be2e7b06245a859"
_V9_CONTRACT_DIGEST = "c602a313d0dbb8ee0528aa4a90587f876817d6a7d93fe35a711ad363805325a5"
_PREDECESSOR_MANIFEST_DIGEST = (
    "1c4293996dc10c1a23de7280d2cd7b22e167be041ef2c86eca3daaba76215805"
)
_PHASE_ONE_METHOD_DIGEST = (
    "3244902ecbf20c155ee49836a10a074d28eb6386a4081d56e0963a6868a2b93f"
)
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_AUTHORIZATION_LIFETIME_MS = 900_000
_MAX_JSON_BYTES = 1_048_576

_STATIC_CONTRACT_HASHES = {
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v9.json": (
        "737236646d066ecc50149d89af94fdca83ec28006c8fee44ca37323a858dc7e1"
    ),
    (
        "docs/security/"
        "voice-bakeoff-environment-reconciliation-phase-0-5-v9.schema.json"
    ): "a794dd4c78157a0287246b41410e6d05187eaf70627e443d512cff68d644a962",
    "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v9.md": (
        "7f718ac29e70bcb222a2ca847b1419ce0891f4956d21d446a27eeb273d021523"
    ),
    (
        "docs/security/"
        "voice-bakeoff-environment-reconciliation-phase-0-5-v9.predecessors.json"
    ): "24ad05b112b5f26cb7b8918a1e6a23a3a49fa82efdb687630b920efe02db3dd5",
}
_REVIEW_BUNDLE_PATHS = tuple(
    sorted(
        (
            "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v9.json",
            (
                "docs/security/"
                "voice-bakeoff-environment-reconciliation-phase-0-5-v9.schema.json"
            ),
            "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v9.md",
            (
                "docs/security/"
                "voice-bakeoff-environment-reconciliation-phase-0-5-v9.predecessors.json"
            ),
            "scripts/materialize_voice_bakeoff_phase_one_payload_v9.py",
            "tests/unit/test_voice_bakeoff_environment_reconciliation_phase_0_5_v9.py",
        )
    )
)
_PREDECESSOR_MANIFEST_PATH = (
    "docs/security/"
    "voice-bakeoff-environment-reconciliation-phase-0-5-v9.predecessors.json"
)
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
_AUTHORIZATION_KEYS = {
    "record_version",
    "record_kind",
    "decision",
    "authorization_id",
    "attempt_id",
    "source_sha",
    "v9_normative_contract_digest",
    "predecessor_manifest_digest",
    "review_bundle",
    "review_bundle_digest",
    "review_acceptances",
    "materialization_authority_identity_digest",
    "custodian_identity_digest",
    "ceremony_root_digest",
    "private_input_sha256",
    "issued_at_ms",
    "validation_time_ms",
    "expires_at_ms",
    "failure_consumes_attempt",
    "connected_action_authorized",
    "authorization_digest",
}
_PRIVATE_INPUT_KEYS = {
    "owner_selected_inventory_session_uuidv4",
    "owner_public_key_digest",
    "organization_operator_identity_configuration_digest",
    "isolated_bakeoff_operator_identity_configuration_digest",
    "raw_evidence_custody_digest",
    "one_use_nonce_seed",
    "issued_at_ms",
    "expires_at_ms",
    "audit_and_quota_effects_acknowledged",
}
_PAYLOAD_KEYS = {
    "payload_version",
    "payload_kind",
    "inventory_session_id",
    "phase",
    "phase_ordinal",
    "predecessor",
    "source_sha",
    "v8_json_digest",
    "v9_normative_contract_digest",
    "predecessor_bundle_manifest_digest",
    "v9_review_bundle_digest",
    "materialization_authorization_digest",
    "materialization_attempt_id",
    "phase_one_method_contract_digest",
    "owner_public_key_digest",
    "identity_configuration_digests",
    "raw_evidence_custody_digest",
    "one_use_nonce_seed",
    "request_count",
    "request_set_digest",
    "requests",
    "issued_at_ms",
    "expires_at_ms",
    "audit_and_quota_effects_acknowledged",
}
_REVIEW_ACCEPTANCES = {
    "staff": "accepted_exact_hashes",
    "security_privacy": "accepted_exact_hashes",
    "operator": "accepted_exact_hashes",
    "advisory_only": True,
    "unresolved_p1_count": 0,
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_STABLE_ERROR_RE = re.compile(r"^[a-z0-9_]{1,64}$")

_AUTHORIZATION_NAME = "materialization-authorization.json"
_PRIVATE_INPUT_NAME = "private-input.json"
_ATTEMPT_NAME = "attempt-record.jsonl"
_OUTPUT_NAME = "unsigned-payload.json"
_AUDIT_NAME = "payload-safe-audit.json"


class MaterializationError(Exception):
    """Payload-safe V9 failure carrying only a stable error code."""

    def __init__(self, code: str):
        if not _STABLE_ERROR_RE.fullmatch(code):
            code = "internal_error"
        self.code = code
        super().__init__(code)


def _canonical(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise MaterializationError("canonicalization_failed") from exc
    return rendered.encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MaterializationError(code)
    return value


def _require_uuid4(value: object, code: str) -> str:
    if not isinstance(value, str) or _UUID4_RE.fullmatch(value) is None:
        raise MaterializationError(code)
    return value


def _require_safe_integer(value: object, code: str) -> int:
    if (
        type(value) is not int
        or value < 1
        or value > _MAX_SAFE_INTEGER
    ):
        raise MaterializationError(code)
    return value


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str):
        raise MaterializationError("bundle_path_invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise MaterializationError("bundle_path_invalid")
    return value


def _read_regular_file(
    path: Path,
    *,
    required_mode: int | None,
    require_read_only: bool,
    maximum_bytes: int = _MAX_JSON_BYTES,
) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise MaterializationError("required_file_unreadable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise MaterializationError("required_file_not_regular")
    mode = stat.S_IMODE(before.st_mode)
    if required_mode is not None and mode != required_mode:
        raise MaterializationError("private_file_mode_invalid")
    if require_read_only and mode & 0o222:
        raise MaterializationError("snapshot_file_writable")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MaterializationError("required_file_unreadable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise MaterializationError("snapshot_changed")
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - observed))
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > maximum_bytes:
                raise MaterializationError("required_file_too_large")
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise MaterializationError("snapshot_changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_json_bytes(value: bytes, code: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterializationError(code) from exc
    if not isinstance(parsed, dict):
        raise MaterializationError(code)
    return parsed


def _verify_exact_file(
    repo_root: Path,
    relative_path: str,
    expected_sha256: str,
    *,
    require_read_only: bool,
) -> None:
    value = _read_regular_file(
        repo_root / relative_path,
        required_mode=None,
        require_read_only=require_read_only,
    )
    if _bytes_digest(value) != expected_sha256:
        raise MaterializationError("snapshot_digest_mismatch")


def _verify_static_contract_files(
    repo_root: Path,
    *,
    require_read_only: bool,
) -> None:
    for relative_path, expected_sha256 in sorted(_STATIC_CONTRACT_HASHES.items()):
        _verify_exact_file(
            repo_root,
            relative_path,
            expected_sha256,
            require_read_only=require_read_only,
        )


def _load_predecessor_manifest(
    repo_root: Path,
    *,
    require_read_only: bool,
) -> list[dict[str, str]]:
    raw = _read_regular_file(
        repo_root / _PREDECESSOR_MANIFEST_PATH,
        required_mode=None,
        require_read_only=require_read_only,
    )
    manifest = _load_json_bytes(raw, "predecessor_manifest_invalid")
    if set(manifest) != {
        "schema_version",
        "manifest_kind",
        "manifest_digest_sha256",
        "entries",
    }:
        raise MaterializationError("predecessor_manifest_invalid")
    if (
        manifest["schema_version"] != 9
        or manifest["manifest_kind"]
        != "voice_bakeoff_v9_exact_predecessor_bundle"
        or manifest["manifest_digest_sha256"] != _PREDECESSOR_MANIFEST_DIGEST
    ):
        raise MaterializationError("predecessor_manifest_invalid")
    entries = manifest["entries"]
    if not isinstance(entries, list) or len(entries) != 32:
        raise MaterializationError("predecessor_manifest_invalid")
    if _digest(entries) != _PREDECESSOR_MANIFEST_DIGEST:
        raise MaterializationError("predecessor_manifest_invalid")
    return entries


def _verify_manifest_entries(
    repo_root: Path,
    entries: list[dict[str, str]],
    *,
    expected_paths: tuple[str, ...] | None,
    require_read_only: bool,
) -> None:
    observed_paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise MaterializationError("bundle_entry_invalid")
        relative_path = _validate_relative_path(entry["path"])
        expected_sha256 = _require_sha256(
            entry["sha256"],
            "bundle_digest_invalid",
        )
        observed_paths.append(relative_path)
        _verify_exact_file(
            repo_root,
            relative_path,
            expected_sha256,
            require_read_only=require_read_only,
        )
    if observed_paths != sorted(observed_paths) or len(observed_paths) != len(
        set(observed_paths)
    ):
        raise MaterializationError("bundle_paths_not_exact")
    if expected_paths is not None and tuple(observed_paths) != expected_paths:
        raise MaterializationError("review_bundle_paths_not_exact")


def _ceremony_root_digest(ceremony_root: Path) -> str:
    domain = b"hey-kevin/voice-bakeoff/v9/ceremony-root/v1\x00"
    return hashlib.sha256(domain + os.fsencode(str(ceremony_root))).hexdigest()


def _validate_ceremony_root(
    ceremony_root: Path,
    *,
    repo_root: Path,
) -> Path:
    if not ceremony_root.is_absolute():
        raise MaterializationError("ceremony_root_not_absolute")
    try:
        before = os.lstat(ceremony_root)
        resolved = ceremony_root.resolve(strict=True)
    except OSError as exc:
        raise MaterializationError("ceremony_root_invalid") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o700
        or resolved != ceremony_root
    ):
        raise MaterializationError("ceremony_root_invalid")
    if resolved == repo_root or repo_root in resolved.parents:
        raise MaterializationError("ceremony_root_inside_repository")
    return resolved


def _validate_authorization(
    authorization: dict[str, Any],
    *,
    ceremony_root: Path,
) -> None:
    if set(authorization) != _AUTHORIZATION_KEYS:
        raise MaterializationError("authorization_keys_invalid")
    if (
        authorization["record_version"] != 1
        or authorization["record_kind"]
        != "voice_bakeoff_v9_materialization_authorization"
        or authorization["decision"]
        != "authorize_one_offline_unsigned_payload_materialization"
        or authorization["source_sha"] != _SOURCE_SHA
        or authorization["v9_normative_contract_digest"] != _V9_CONTRACT_DIGEST
        or authorization["predecessor_manifest_digest"]
        != _PREDECESSOR_MANIFEST_DIGEST
        or authorization["review_acceptances"] != _REVIEW_ACCEPTANCES
        or authorization["failure_consumes_attempt"] is not True
        or authorization["connected_action_authorized"] is not False
    ):
        raise MaterializationError("authorization_contract_invalid")
    _require_uuid4(authorization["authorization_id"], "authorization_id_invalid")
    _require_uuid4(authorization["attempt_id"], "attempt_id_invalid")
    for field in (
        "review_bundle_digest",
        "materialization_authority_identity_digest",
        "custodian_identity_digest",
        "ceremony_root_digest",
        "private_input_sha256",
        "authorization_digest",
    ):
        _require_sha256(authorization[field], f"{field}_invalid")

    review_bundle = authorization["review_bundle"]
    if not isinstance(review_bundle, list) or len(review_bundle) != 6:
        raise MaterializationError("review_bundle_invalid")
    if _digest(review_bundle) != authorization["review_bundle_digest"]:
        raise MaterializationError("review_bundle_digest_mismatch")
    observed_paths = []
    for entry in review_bundle:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise MaterializationError("review_bundle_invalid")
        observed_paths.append(_validate_relative_path(entry["path"]))
        _require_sha256(entry["sha256"], "review_bundle_invalid")
    if tuple(observed_paths) != _REVIEW_BUNDLE_PATHS:
        raise MaterializationError("review_bundle_paths_not_exact")

    issued = _require_safe_integer(
        authorization["issued_at_ms"],
        "authorization_issued_at_invalid",
    )
    validation = _require_safe_integer(
        authorization["validation_time_ms"],
        "authorization_validation_time_invalid",
    )
    expires = _require_safe_integer(
        authorization["expires_at_ms"],
        "authorization_expires_at_invalid",
    )
    if not issued <= validation < expires:
        raise MaterializationError("authorization_not_fresh")
    if expires - issued > _MAX_AUTHORIZATION_LIFETIME_MS:
        raise MaterializationError("authorization_lifetime_exceeded")
    if authorization["ceremony_root_digest"] != _ceremony_root_digest(
        ceremony_root
    ):
        raise MaterializationError("ceremony_root_digest_mismatch")

    unsigned = {
        key: value
        for key, value in authorization.items()
        if key != "authorization_digest"
    }
    if authorization["authorization_digest"] != _digest(unsigned):
        raise MaterializationError("authorization_digest_mismatch")


def _verify_snapshot(
    repo_root: Path,
    authorization: dict[str, Any],
    *,
    require_read_only: bool,
) -> None:
    _verify_static_contract_files(
        repo_root,
        require_read_only=require_read_only,
    )
    review_bundle = authorization["review_bundle"]
    _verify_manifest_entries(
        repo_root,
        review_bundle,
        expected_paths=_REVIEW_BUNDLE_PATHS,
        require_read_only=require_read_only,
    )
    predecessor_entries = _load_predecessor_manifest(
        repo_root,
        require_read_only=require_read_only,
    )
    _verify_manifest_entries(
        repo_root,
        predecessor_entries,
        expected_paths=None,
        require_read_only=require_read_only,
    )


def _read_authorization(ceremony_root: Path) -> dict[str, Any]:
    raw = _read_regular_file(
        ceremony_root / _AUTHORIZATION_NAME,
        required_mode=0o600,
        require_read_only=False,
    )
    return _load_json_bytes(raw, "authorization_json_invalid")


def _read_private_input(
    ceremony_root: Path,
    authorization: dict[str, Any],
) -> dict[str, Any]:
    raw = _read_regular_file(
        ceremony_root / _PRIVATE_INPUT_NAME,
        required_mode=0o600,
        require_read_only=False,
    )
    if _bytes_digest(raw) != authorization["private_input_sha256"]:
        raise MaterializationError("private_input_digest_mismatch")
    value = _load_json_bytes(raw, "private_input_json_invalid")
    if set(value) != _PRIVATE_INPUT_KEYS:
        raise MaterializationError("private_input_keys_invalid")
    _require_uuid4(
        value["owner_selected_inventory_session_uuidv4"],
        "inventory_session_invalid",
    )
    for field in (
        "owner_public_key_digest",
        "organization_operator_identity_configuration_digest",
        "isolated_bakeoff_operator_identity_configuration_digest",
        "raw_evidence_custody_digest",
        "one_use_nonce_seed",
    ):
        _require_sha256(value[field], f"{field}_invalid")
    issued = _require_safe_integer(value["issued_at_ms"], "issued_at_invalid")
    expires = _require_safe_integer(value["expires_at_ms"], "expires_at_invalid")
    validation = authorization["validation_time_ms"]
    if not issued <= validation < expires:
        raise MaterializationError("private_input_not_fresh")
    if expires - issued > _MAX_AUTHORIZATION_LIFETIME_MS:
        raise MaterializationError("private_input_lifetime_exceeded")
    if value["audit_and_quota_effects_acknowledged"] is not True:
        raise MaterializationError("audit_acknowledgement_missing")
    return value


def _build_request(
    *,
    index: int,
    project_id: str,
    configurations: dict[str, str],
) -> dict[str, Any]:
    identity = _QUERY_IDENTITIES[project_id]
    request = {
        "request_index": index,
        "request_id": f"req-{index:03d}",
        "method_ref": "project_identity_get",
        "api_method": "cloudresourcemanager.projects.get",
        "identity_ref": identity,
        "local_configuration_digest": configurations[identity],
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


def _build_payload(
    private_input: dict[str, Any],
    authorization: dict[str, Any],
) -> dict[str, Any]:
    configurations = {
        "organization_operator_identity": private_input[
            "organization_operator_identity_configuration_digest"
        ],
        "isolated_bakeoff_operator_identity": private_input[
            "isolated_bakeoff_operator_identity_configuration_digest"
        ],
    }
    requests = [
        _build_request(
            index=index,
            project_id=project_id,
            configurations=configurations,
        )
        for index, project_id in enumerate(_PROJECT_IDS)
    ]
    return {
        "payload_version": 9,
        "payload_kind": "unsigned_owner_signing_payload",
        "inventory_session_id": private_input[
            "owner_selected_inventory_session_uuidv4"
        ],
        "phase": "project_id_binding",
        "phase_ordinal": 1,
        "predecessor": "GENESIS",
        "source_sha": _SOURCE_SHA,
        "v8_json_digest": _V8_JSON_SHA,
        "v9_normative_contract_digest": _V9_CONTRACT_DIGEST,
        "predecessor_bundle_manifest_digest": _PREDECESSOR_MANIFEST_DIGEST,
        "v9_review_bundle_digest": authorization["review_bundle_digest"],
        "materialization_authorization_digest": authorization[
            "authorization_digest"
        ],
        "materialization_attempt_id": authorization["attempt_id"],
        "phase_one_method_contract_digest": _PHASE_ONE_METHOD_DIGEST,
        "owner_public_key_digest": private_input["owner_public_key_digest"],
        "identity_configuration_digests": configurations,
        "raw_evidence_custody_digest": private_input[
            "raw_evidence_custody_digest"
        ],
        "one_use_nonce_seed": private_input["one_use_nonce_seed"],
        "request_count": 4,
        "request_set_digest": _digest(requests),
        "requests": requests,
        "issued_at_ms": private_input["issued_at_ms"],
        "expires_at_ms": private_input["expires_at_ms"],
        "audit_and_quota_effects_acknowledged": True,
    }


def _validate_payload(
    payload: dict[str, Any],
    private_input: dict[str, Any],
    authorization: dict[str, Any],
) -> None:
    if set(payload) != _PAYLOAD_KEYS:
        raise MaterializationError("payload_keys_invalid")
    if (
        payload["payload_version"] != 9
        or payload["payload_kind"] != "unsigned_owner_signing_payload"
        or payload["phase"] != "project_id_binding"
        or payload["phase_ordinal"] != 1
        or payload["predecessor"] != "GENESIS"
        or payload["source_sha"] != _SOURCE_SHA
        or payload["v8_json_digest"] != _V8_JSON_SHA
        or payload["v9_normative_contract_digest"] != _V9_CONTRACT_DIGEST
        or payload["predecessor_bundle_manifest_digest"]
        != _PREDECESSOR_MANIFEST_DIGEST
        or payload["v9_review_bundle_digest"]
        != authorization["review_bundle_digest"]
        or payload["materialization_authorization_digest"]
        != authorization["authorization_digest"]
        or payload["materialization_attempt_id"] != authorization["attempt_id"]
        or payload["phase_one_method_contract_digest"]
        != _PHASE_ONE_METHOD_DIGEST
        or payload["request_count"] != 4
        or payload["audit_and_quota_effects_acknowledged"] is not True
    ):
        raise MaterializationError("payload_contract_invalid")
    _require_uuid4(payload["inventory_session_id"], "payload_session_invalid")
    if payload["inventory_session_id"] != private_input[
        "owner_selected_inventory_session_uuidv4"
    ]:
        raise MaterializationError("payload_session_mismatch")

    configurations = payload["identity_configuration_digests"]
    if not isinstance(configurations, dict) or set(configurations) != {
        "organization_operator_identity",
        "isolated_bakeoff_operator_identity",
    }:
        raise MaterializationError("payload_configurations_invalid")
    for value in configurations.values():
        _require_sha256(value, "payload_configuration_digest_invalid")
    for field in (
        "owner_public_key_digest",
        "raw_evidence_custody_digest",
        "one_use_nonce_seed",
        "request_set_digest",
    ):
        _require_sha256(payload[field], f"payload_{field}_invalid")

    requests = payload["requests"]
    if not isinstance(requests, list) or len(requests) != 4:
        raise MaterializationError("payload_requests_invalid")
    expected_requests = [
        _build_request(
            index=index,
            project_id=project_id,
            configurations=configurations,
        )
        for index, project_id in enumerate(_PROJECT_IDS)
    ]
    if requests != expected_requests or payload["request_set_digest"] != _digest(
        expected_requests
    ):
        raise MaterializationError("payload_relationship_invalid")
    if "owner_signature" in payload or "owner_authorization" in payload:
        raise MaterializationError("payload_authority_field_forbidden")


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(descriptor, value[offset:])
        if written <= 0:
            raise MaterializationError("file_write_failed")
        offset += written


def _atomic_create(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise MaterializationError("exclusive_path_exists") from exc
        raise MaterializationError("exclusive_create_failed") from exc
    try:
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise MaterializationError("created_file_mode_invalid")
        _write_all(descriptor, value)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MaterializationError("directory_sync_failed") from exc
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_no_replace(
    ceremony_root: Path,
    *,
    final_name: str,
    value: bytes,
    attempt_id: str,
) -> Path:
    final_path = ceremony_root / final_name
    temp_path = ceremony_root / f".{final_name}.{attempt_id}.tmp"
    _atomic_create(temp_path, value)
    try:
        try:
            os.link(temp_path, final_path, follow_symlinks=False)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise MaterializationError("exclusive_path_exists") from exc
            raise MaterializationError("atomic_publish_failed") from exc
        _fsync_directory(ceremony_root)
    except Exception:
        raise
    else:
        try:
            os.unlink(temp_path)
            _fsync_directory(ceremony_root)
        except OSError as exc:
            raise MaterializationError("published_temp_cleanup_failed") from exc
    return final_path


def _append_attempt_event(attempt_path: Path, event: dict[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(attempt_path, flags)
    except OSError as exc:
        raise MaterializationError("attempt_append_failed") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise MaterializationError("attempt_record_invalid")
        _write_all(descriptor, _canonical(event) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _attempt_event(
    authorization: dict[str, Any],
    *,
    event: str,
    state: str,
    payload_digest: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    return {
        "record_version": 1,
        "record_kind": "voice_bakeoff_v9_materialization_attempt_event",
        "event": event,
        "state": state,
        "attempt_id": authorization["attempt_id"],
        "authorization_id": authorization["authorization_id"],
        "source_sha": _SOURCE_SHA,
        "v9_normative_contract_digest": _V9_CONTRACT_DIGEST,
        "payload_digest": payload_digest,
        "error_code": error_code,
        "private_values_present": False,
        "connected_action_occurred": False,
    }


def _audit_record(
    authorization: dict[str, Any],
    *,
    status: str,
    payload_digest: str | None,
    error_code: str | None,
) -> dict[str, Any]:
    return {
        "record_version": 1,
        "record_kind": "voice_bakeoff_v9_payload_safe_materialization_audit",
        "status": status,
        "attempt_id": authorization["attempt_id"],
        "authorization_id": authorization["authorization_id"],
        "source_sha": _SOURCE_SHA,
        "v9_normative_contract_digest": _V9_CONTRACT_DIGEST,
        "payload_digest": payload_digest,
        "error_code": error_code,
        "private_values_present": False,
        "connected_action_occurred": False,
    }


def _fault(point: str) -> None:
    """Test seam. Production behavior is intentionally inert."""


def _has_payload_residue(ceremony_root: Path, attempt_id: str) -> bool:
    paths = (
        ceremony_root / _OUTPUT_NAME,
        ceremony_root / f".{_OUTPUT_NAME}.{attempt_id}.tmp",
    )
    return any(path.exists() or path.is_symlink() for path in paths)


def _best_effort_failure_record(
    ceremony_root: Path,
    attempt_path: Path,
    authorization: dict[str, Any],
    error: MaterializationError,
) -> None:
    residue = _has_payload_residue(ceremony_root, authorization["attempt_id"])
    state = "consumed_with_residue_stop" if residue else "consumed_no_payload"
    try:
        _append_attempt_event(
            attempt_path,
            _attempt_event(
                authorization,
                event="materialization_failed",
                state=state,
                error_code=error.code,
            ),
        )
    except Exception:
        pass
    audit_path = ceremony_root / _AUDIT_NAME
    if not audit_path.exists() and not audit_path.is_symlink():
        try:
            _publish_no_replace(
                ceremony_root,
                final_name=_AUDIT_NAME,
                value=_canonical(
                    _audit_record(
                        authorization,
                        status=state,
                        payload_digest=None,
                        error_code=error.code,
                    )
                ),
                attempt_id=authorization["attempt_id"],
            )
        except Exception:
            pass


def materialize_one_attempt(
    ceremony_root: Path,
    *,
    repo_root: Path = _REPO_ROOT,
) -> dict[str, str]:
    """Consume one attempt and publish one unsigned payload or fail terminally."""

    repo_root = repo_root.resolve(strict=True)
    ceremony_root = _validate_ceremony_root(
        ceremony_root,
        repo_root=repo_root,
    )
    _verify_static_contract_files(
        repo_root,
        require_read_only=True,
    )
    authorization = _read_authorization(ceremony_root)
    _validate_authorization(
        authorization,
        ceremony_root=ceremony_root,
    )
    _verify_snapshot(
        repo_root,
        authorization,
        require_read_only=True,
    )

    attempt_path = ceremony_root / _ATTEMPT_NAME
    initial_event = _attempt_event(
        authorization,
        event="attempt_consumed",
        state="consumed_no_payload",
    )
    try:
        _atomic_create(attempt_path, _canonical(initial_event) + b"\n")
    except MaterializationError as exc:
        if exc.code == "exclusive_path_exists":
            raise MaterializationError("attempt_already_consumed") from exc
        raise

    try:
        _fault("after_attempt_consumed")
        _verify_snapshot(
            repo_root,
            authorization,
            require_read_only=True,
        )
        _fault("before_private_input")
        private_input = _read_private_input(ceremony_root, authorization)
        _fault("after_private_input")
        payload = _build_payload(private_input, authorization)
        _validate_payload(payload, private_input, authorization)
        payload_bytes = _canonical(payload)
        payload_digest = _bytes_digest(payload_bytes)
        _fault("before_payload_publish")
        _publish_no_replace(
            ceremony_root,
            final_name=_OUTPUT_NAME,
            value=payload_bytes,
            attempt_id=authorization["attempt_id"],
        )
        _fault("after_payload_publish")
        audit = _audit_record(
            authorization,
            status="generated_one_payload",
            payload_digest=payload_digest,
            error_code=None,
        )
        _publish_no_replace(
            ceremony_root,
            final_name=_AUDIT_NAME,
            value=_canonical(audit),
            attempt_id=authorization["attempt_id"],
        )
        _fault("after_audit_publish")
        _append_attempt_event(
            attempt_path,
            _attempt_event(
                authorization,
                event="payload_committed",
                state="generated_one_payload",
                payload_digest=payload_digest,
            ),
        )
        _fault("after_success_event")
        return {
            "status": "generated_one_payload",
            "payload_digest": payload_digest,
        }
    except MaterializationError as exc:
        _best_effort_failure_record(
            ceremony_root,
            attempt_path,
            authorization,
            exc,
        )
        raise
    except Exception as exc:
        error = MaterializationError("internal_error")
        _best_effort_failure_record(
            ceremony_root,
            attempt_path,
            authorization,
            error,
        )
        raise error from exc


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        return 64
    try:
        materialize_one_attempt(Path(arguments[0]))
    except MaterializationError:
        return 65
    except Exception:
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
