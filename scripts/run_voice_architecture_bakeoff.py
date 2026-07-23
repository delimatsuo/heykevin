#!/usr/bin/env python3
"""Fail-closed, dry-run-only preflight for a future voice bakeoff approval."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path


_MAX_FILE_BYTES = 131_072
_SCHEMA_PATH = Path("tests/fixtures/voice_architecture_bakeoff/provider_approval.schema.json")
_REQUIRED_ROLES = {"staff", "security_privacy", "conversation_product"}
_RISKY_FEATURES = {
    "tools",
    "writes",
    "notifications",
    "terminal_actions",
    "transfers",
    "recording",
    "tracing",
    "data_sharing",
    "request_response_logging",
    "session_resumption",
    "provider_cache",
}
_ARTIFACT_DIGESTS = {"corpus", "setup", "configuration", "evaluator", "security_annex", "caller_ux"}
_DEPENDENCY_FIELDS = {"role", "provider", "version", "endpoint_ref", "destination_allowlist_ref", "credential_ref", "account_region_ref", "nonproduction_identity_ref", "privacy_posture_ref"}
_CAPS = {
    "requests",
    "attempts",
    "concurrency",
    "duration_ms",
    "bytes",
    "audio_ms",
    "retries",
    "tokens",
    "cost_minor_units",
}
_ARM_ROLES = {
    "A": {"telephony", "native_voice"},
    "B1": {"telephony", "speech_to_text", "text_generation", "text_to_speech"},
    "B2": {"telephony", "conversation_relay", "text_generation"},
    "C": {"telephony", "native_voice"},
}


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load(path: Path) -> dict[str, object]:
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("input exceeds size limit")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_]{1,128}", value))


def _canonical_digest(value: dict[str, object]) -> str:
    def signature_metadata(item: object) -> object:
        if not isinstance(item, dict):
            return {"invalid_signature_entry": True}
        return {name: item[name] for name in sorted(item) if name != "signature"}

    material = {
        key: (
            [signature_metadata(item) for item in item_value]
            if key == "signatures" and isinstance(item_value, list)
            else item_value
        )
        for key, item_value in value.items()
        if key != "self_digest"
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _dependency_inventory_digest(dependencies: object) -> str:
    encoded = json.dumps(dependencies, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate(approval: dict[str, object], manifest: dict[str, object], arm: str, source_sha: str, now_ms: int | None = None, schema: dict[str, object] | None = None, manifest_digest: str | None = None) -> list[str]:
    required = {"approval_id", "nonce", "issued_at_ms", "expires_at_ms", "self_digest", "environment", "arm", "source_sha", "manifest_digest", "dependency_inventory_digest", "artifact_digests", "dependencies", "caps", "disabled_features", "custody_references", "trust_metadata", "signatures"}
    errors = []
    if manifest_digest is None:
        manifest_digest = _manifest_digest_bytes(json.dumps(manifest, separators=(",", ":")).encode("utf-8"))
    if schema is not None:
        if (
            schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
            or set(schema.get("required", [])) != required
            or set(schema.get("properties", [])) != required
            or schema.get("additionalProperties") is not False
            or set(schema.get("x-required-roles", [])) != _REQUIRED_ROLES
            or schema.get("x-execution") != "unsupported"
        ):
            errors.append("approval schema contract mismatch")
    candidate = manifest.get("candidate")
    if set(approval) != required:
        errors.append("approval schema mismatch")
    if manifest.get("authorization_status") == "template_only":
        errors.append("template manifest is not executable")
    if approval.get("environment") != "bakeoff" or manifest.get("environment") != "bakeoff" or manifest.get("authorization_status") != "sealed" or not isinstance(candidate, dict) or candidate.get("arm") != arm or candidate.get("source_sha") != source_sha:
        errors.append("manifest status or arm mismatch")
    if approval.get("arm") != arm or approval.get("source_sha") != source_sha:
        errors.append("requested binding mismatch")
    if not _identifier(approval.get("approval_id")) or not _identifier(approval.get("nonce")) or not isinstance(source_sha, str) or len(source_sha) != 40:
        errors.append("approval identifier or source SHA invalid")
    if not isinstance(candidate, dict) or approval.get("manifest_digest") != manifest_digest:
        errors.append("manifest binding mismatch")
    if (
        not isinstance(candidate, dict)
        or not _digest(approval.get("dependency_inventory_digest"))
        or approval.get("dependency_inventory_digest") != candidate.get("dependency_inventory_digest")
        or approval.get("dependency_inventory_digest") != _dependency_inventory_digest(approval.get("dependencies"))
    ):
        errors.append("dependency inventory binding mismatch")
    if not _digest(approval.get("self_digest")) or approval.get("self_digest") != _canonical_digest(approval):
        errors.append("approval self digest mismatch")
    signatures = approval.get("signatures")
    if not isinstance(signatures, list) or len(signatures) != 3 or {item.get("role") for item in signatures if isinstance(item, dict)} != _REQUIRED_ROLES:
        errors.append("required approval roles missing")
    elif len({item.get("identity") for item in signatures if isinstance(item, dict)}) != len(signatures):
        errors.append("approval identities must be distinct")
    caps = approval.get("caps")
    if not isinstance(caps, dict) or set(caps) != _CAPS or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in caps.values()):
        errors.append("caps must be positive integers")
    disabled = approval.get("disabled_features")
    if not isinstance(disabled, dict) or set(disabled) != _RISKY_FEATURES or any(value is not True for value in disabled.values()):
        errors.append("risky features must be disabled")
    if not isinstance(approval.get("dependencies"), list) or not approval["dependencies"]:
        errors.append("dependencies are required")
    elif any(not isinstance(item, dict) or set(item) != _DEPENDENCY_FIELDS or any(not _identifier(item.get(key)) for key in _DEPENDENCY_FIELDS) for item in approval["dependencies"]) or {item["role"] for item in approval["dependencies"] if isinstance(item, dict)} != _ARM_ROLES.get(arm, set()) or len({item["role"] for item in approval["dependencies"] if isinstance(item, dict)}) != len(approval["dependencies"]):
        errors.append("dependency record invalid")
    for field in ("artifact_digests", "custody_references", "trust_metadata"):
        value = approval.get(field)
        if not isinstance(value, dict) or not value or any(not _identifier(key) or not isinstance(item, str) or not item for key, item in value.items()):
            errors.append(f"{field} invalid")
    artifacts = approval.get("artifact_digests")
    if not isinstance(artifacts, dict) or set(artifacts) != _ARTIFACT_DIGESTS or any(not _digest(value) for value in artifacts.values()):
        errors.append("artifact digest set invalid")
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if isinstance(approval.get("issued_at_ms"), bool) or not isinstance(approval.get("issued_at_ms"), int) or isinstance(approval.get("expires_at_ms"), bool) or not isinstance(approval.get("expires_at_ms"), int) or approval["issued_at_ms"] >= approval["expires_at_ms"] or approval["expires_at_ms"] <= current_ms:
        errors.append("approval is expired or timestamps invalid")
    if any(not isinstance(item, dict) or set(item) != {"role", "identity", "key_id", "algorithm", "signature"} or not all(isinstance(item.get(key), str) and item[key] for key in ("identity", "key_id", "algorithm", "signature")) for item in signatures if isinstance(signatures, list)):
        errors.append("unsigned signature record")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", required=True)
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parents[1]
        source_sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        manifest_bytes = args.manifest.read_bytes()
        errors = validate(_load(args.approval), _load(args.manifest), args.arm, source_sha, schema=_load(root / _SCHEMA_PATH), manifest_digest=_manifest_digest_bytes(manifest_bytes))
    except (OSError, ValueError, json.JSONDecodeError):
        errors = ["invalid local input"]
    verdict = "blocked_external_verification_required" if not errors else "rejected_local_preflight"
    print(json.dumps({"verdict": verdict, "error_count": len(errors)}, sort_keys=True))
    return 3 if verdict == "blocked_external_verification_required" else 2


if __name__ == "__main__":
    raise SystemExit(main())
