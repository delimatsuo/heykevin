#!/usr/bin/env python3
"""Fail-closed, dry-run-only preflight for a future voice bakeoff approval."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services.voice_bakeoff_credential_broker import NonproductionCredentialBroker
from app.services.voice_bakeoff_nonce_ledger import FileBackedNonceLedger
from app.services.voice_bakeoff_residue_audit import audit_residue
from app.services.voice_bakeoff_security_contracts import (
    ApprovalArm,
    ApprovalCaps,
    ApprovalProvenanceSigner,
    DetachedApprovalSignature,
    InMemoryTrustGenerationPinStore,
    OfflineApprovalVerifier,
    SignedApproval,
    SignerRole,
    TechnicalReviewReceipt,
    TrustedSignerKey,
    TrustGenerationPin,
    TrustSnapshot,
    TrustSnapshotRootSigner,
    TrustSnapshotRootVerifier,
    approval_signature_payload,
)

try:
    from scripts.voice_bakeoff_caller import run_offline_self_check
except ModuleNotFoundError:
    from voice_bakeoff_caller import run_offline_self_check

try:
    from scripts.request_voice_bakeoff_review import reviewer_is_procedurally_separate
except ModuleNotFoundError:
    from request_voice_bakeoff_review import reviewer_is_procedurally_separate


_MAX_FILE_BYTES = 131_072
_SCHEMA_PATH = Path("tests/fixtures/voice_architecture_bakeoff/provider_approval.schema.json")
_AUTHORIZATION_MODEL = "sole_owner"
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
_OFFLINE_ADAPTERS = {
    "A": "app.services.voice_candidates.native_gemini:NativeGeminiAdapter",
    "B1": "app.services.voice_candidates.chained_streaming:ChainedStreamingAdapter",
    "B2": "app.services.voice_candidates.conversation_relay:ConversationRelayAdapter",
    "C": "app.services.voice_candidates.manual_native:ManualNativeAdapter",
}
_OFFLINE_HARNESS = "scripts/voice_bakeoff_caller.py:run_offline_self_check"

# --- Task 6: real cryptographic/replay/broker verification -----------------
#
# There is no persisted, externally-provisioned trust store yet (no task in
# this plan builds one — see docs/security/voice-architecture-bakeoff-controls.md,
# "Gates that keep Task 3.4 and Task 4.8 blocked": "cryptographic signer
# keys, current trust store... do not exist in this slice"). Rather than
# skip real Ed25519 verification until that lands, or fabricate an
# unconditionally-self-signed snapshot that would make verification a
# no-op, the runner accepts the sole owner's Ed25519 *public* key (not a
# secret) as an operator-supplied --trust-owner-public-key hex string. When
# absent or malformed, verification fails closed. When present, the runner
# builds a minimal, self-consistent TrustSnapshot around that one public
# key and a fresh, single-use, ephemeral "root" keypair — the root keypair
# carries no external trust meaning of its own; it exists only to satisfy
# OfflineApprovalVerifier's constructor shape, which requires a
# self-authenticating snapshot. The only externally-meaningful comparison
# is: does the approval's signature verify against the operator-configured
# owner public key. This is a deliberate, documented scope boundary — see
# the report for this task.
_TRUST_POLICY_DIGEST = hashlib.sha256(
    b"bakeoff-runner-ephemeral-trust-policy/v1"
).hexdigest()
_TRUST_CAS_DIGEST = hashlib.sha256(
    b"bakeoff-runner-ephemeral-trust-cas/v1"
).hexdigest()
_ZERO_DIGEST = "0" * 64
# The approval schema's caps dict (_CAPS, 9 fields) does not yet track
# ApprovalCaps' "calls" or "artifact_ttl_ms" fields separately. "calls" is
# conservatively defaulted to the already-approved "requests" bound;
# artifact_ttl_ms uses this fixed default (also reused for the residue
# audit below) until the schema grows a dedicated field.
_DEFAULT_ARTIFACT_TTL_MS = 86_400_000


def _string_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_trust_material(
    approval: dict[str, object],
    owner_public_key: bytes,
) -> tuple[TrustSnapshot, OfflineApprovalVerifier]:
    """Build a minimal, self-consistent trust snapshot bound to
    `owner_public_key`, plus a verifier that can check a SignedApproval
    against it. See the module-level comment above for what is and is not
    externally trustworthy about this."""
    owner_authorization = approval["owner_authorization"]
    expires_at_ms = approval["expires_at_ms"] + 1
    signer = TrustedSignerKey(
        role=SignerRole.OWNER,
        identity_ref=owner_authorization["identity"],
        key_id=owner_authorization["key_id"],
        public_key=owner_public_key,
        effective_at_ms=0,
        expires_at_ms=expires_at_ms,
        revoked_at_ms=None,
    )
    ephemeral_key = Ed25519PrivateKey.generate()
    unsigned_snapshot = TrustSnapshot(
        generation=1,
        version_ref="ref_bakeoff_runner_trust_generation_1",
        policy_digest=_TRUST_POLICY_DIGEST,
        immutable_custody_ref="ref_bakeoff_runner_trust_custody",
        effective_at_ms=0,
        expires_at_ms=expires_at_ms,
        signers=(signer,),
        sole_owner_authorization=True,
        no_break_glass=True,
        root_signature=b"\x00" * 64,
    )
    snapshot = TrustSnapshotRootSigner(ephemeral_key).issue(unsigned_snapshot)
    root_verifier = TrustSnapshotRootVerifier(
        ephemeral_key.public_key().public_bytes_raw()
    )
    pin_store = InMemoryTrustGenerationPinStore(
        TrustGenerationPin(
            generation=1,
            snapshot_digest=snapshot.snapshot_digest,
            root_key_fingerprint=root_verifier.key_fingerprint,
            persistence_ref="ref_bakeoff_runner_trust_pin",
            cas_version_digest=_TRUST_CAS_DIGEST,
        )
    )
    verifier = OfflineApprovalVerifier(
        provenance_signer=ApprovalProvenanceSigner(ephemeral_key),
        snapshot_root_verifier=root_verifier,
        generation_pin_store=pin_store,
    )
    return snapshot, verifier


def _resolve_trust_owner_public_key(trust_owner_public_key_hex: str | None) -> bytes | None:
    if not isinstance(trust_owner_public_key_hex, str):
        return None
    try:
        candidate = bytes.fromhex(trust_owner_public_key_hex)
    except ValueError:
        return None
    return candidate if len(candidate) == 32 else None


def _build_signed_approval(
    approval: dict[str, object],
    trust_snapshot_digest: str,
    signer_set_digest: str,
) -> SignedApproval:
    """Construct the real SignedApproval envelope from the runner's
    already-shape-validated approval dict. May raise ValueError/TypeError
    if a field does not additionally satisfy the real dataclasses'
    stricter invariants (e.g. opaque refs need a "ref_" prefix) — callers
    must treat that as a rejection, not a bug; see Task 5's cross-task
    note on this exact mismatch."""
    owner_authorization = approval["owner_authorization"]
    technical_review = approval["technical_review"]
    caps = approval["caps"]
    payload_digest = approval["self_digest"]
    binding_digest = approval["manifest_digest"]
    return SignedApproval(
        payload_digest=payload_digest,
        approval_id_digest=_string_digest(approval["approval_id"]),
        nonce_digest=_string_digest(approval["nonce"]),
        binding_digest=binding_digest,
        trust_snapshot_digest=trust_snapshot_digest,
        signer_set_digest=signer_set_digest,
        environment=approval["environment"],
        arm=ApprovalArm(approval["arm"]),
        epoch=1,
        issued_at_ms=approval["issued_at_ms"],
        expires_at_ms=approval["expires_at_ms"],
        caps=ApprovalCaps(
            requests=caps["requests"],
            attempts=caps["attempts"],
            calls=caps["requests"],
            concurrency=caps["concurrency"],
            duration_ms=caps["duration_ms"],
            bytes=caps["bytes"],
            audio_ms=caps["audio_ms"],
            retries=caps["retries"],
            tokens=caps["tokens"],
            cost_minor_units=caps["cost_minor_units"],
            artifact_ttl_ms=_DEFAULT_ARTIFACT_TTL_MS,
        ),
        technical_review=TechnicalReviewReceipt(
            review_digest=technical_review["review_digest"],
            provenance_ref=technical_review["provenance_ref"],
            reviewed_payload_digest=payload_digest,
            reviewed_binding_digest=binding_digest,
            unresolved_p1_count=technical_review["unresolved_p1_count"],
            advisory_only=technical_review["advisory_only"],
        ),
        signatures=(
            DetachedApprovalSignature(
                role=SignerRole(owner_authorization["role"]),
                identity_ref=owner_authorization["identity"],
                key_id=owner_authorization["key_id"],
                signature=bytes.fromhex(owner_authorization["signature"]),
            ),
        ),
    )


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
    def owner_authorization_metadata(item: object) -> object:
        if not isinstance(item, dict):
            return {"invalid_owner_authorization": True}
        return {
            name: item[name]
            for name in sorted(item)
            if name != "signature"
        }

    material = {
        key: (
            owner_authorization_metadata(item_value)
            if key == "owner_authorization"
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


def _validate_and_build_signed_approval(
    approval: dict[str, object],
    manifest: dict[str, object],
    arm: str,
    source_sha: str,
    current_ms: int,
    schema: dict[str, object] | None,
    manifest_digest: str | None,
    trust_owner_public_key_hex: str | None,
) -> tuple[list[str], SignedApproval | None, tuple[TrustSnapshot, OfflineApprovalVerifier] | None]:
    """Run every `validate()` check that does not require an already-verified
    signature — shape/digest/binding shape, reviewer/signer separation, and
    (pre-verification) `SignedApproval` construction against an ephemeral
    trust snapshot bound to `trust_owner_public_key_hex` — and return
    `(errors, signed_approval, trust_material)`.

    `signed_approval`/`trust_material` are `None` whenever `errors` is
    non-empty. `signed_approval` can still come back non-`None` with
    `trust_material` still `None` (an absent/malformed
    `trust_owner_public_key_hex` never itself appends to `errors` here — see
    `_build_trust_material`'s call site below); callers that need a
    genuinely trust-anchored `signed_approval` (not one built against the
    `_ZERO_DIGEST` fallback) must check `trust_material is not None`
    themselves, exactly as `validate()`'s own `verifier.verify()` step below
    does.

    Shared by `validate()` (which continues on to call
    `OfflineApprovalVerifier.verify()` and the credential broker) and
    `main()`'s `--emit-signing-payload` mode (which stops here, before real
    signature verification — there is no real signature yet — to hand an
    operator the exact bytes a real owner signature must cover).
    """
    required = {"approval_id", "nonce", "issued_at_ms", "expires_at_ms", "self_digest", "environment", "arm", "source_sha", "manifest_digest", "dependency_inventory_digest", "artifact_digests", "dependencies", "caps", "disabled_features", "custody_references", "trust_metadata", "authorization_model", "owner_authorization", "technical_review"}
    errors = []
    if arm not in _OFFLINE_ADAPTERS:
        errors.append("candidate adapter is not registered")
    if manifest_digest is None:
        manifest_digest = _manifest_digest_bytes(json.dumps(manifest, separators=(",", ":")).encode("utf-8"))
    if schema is not None:
        if (
            schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
            or set(schema.get("required", [])) != required
            or set(schema.get("properties", [])) != required
            or schema.get("additionalProperties") is not False
            or schema.get("x-authorization-model") != _AUTHORIZATION_MODEL
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
    if approval.get("authorization_model") != _AUTHORIZATION_MODEL:
        errors.append("authorization model invalid")
    owner_authorization = approval.get("owner_authorization")
    if (
        not isinstance(owner_authorization, dict)
        or set(owner_authorization)
        != {"role", "identity", "key_id", "algorithm", "signature"}
        or owner_authorization.get("role") != "owner"
        or any(
            not isinstance(owner_authorization.get(key), str)
            or not owner_authorization[key]
            for key in ("identity", "key_id", "signature")
        )
        or owner_authorization.get("algorithm") != "ed25519"
    ):
        errors.append("owner authorization invalid")
    technical_review = approval.get("technical_review")
    if (
        not isinstance(technical_review, dict)
        or set(technical_review)
        != {
            "review_digest",
            "provenance_ref",
            "source_sha",
            "manifest_digest",
            "unresolved_p1_count",
            "advisory_only",
        }
        or not _digest(technical_review.get("review_digest"))
        or not _identifier(technical_review.get("provenance_ref"))
        or technical_review.get("source_sha") != approval.get("source_sha")
        or technical_review.get("manifest_digest") != approval.get("manifest_digest")
        or type(technical_review.get("unresolved_p1_count")) is not int
        or technical_review["unresolved_p1_count"] != 0
        or technical_review.get("advisory_only") is not True
    ):
        errors.append("technical review receipt invalid")
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
    if isinstance(approval.get("issued_at_ms"), bool) or not isinstance(approval.get("issued_at_ms"), int) or isinstance(approval.get("expires_at_ms"), bool) or not isinstance(approval.get("expires_at_ms"), int) or approval["issued_at_ms"] >= approval["expires_at_ms"] or approval["expires_at_ms"] <= current_ms:
        errors.append("approval is expired or timestamps invalid")

    # --- Task 6: cryptographic/replay/broker verification, earliest-boundary-first ---
    # Each step below is gated on "if not errors" so it only runs once every
    # shape check above it has already passed — this both matches Task 3.4's
    # earliest-boundary-first spec text and prevents an unhandled exception
    # from indexing into a field an earlier check already found malformed.
    if not errors:
        if not reviewer_is_procedurally_separate(
            signer_provenance_ref=owner_authorization["identity"],
            reviewer_provenance_ref=technical_review["provenance_ref"],
        ):
            errors.append("reviewer is not procedurally separate from signer")

    trust_material = None
    if not errors:
        owner_public_key = _resolve_trust_owner_public_key(trust_owner_public_key_hex)
        if owner_public_key is not None:
            try:
                trust_material = _build_trust_material(approval, owner_public_key)
            except (ValueError, TypeError):
                trust_material = None

    trust_snapshot_digest = trust_material[0].snapshot_digest if trust_material else _ZERO_DIGEST
    signer_set_digest = trust_material[0].signer_set_digest if trust_material else _ZERO_DIGEST

    signed_approval = None
    if not errors:
        try:
            signed_approval = _build_signed_approval(approval, trust_snapshot_digest, signer_set_digest)
        except (ValueError, TypeError) as exc:
            errors.append(f"signed approval construction failed: {exc}")

    return errors, signed_approval, trust_material


def validate(approval: dict[str, object], manifest: dict[str, object], arm: str, source_sha: str, now_ms: int | None = None, schema: dict[str, object] | None = None, manifest_digest: str | None = None, trust_owner_public_key_hex: str | None = None) -> list[str]:
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    errors, signed_approval, trust_material = _validate_and_build_signed_approval(
        approval,
        manifest,
        arm,
        source_sha,
        current_ms,
        schema,
        manifest_digest,
        trust_owner_public_key_hex,
    )

    if not errors:
        verified = None
        if signed_approval is not None and trust_material is not None:
            snapshot, verifier = trust_material
            verified = verifier.verify(signed_approval, snapshot, now_ms=current_ms)
        if verified is None:
            errors.append("signature or trust verification failed")

    if not errors:
        broker = NonproductionCredentialBroker(env=os.environ)
        for dependency in approval["dependencies"]:
            grant = broker.resolve(
                dependency_role=dependency["role"],
                approved_credential_ref=dependency["credential_ref"],
                approved_account_region_ref=dependency["account_region_ref"],
            )
            if grant is None:
                errors.append(f"credential broker denied dependency: {dependency['role']}")

    return errors


def _load_inputs(
    args: argparse.Namespace,
) -> tuple[str, dict[str, object], dict[str, object], dict[str, object], str]:
    """Load and return `(source_sha, manifest, approval, schema,
    manifest_digest)` — the local inputs both `main()`'s normal
    validate-and-admit path and its `--emit-signing-payload` mode need
    before their behavior diverges. Propagates whatever `_load()`/
    `read_bytes()`/`subprocess` itself raises; callers are responsible for
    catching `(OSError, ValueError, json.JSONDecodeError)`, exactly as
    `main()` already did for this same loading sequence before it moved
    here."""
    root = Path(__file__).resolve().parents[1]
    source_sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    manifest_bytes = args.manifest.read_bytes()
    manifest = _load(args.manifest)
    approval = _load(args.approval)
    schema = _load(root / _SCHEMA_PATH)
    manifest_digest = _manifest_digest_bytes(manifest_bytes)
    return source_sha, manifest, approval, schema, manifest_digest


def _run_emit_signing_payload(args: argparse.Namespace) -> int:
    """`--emit-signing-payload` mode: compute and write the exact JSON
    payload dict a real owner signature must cover for this
    approval/manifest/arm/--trust-owner-public-key combination — everything
    `validate()` checks up to, but not including, verifying a real
    signature. Never admits a nonce and never runs the residue audit:
    neither applies yet — there is no real signature to admit a nonce for,
    and nothing here executes.

    Feed the written file to
    `scripts/sign_voice_bakeoff_approval.py --payload <path> --domain-name
    approval` to produce a signature `OfflineApprovalVerifier.verify()` will
    accept once it is embedded into the approval JSON's
    `owner_authorization.signature`. Until then, that field only needs to be
    present and syntactically valid 128-hex-character text to satisfy this
    mode's own shape/construction checks (e.g. 128 zeros) — its value is
    excluded from `self_digest` (see `_canonical_digest`) and ignored by
    `approval_signature_payload`, so a placeholder there does not change the
    bytes this mode emits.
    """
    try:
        source_sha, manifest, approval, schema, manifest_digest = _load_inputs(args)
        current_ms = int(time.time() * 1000)
        errors, signed_approval, trust_material = _validate_and_build_signed_approval(
            approval,
            manifest,
            args.arm,
            source_sha,
            current_ms,
            schema,
            manifest_digest,
            args.trust_owner_public_key,
        )
        if not errors and trust_material is None:
            errors = ["cannot emit signing payload without a valid --trust-owner-public-key"]
        if not errors and signed_approval is not None:
            payload_text = json.dumps(approval_signature_payload(signed_approval), indent=2, sort_keys=True)
            args.emit_signing_payload.write_text(payload_text, encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError):
        errors = ["invalid local input"]
    verdict = "signing_payload_emitted" if not errors else "rejected_local_preflight"
    print(json.dumps({"verdict": verdict, "error_count": len(errors)}, sort_keys=True))
    return 0 if not errors else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", required=True)
    parser.add_argument("--nonce-ledger", type=Path, required=True)
    parser.add_argument("--residue-destination", type=Path, required=True)
    parser.add_argument("--trust-owner-public-key", default=None)
    parser.add_argument(
        "--emit-signing-payload",
        type=Path,
        default=None,
        help=(
            "Write the canonical JSON payload a real owner signature must "
            "cover for --approval/--manifest/--arm/--trust-owner-public-key "
            "to this path, then exit — do not validate a signature, admit a "
            "nonce, or audit residue. --dry-run/--nonce-ledger/"
            "--residue-destination are still required by argparse but are "
            "not read in this mode."
        ),
    )
    args = parser.parse_args()
    if args.emit_signing_payload is not None:
        return _run_emit_signing_payload(args)
    try:
        source_sha, manifest, approval, schema, manifest_digest = _load_inputs(args)
        errors = validate(
            approval,
            manifest,
            args.arm,
            source_sha,
            schema=schema,
            manifest_digest=manifest_digest,
            trust_owner_public_key_hex=args.trust_owner_public_key,
        )
        if not errors and not run_offline_self_check(arm=args.arm, manifest=manifest):
            errors.append("offline caller harness self-check failed")
        if not errors and not FileBackedNonceLedger(args.nonce_ledger).admit(
            nonce_digest=approval["nonce"],
            approval_id_digest=approval["approval_id"],
            binding_digest=approval["manifest_digest"],
            epoch=1,
        ):
            errors.append("nonce already consumed")
    except (OSError, ValueError, json.JSONDecodeError):
        errors = ["invalid local input"]
    verdict = "blocked_external_verification_required" if not errors else "rejected_local_preflight"
    current_ms = int(time.time() * 1000)
    residue_result = audit_residue(
        args.residue_destination,
        artifact_ttl_ms=_DEFAULT_ARTIFACT_TTL_MS,
        now_ms=current_ms,
    )
    print(
        json.dumps(
            {
                "verdict": verdict,
                "error_count": len(errors),
                "residue_audit": {
                    "passed": residue_result.passed,
                    "remaining_paths": list(residue_result.remaining_paths),
                },
            },
            sort_keys=True,
        )
    )
    return 3 if verdict == "blocked_external_verification_required" else 2


if __name__ == "__main__":
    raise SystemExit(main())
