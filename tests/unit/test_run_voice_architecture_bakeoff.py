"""Tests for the fail-closed, dry-run-only approval preflight."""

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from app.services.voice_bakeoff_credential_broker import NonproductionCredentialBroker
from app.services.voice_bakeoff_nonce_ledger import FileBackedNonceLedger
from app.services.voice_bakeoff_security_contracts import (
    _APPROVAL_DOMAIN,
    approval_signature_message,
)
from scripts.request_voice_bakeoff_review import (
    build_receipt_request,
    parse_review_response,
    reviewer_is_procedurally_separate,
)
from scripts.sign_voice_bakeoff_approval import sign_payload
from scripts.voice_bakeoff_caller import development_harness_manifest

_SCRIPT = Path("scripts/run_voice_architecture_bakeoff.py")
_SPEC = importlib.util.spec_from_file_location("bakeoff_runner", _SCRIPT)
assert _SPEC and _SPEC.loader
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)

# When the runner is launched as a subprocess via a direct file path (as the
# CLI tests below do), Python sets sys.path[0] to the script's own directory
# (scripts/), not the repository root — so its `from app.services... import`
# statements need the repo root on PYTHONPATH to resolve. In-process imports
# (like the ones above) don't need this: pytest already puts the repo root
# on sys.path for this process.
_REPO_ROOT = _SCRIPT.resolve().parents[1]


def _approval() -> dict[str, object]:
    value: dict[str, object] = {
        "approval_id": "approval_1", "nonce": "nonce_1", "issued_at_ms": 1,
        "expires_at_ms": 2_000, "self_digest": "0" * 64, "environment": "bakeoff", "arm": "B1",
        "source_sha": "a" * 40, "manifest_digest": "0" * 64, "dependency_inventory_digest": "0" * 64,
        "artifact_digests": {key: "c" * 64 for key in runner._ARTIFACT_DIGESTS},
        "dependencies": [
            {
                key: role if key == "role" else f"{key}_{role}"
                for key in runner._DEPENDENCY_FIELDS
            }
            for role in runner._ARM_ROLES["B1"]
        ],
        "caps": {key: 1 for key in runner._CAPS},
        "disabled_features": {key: True for key in runner._RISKY_FEATURES},
        "custody_references": {"immutable": "reference"},
        "trust_metadata": {"trust_store": "reference"},
        "authorization_model": "sole_owner",
        "owner_authorization": {
            "role": "owner",
            "identity": "ref_owner_1",
            "key_id": "ref_key_1",
            "algorithm": "ed25519",
            "signature": "detached_1",
        },
        "technical_review": {
            "review_digest": "d" * 64,
            "provenance_ref": "ref_technical_review_1",
            "source_sha": "a" * 40,
            "manifest_digest": "0" * 64,
            "unresolved_p1_count": 0,
            "advisory_only": True,
        },
    }
    value["self_digest"] = runner._canonical_digest(value)
    return value


def _manifest(template_only: bool = False) -> dict[str, object]:
    seed = "9" * 64
    manifest = {
        "authorization_status": "template_only" if template_only else "sealed",
        "environment": "bakeoff",
        "candidate": {"arm": "B1", "source_sha": "a" * 40, "dependency_inventory_digest": "0" * 64},
        "caller_harness": development_harness_manifest(
            sealed_seed_digest=seed,
        ),
    }
    return manifest


def _resign(approval: dict[str, object]) -> None:
    approval["self_digest"] = runner._canonical_digest(approval)


def _bound_approval(manifest: dict[str, object]) -> dict[str, object]:
    approval = _approval()
    approval["dependency_inventory_digest"] = runner._dependency_inventory_digest(approval["dependencies"])
    manifest["candidate"]["dependency_inventory_digest"] = approval["dependency_inventory_digest"]
    approval["manifest_digest"] = runner._manifest_digest_bytes(
        __import__("json").dumps(manifest, separators=(",", ":")).encode("utf-8")
    )
    approval["technical_review"]["manifest_digest"] = approval["manifest_digest"]
    _resign(approval)
    return approval


def _rebind_manifest(approval: dict[str, object], manifest: dict[str, object]) -> None:
    approval["manifest_digest"] = runner._manifest_digest_bytes(
        __import__("json").dumps(manifest, separators=(",", ":")).encode("utf-8")
    )
    approval["technical_review"]["manifest_digest"] = approval["manifest_digest"]
    _resign(approval)


def _sign_with_key(
    approval: dict[str, object],
    private_key: ed25519.Ed25519PrivateKey,
    trust_owner_public_key_hex: str,
) -> None:
    """(Re-)sign `approval` with `private_key`.

    The signed message is computed against the ephemeral trust snapshot
    `runner._build_trust_material` derives for `trust_owner_public_key_hex`
    — which need not correspond to `private_key`. Tests use a deliberate
    mismatch between the two to prove a wrong-key signature is rejected.
    """
    approval["owner_authorization"]["signature"] = "0" * 128
    _resign(approval)
    owner_public_key = bytes.fromhex(trust_owner_public_key_hex)
    snapshot, _ = runner._build_trust_material(approval, owner_public_key)
    unsigned = runner._build_signed_approval(
        approval, snapshot.snapshot_digest, snapshot.signer_set_digest
    )
    message = approval_signature_message(unsigned)
    approval["owner_authorization"]["signature"] = private_key.sign(message).hex()
    _resign(approval)


def _signed_approval(
    manifest: dict[str, object],
) -> tuple[dict[str, object], ed25519.Ed25519PrivateKey, str]:
    """Build an approval bound to `manifest` and genuinely Ed25519-signed.

    Returns (approval, private_key, trust_owner_public_key_hex) — the hex
    string is what a caller passes as validate()'s/main()'s
    trust_owner_public_key_hex so the signature verifies.
    """
    approval = _bound_approval(manifest)
    private_key = ed25519.Ed25519PrivateKey.generate()
    trust_owner_public_key_hex = private_key.public_key().public_bytes_raw().hex()
    _sign_with_key(approval, private_key, trust_owner_public_key_hex)
    return approval, private_key, trust_owner_public_key_hex


def _nonprod_dependency_overrides(
    dependencies: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, str]]:
    """Return (new_dependencies, env_vars).

    Exporting env_vars makes NonproductionCredentialBroker grant every
    dependency in new_dependencies — each dependency's credential_ref/
    account_region_ref is replaced with the real SHA-256 digest of a
    synthetic nonproduction value the broker will look up by role.
    """
    new_dependencies = []
    env_vars: dict[str, str] = {}
    for dependency in dependencies:
        role = dependency["role"]
        credential_value = f"nonprod-credential-{role}"
        account_region_value = f"bakeoff-nonprod-{role}:us-west1"
        env_vars[f"BAKEOFF_NONPROD_CREDENTIAL__{role.upper()}"] = credential_value
        env_vars[f"BAKEOFF_NONPROD_ACCOUNT_REGION__{role.upper()}"] = account_region_value
        updated = dict(dependency)
        updated["credential_ref"] = hashlib.sha256(
            credential_value.encode("utf-8")
        ).hexdigest()
        updated["account_region_ref"] = hashlib.sha256(
            account_region_value.encode("utf-8")
        ).hexdigest()
        new_dependencies.append(updated)
    return new_dependencies, env_vars


def _bind_nonprod_dependencies(
    approval: dict[str, object], manifest: dict[str, object]
) -> dict[str, str]:
    """Mutate approval['dependencies'] in place to real broker-grantable
    values and rebind/resign the envelope. Returns the env vars a caller
    must export for the broker to grant them. Must run before signing —
    it changes self_digest."""
    new_dependencies, env_vars = _nonprod_dependency_overrides(approval["dependencies"])
    approval["dependencies"] = new_dependencies
    approval["dependency_inventory_digest"] = runner._dependency_inventory_digest(
        new_dependencies
    )
    manifest["candidate"]["dependency_inventory_digest"] = approval[
        "dependency_inventory_digest"
    ]
    _rebind_manifest(approval, manifest)
    return env_vars


def _configure_nonprod_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    approval: dict[str, object],
    manifest: dict[str, object],
) -> None:
    env_vars = _bind_nonprod_dependencies(approval, manifest)
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)


def test_valid_shape_still_requires_external_verification(monkeypatch: pytest.MonkeyPatch):
    manifest = _manifest()
    approval = _bound_approval(manifest)
    _configure_nonprod_dependencies(monkeypatch, approval, manifest)
    private_key = ed25519.Ed25519PrivateKey.generate()
    trust_owner_public_key_hex = private_key.public_key().public_bytes_raw().hex()
    _sign_with_key(approval, private_key, trust_owner_public_key_hex)
    assert (
        runner.validate(
            approval,
            manifest,
            "B1",
            "a" * 40,
            now_ms=1_000,
            trust_owner_public_key_hex=trust_owner_public_key_hex,
        )
        == []
    )


def test_rejects_template_wrong_binding_digest_authorization_caps_and_risky_features():
    manifest = _manifest()
    approval = _bound_approval(manifest)
    assert "template manifest" in runner.validate(approval, _manifest(True), "B1", "a" * 40)[0]
    approval["source_sha"] = "f" * 40
    assert "requested binding" in runner.validate(approval, _manifest(), "B1", "a" * 40)[0]
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["self_digest"] = "f" * 64
    assert "self digest" in runner.validate(approval, manifest, "B1", "a" * 40)[0]
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["owner_authorization"] = {"role": "owner", "identity": "same"}
    _resign(approval)
    assert "owner authorization" in runner.validate(approval, manifest, "B1", "a" * 40)[0]
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["technical_review"]["unresolved_p1_count"] = 1
    _resign(approval)
    assert "technical review" in runner.validate(approval, manifest, "B1", "a" * 40)[0]
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["technical_review"]["unresolved_p1_count"] = False
    _resign(approval)
    assert "technical review" in runner.validate(approval, manifest, "B1", "a" * 40)[0]
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["technical_review"]["source_sha"] = "f" * 40
    _resign(approval)
    assert "technical review" in runner.validate(approval, manifest, "B1", "a" * 40)[0]
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["technical_review"]["manifest_digest"] = "f" * 64
    _resign(approval)
    assert "technical review" in runner.validate(approval, manifest, "B1", "a" * 40)[0]
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["authorization_model"] = "quorum"
    _resign(approval)
    assert "authorization model" in runner.validate(approval, manifest, "B1", "a" * 40)[0]
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["signatures"] = []
    _resign(approval)
    assert "schema mismatch" in runner.validate(approval, manifest, "B1", "a" * 40)[0]
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["caps"] = {"requests": 0}
    _resign(approval)
    assert "caps" in runner.validate(approval, manifest, "B1", "a" * 40)[0]
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["disabled_features"] = {}
    _resign(approval)
    assert "risky" in runner.validate(approval, manifest, "B1", "a" * 40)[0]


def test_rejects_expired_invalid_owner_authorization_and_altered_manifest():
    manifest = _manifest()
    approval = _bound_approval(manifest)
    assert "expired" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=2_000)[0]
    approval = _bound_approval(manifest)
    approval["owner_authorization"].pop("signature")
    _resign(approval)
    assert "owner authorization" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]
    approval = _bound_approval(manifest)
    manifest["candidate"]["arm"] = "A"
    assert "manifest" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]


def test_rejects_nonclosed_sets_duplicate_dependencies_and_environment_mismatch():
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["disabled_features"]["future_feature"] = True
    _resign(approval)
    assert "risky" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]

    approval = _bound_approval(manifest)
    approval["caps"].pop("tokens")
    _resign(approval)
    assert "caps" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]

    approval = _bound_approval(manifest)
    approval["dependencies"].append(dict(approval["dependencies"][0]))
    _resign(approval)
    assert "dependency" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]

    approval = _bound_approval(manifest)
    approval["environment"] = "staging"
    _resign(approval)
    assert "manifest status" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]


def test_rejects_inventory_mismatch_and_mutated_authorization_metadata():
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["dependency_inventory_digest"] = "d" * 64
    _resign(approval)
    assert "inventory" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]

    approval = _bound_approval(manifest)
    approval["owner_authorization"]["key_id"] = "other_key"
    assert "self digest" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]

    approval = _bound_approval(manifest)
    approval["owner_authorization"] = "invalid"
    _resign(approval)
    assert "owner authorization" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]


def test_b2_requires_text_generation_dependency():
    manifest = _manifest()
    manifest["candidate"]["arm"] = "B2"
    approval = _bound_approval(manifest)
    approval["arm"] = "B2"
    approval["dependencies"] = [
        {
            key: role if key == "role" else f"{key}_{role}"
            for key in runner._DEPENDENCY_FIELDS
        }
        for role in runner._ARM_ROLES["B2"] - {"text_generation"}
    ]
    approval["dependency_inventory_digest"] = runner._dependency_inventory_digest(approval["dependencies"])
    manifest["candidate"]["dependency_inventory_digest"] = approval["dependency_inventory_digest"]
    _rebind_manifest(approval, manifest)
    assert "dependency" in runner.validate(approval, manifest, "B2", "a" * 40, now_ms=1_000)[0]


def test_forged_signature_is_rejected_before_credential_resolution(
    monkeypatch: pytest.MonkeyPatch,
):
    manifest = _manifest()
    approval, _key, trust_owner_public_key_hex = _signed_approval(manifest)
    approval["owner_authorization"]["signature"] = "f" * 128  # syntactically-valid hex, wrong signature
    _resign(approval)

    read_credential_keys: list[str] = []
    monkeypatch.setattr(
        runner.os.environ,
        "get",
        lambda key, *a: (read_credential_keys.append(key) or None) if "CREDENTIAL" in key else None,
    )

    errors = runner.validate(
        approval,
        manifest,
        "B1",
        "a" * 40,
        now_ms=1_000,
        trust_owner_public_key_hex=trust_owner_public_key_hex,
    )
    assert any("signature" in e or "verification" in e for e in errors)
    assert read_credential_keys == []


def test_wrong_owner_key_is_rejected():
    manifest = _manifest()
    approval, _real_key, trust_owner_public_key_hex = _signed_approval(manifest)
    wrong_key = ed25519.Ed25519PrivateKey.generate()
    # Re-sign with a different key while validate() still trusts the real
    # owner's public key — proves a signature from the wrong key is caught,
    # not just an absent/malformed trust configuration.
    _sign_with_key(approval, wrong_key, trust_owner_public_key_hex)

    errors = runner.validate(
        approval,
        manifest,
        "B1",
        "a" * 40,
        now_ms=1_000,
        trust_owner_public_key_hex=trust_owner_public_key_hex,
    )
    assert any("signature" in e or "verification" in e for e in errors)


def test_unconfigured_trust_key_is_rejected():
    manifest = _manifest()
    approval, _key, _trust_hex = _signed_approval(manifest)

    errors = runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)
    assert any("signature" in e or "verification" in e for e in errors)


def test_replayed_nonce_is_rejected_on_second_invocation(tmp_path: Path):
    manifest = _manifest()
    approval, _key, _trust_hex = _signed_approval(manifest)
    ledger = FileBackedNonceLedger(tmp_path / "ledger.json")

    first = ledger.admit(
        nonce_digest=approval["nonce"],
        approval_id_digest=approval["approval_id"],
        binding_digest=approval["self_digest"],
        epoch=1,
    )
    second = ledger.admit(
        nonce_digest=approval["nonce"],
        approval_id_digest=approval["approval_id"],
        binding_digest=approval["self_digest"],
        epoch=1,
    )
    assert first is True
    assert second is False


def test_credential_swapped_dependency_is_rejected():
    broker = NonproductionCredentialBroker(env={"BAKEOFF_NONPROD_CREDENTIAL__TELEPHONY": "wrong"})
    grant = broker.resolve(
        dependency_role="telephony",
        approved_credential_ref="0" * 64,
        approved_account_region_ref="0" * 64,
    )
    assert grant is None


def test_destination_mismatched_dependency_is_rejected():
    broker = NonproductionCredentialBroker(
        env={
            "BAKEOFF_NONPROD_CREDENTIAL__TELEPHONY": "cred",
            "BAKEOFF_NONPROD_ACCOUNT_REGION__TELEPHONY": "kevin-491315:us-central1",
        }
    )
    grant = broker.resolve(
        dependency_role="telephony",
        approved_credential_ref=hashlib.sha256(b"cred").hexdigest(),
        approved_account_region_ref=hashlib.sha256(b"kevin-491315:us-central1").hexdigest(),
    )
    assert grant is None  # production account/region is denylisted unconditionally


def test_credential_denial_surfaces_through_validate_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
):
    manifest = _manifest()
    approval, _key, trust_owner_public_key_hex = _signed_approval(manifest)
    # No BAKEOFF_NONPROD_CREDENTIAL__* / BAKEOFF_NONPROD_ACCOUNT_REGION__*
    # env vars are configured, so the broker denies every dependency.
    for key in list(os.environ):
        if key.startswith("BAKEOFF_NONPROD_"):
            monkeypatch.delenv(key, raising=False)

    errors = runner.validate(
        approval,
        manifest,
        "B1",
        "a" * 40,
        now_ms=1_000,
        trust_owner_public_key_hex=trust_owner_public_key_hex,
    )
    assert any("credential broker denied dependency" in e for e in errors)


def test_reviewer_same_as_signer_is_rejected():
    manifest = _manifest()
    approval, _key, trust_owner_public_key_hex = _signed_approval(manifest)
    approval["owner_authorization"]["identity"] = "ref_same_session"
    approval["technical_review"]["provenance_ref"] = "ref_same_session"
    _resign(approval)

    errors = runner.validate(
        approval,
        manifest,
        "B1",
        "a" * 40,
        now_ms=1_000,
        trust_owner_public_key_hex=trust_owner_public_key_hex,
    )
    assert any("reviewer" in e or "provenance" in e for e in errors)


def test_runner_contains_no_network_or_credential_imports():
    source = _SCRIPT.read_text(encoding="utf-8")
    imports = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not {
        imported
        for imported in imports
        if imported.split(".")[0]
        in {"socket", "requests", "http", "boto", "google", "secretmanager"}
    }


_OFFLINE_SOURCE_PATHS = {
    "scripts.run_voice_architecture_bakeoff": _SCRIPT,
    "scripts.voice_bakeoff_caller": Path("scripts/voice_bakeoff_caller.py"),
    "scripts.request_voice_bakeoff_review": Path(
        "scripts/request_voice_bakeoff_review.py"
    ),
    "app.services.voice_bakeoff_credential_broker": Path(
        "app/services/voice_bakeoff_credential_broker.py"
    ),
    "app.services.voice_bakeoff_nonce_ledger": Path(
        "app/services/voice_bakeoff_nonce_ledger.py"
    ),
    "app.services.voice_bakeoff_residue_audit": Path(
        "app/services/voice_bakeoff_residue_audit.py"
    ),
    "app.services.voice_bakeoff_security_contracts": Path(
        "app/services/voice_bakeoff_security_contracts.py"
    ),
}
_OFFLINE_APPROVED_SOURCE_DIGESTS = {
    "scripts.run_voice_architecture_bakeoff": (
        "a69dd22dc599e696654205af2d3adc10570013740575a9cfd6da4d945c3020be"
    ),
    "scripts.voice_bakeoff_caller": (
        "96971e32581823ba659723b4bb2f0a03260c05a67f114c437b4a7d316d0ab9ac"
    ),
    "scripts.request_voice_bakeoff_review": (
        "2a102679528d82ac46170053cdd76b475bec8984a9ba1b20f7bbff2ad9ccf8e6"
    ),
    "app.services.voice_bakeoff_credential_broker": (
        "d40630fe20fb0a930a59aa8e80a071e466b78725a239e6de74812ea65e6fd2ca"
    ),
    "app.services.voice_bakeoff_nonce_ledger": (
        "ce23e77690a32725e543c55665e430273b2fde5e28ebfb3705a52f4c86042e45"
    ),
    "app.services.voice_bakeoff_residue_audit": (
        "236deb489c68e27aaeec0439d5e411aa50f1d2c7461f17ee4f80214d0441d574"
    ),
    "app.services.voice_bakeoff_security_contracts": (
        "0f49fcd1dce75d05d205c9349765720e316c0f5955f373d99f64bdc174f3ea12"
    ),
}
_OFFLINE_ALLOWED_IMPORTS = {
    "scripts.run_voice_architecture_bakeoff": {
        ("from", "__future__", "annotations", ""),
        ("import", "argparse", "", ""),
        ("import", "hashlib", "", ""),
        ("import", "json", "", ""),
        ("import", "os", "", ""),
        ("import", "re", "", ""),
        ("import", "subprocess", "", ""),
        ("import", "time", "", ""),
        ("from", "pathlib", "Path", ""),
        (
            "from",
            "cryptography.hazmat.primitives.asymmetric.ed25519",
            "Ed25519PrivateKey",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_credential_broker",
            "NonproductionCredentialBroker",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_nonce_ledger",
            "FileBackedNonceLedger",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_residue_audit",
            "audit_residue",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "ApprovalArm",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "ApprovalCaps",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "ApprovalProvenanceSigner",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "DetachedApprovalSignature",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "InMemoryTrustGenerationPinStore",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "OfflineApprovalVerifier",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "SignedApproval",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "SignerRole",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "TechnicalReviewReceipt",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "TrustGenerationPin",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "TrustSnapshot",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "TrustSnapshotRootSigner",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "TrustSnapshotRootVerifier",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "TrustedSignerKey",
            "",
        ),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "approval_signature_payload",
            "",
        ),
        (
            "from",
            "scripts.voice_bakeoff_caller",
            "run_offline_self_check",
            "",
        ),
        ("from", "voice_bakeoff_caller", "run_offline_self_check", ""),
        (
            "from",
            "scripts.request_voice_bakeoff_review",
            "reviewer_is_procedurally_separate",
            "",
        ),
        (
            "from",
            "request_voice_bakeoff_review",
            "reviewer_is_procedurally_separate",
            "",
        ),
    },
    "scripts.voice_bakeoff_caller": {
        ("from", "__future__", "annotations", ""),
        ("import", "hashlib", "", ""),
        ("import", "json", "", ""),
        ("import", "random", "", ""),
        ("import", "secrets", "", ""),
        ("import", "tempfile", "", ""),
        ("import", "threading", "", ""),
        ("from", "collections.abc", "Callable", ""),
        ("from", "collections.abc", "Iterable", ""),
        ("from", "dataclasses", "dataclass", ""),
        ("from", "dataclasses", "field", ""),
        ("from", "pathlib", "Path", ""),
        ("from", "typing", "Protocol", ""),
        (
            "from",
            "cryptography.hazmat.primitives.ciphers.aead",
            "AESGCM",
            "",
        ),
    },
    "scripts.request_voice_bakeoff_review": {
        ("from", "__future__", "annotations", ""),
        (
            "from",
            "app.services.voice_bakeoff_security_contracts",
            "TechnicalReviewReceipt",
            "",
        ),
    },
    "app.services.voice_bakeoff_credential_broker": {
        ("from", "__future__", "annotations", ""),
        ("import", "dataclasses", "", ""),
        ("import", "hashlib", "", ""),
        ("from", "typing", "Mapping", ""),
    },
    "app.services.voice_bakeoff_nonce_ledger": {
        ("from", "__future__", "annotations", ""),
        ("import", "dataclasses", "", ""),
        ("import", "fcntl", "", ""),
        ("import", "json", "", ""),
        ("import", "os", "", ""),
        ("import", "pathlib", "", ""),
        ("import", "tempfile", "", ""),
    },
    "app.services.voice_bakeoff_residue_audit": {
        ("from", "__future__", "annotations", ""),
        ("import", "dataclasses", "", ""),
        ("import", "os", "", ""),
        ("import", "pathlib", "", ""),
    },
    "app.services.voice_bakeoff_security_contracts": {
        ("import", "dataclasses", "", ""),
        ("import", "enum", "", ""),
        ("import", "hashlib", "", ""),
        ("import", "hmac", "", ""),
        ("import", "json", "", ""),
        ("import", "secrets", "", ""),
        ("import", "threading", "", ""),
        ("import", "cryptography.exceptions", "", ""),
        (
            "import",
            "cryptography.hazmat.primitives.asymmetric.ed25519",
            "",
            "",
        ),
    },
}
_OFFLINE_ALLOWED_GETATTR = {
    "scripts.run_voice_architecture_bakeoff": [],
    "scripts.voice_bakeoff_caller": sorted(
        [
            ast.dump(
                ast.parse(expression, mode="eval").body,
                include_attributes=False,
            )
            for expression in (
                "getattr(current, name)",
                "getattr(current, name)",
                "getattr(self._caps, name)",
                "getattr(self._usage, name)",
                'getattr(session_runner, "budget", None)',
                'getattr(session_runner, "run", None)',
                'getattr(result, "returned_audio", None)',
            )
        ]
    ),
    "scripts.request_voice_bakeoff_review": [],
    "app.services.voice_bakeoff_credential_broker": [],
    "app.services.voice_bakeoff_nonce_ledger": [],
    "app.services.voice_bakeoff_residue_audit": [],
    "app.services.voice_bakeoff_security_contracts": sorted(
        [
            ast.dump(
                ast.parse(expression, mode="eval").body,
                include_attributes=False,
            )
            for expression in (
                # ApprovalCaps.canonical_value, VerifiedApproval field loops,
                # and every other dataclass's field-name-driven __post_init__
                # digest checks — 12 occurrences of getattr(self, field_name)
                # and 6 of getattr(self, field.name), counted directly from
                # the source (see the runner report for how this was derived).
                "getattr(self, field_name)",
                "getattr(self, field_name)",
                "getattr(self, field_name)",
                "getattr(self, field_name)",
                "getattr(self, field_name)",
                "getattr(self, field_name)",
                "getattr(self, field_name)",
                "getattr(self, field_name)",
                "getattr(self, field_name)",
                "getattr(self, field_name)",
                "getattr(self, field_name)",
                "getattr(self, field_name)",
                "getattr(self, field.name)",
                "getattr(self, field.name)",
                "getattr(self, field.name)",
                "getattr(self, field.name)",
                "getattr(self, field.name)",
                "getattr(self, field.name)",
            )
        ]
    ),
}
_OFFLINE_ALLOWED_FILE_IO = {
    "scripts.run_voice_architecture_bakeoff": sorted(
        [
            ast.dump(
                ast.parse(expression, mode="eval").body,
                include_attributes=False,
            )
            for expression in (
                "path.stat()",
                'path.read_text(encoding="utf-8")',
                "args.manifest.read_bytes()",
                'args.emit_signing_payload.write_text(payload_text, encoding="utf-8")',
            )
        ]
    ),
    "scripts.voice_bakeoff_caller": sorted(
        [
            ast.dump(
                ast.parse(expression, mode="eval").body,
                include_attributes=False,
            )
            for expression in (
                "path.write_bytes(encrypted)",
                "path.read_bytes()",
            )
        ]
    ),
    "scripts.request_voice_bakeoff_review": [],
    "app.services.voice_bakeoff_credential_broker": [],
    "app.services.voice_bakeoff_nonce_ledger": sorted(
        [
            ast.dump(
                ast.parse(expression, mode="eval").body,
                include_attributes=False,
            )
            for expression in (
                'self._lock_path.open("r+", encoding="utf-8")',
                'self._path.read_text(encoding="utf-8")',
            )
        ]
    ),
    "app.services.voice_bakeoff_residue_audit": sorted(
        [
            ast.dump(
                ast.parse(expression, mode="eval").body,
                include_attributes=False,
            )
            for expression in ("path.stat()",)
        ]
    ),
    "app.services.voice_bakeoff_security_contracts": [],
}


def _resolved_from_module(module_name: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = module_name.split(".")[:-1]
    keep = len(package) - (node.level - 1)
    prefix = package[: max(keep, 0)]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def _import_contract(
    module_name: str,
    tree: ast.AST,
) -> tuple[
    set[tuple[str, str, str, str]],
    set[str],
]:
    records: set[tuple[str, str, str, str]] = set()
    local_dependencies: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                records.add(("import", alias.name, "", alias.asname or ""))
                if alias.name.startswith(("scripts.", "app.services.voice_bakeoff_")):
                    local_dependencies.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolved_from_module(module_name, node)
            for alias in node.names:
                records.add(("from", base, alias.name, alias.asname or ""))
            if base in ("voice_bakeoff_caller", "request_voice_bakeoff_review"):
                local_dependencies.add(f"scripts.{base}")
            elif base.startswith(("scripts.", "app.services.voice_bakeoff_")):
                local_dependencies.add(base)
    return records, local_dependencies


def _offline_firewall_errors(
    overrides: dict[str, str] | None = None,
) -> list[str]:
    source_overrides = overrides or {}
    errors: list[str] = []
    # app.services.* (unlike scripts/, a namespace package) has a real,
    # pre-existing __init__.py per directory — app/__init__.py and
    # app/services/__init__.py both predate this plan and are part of the
    # unrelated FastAPI application layout. A non-empty initializer could
    # hide code that runs on import without ever appearing in the AST walk
    # below, so this still fails closed on content — it only stops
    # rejecting an initializer's mere *existence* once confirmed empty.
    package_initializers = {
        path.parent / "__init__.py"
        for path in _OFFLINE_SOURCE_PATHS.values()
        if (path.parent / "__init__.py").exists()
    }
    non_empty_initializers = {
        path
        for path in package_initializers
        if path.read_text(encoding="utf-8").strip()
    }
    if non_empty_initializers:
        errors.append("unapproved package initializer")
    pending = ["scripts.run_voice_architecture_bakeoff"]
    visited: set[str] = set()
    while pending:
        module_name = pending.pop()
        if module_name in visited:
            continue
        visited.add(module_name)
        path = _OFFLINE_SOURCE_PATHS.get(module_name)
        if path is None:
            errors.append(f"unapproved local dependency: {module_name}")
            continue
        source = source_overrides.get(
            module_name,
            path.read_text(encoding="utf-8"),
        )
        if (
            hashlib.sha256(source.encode("utf-8")).hexdigest()
            != _OFFLINE_APPROVED_SOURCE_DIGESTS[module_name]
        ):
            errors.append(f"source digest mismatch: {module_name}")
        tree = ast.parse(source)
        records, dependencies = _import_contract(module_name, tree)
        if records != _OFFLINE_ALLOWED_IMPORTS[module_name]:
            errors.append(f"import contract mismatch: {module_name}")
        pending.extend(dependencies - visited)

        forbidden_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id
            in {
                "__import__",
                "compile",
                "eval",
                "exec",
                "globals",
                "locals",
                "open",
                "vars",
            }
        }
        if forbidden_names:
            errors.append(f"forbidden builtin call: {module_name}")

        forbidden_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {
                "create_connection",
                "create_subprocess_exec",
                "create_subprocess_shell",
                "entry_points",
                "getenv",
                "import_module",
                "load_module",
                "open_connection",
                "popen",
                "system",
                "urlopen",
            }
        }
        if forbidden_attributes:
            errors.append(f"forbidden dynamic or authority call: {module_name}")

        if any(
            isinstance(node, ast.Name) and node.id == "__builtins__"
            for node in ast.walk(tree)
        ):
            errors.append(f"forbidden builtins reference: {module_name}")

        getattr_calls = sorted(
            ast.dump(node, include_attributes=False)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
        )
        if getattr_calls != _OFFLINE_ALLOWED_GETATTR[module_name]:
            errors.append(f"getattr contract mismatch: {module_name}")

        file_io_calls = sorted(
            ast.dump(node, include_attributes=False)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {
                "open",
                "read_bytes",
                "read_text",
                "stat",
                "write_bytes",
                "write_text",
            }
        )
        if file_io_calls != _OFFLINE_ALLOWED_FILE_IO[module_name]:
            errors.append(f"filesystem I/O contract mismatch: {module_name}")

        subprocess_references = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "subprocess"
        ]
        if module_name == "scripts.run_voice_architecture_bakeoff":
            subprocess_calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ]
            expected = ast.parse(
                'subprocess.check_output('
                '["git", "-C", str(root), "rev-parse", "HEAD"], text=True'
                ")",
                mode="eval",
            ).body
            if (
                len(subprocess_calls) != 1
                or len(subprocess_references) != 1
                or ast.dump(subprocess_calls[0], include_attributes=False)
                != ast.dump(expected, include_attributes=False)
            ):
                errors.append("subprocess contract mismatch")
        elif subprocess_references:
            errors.append(f"subprocess reference outside runner: {module_name}")
    return errors


def test_runner_and_reachable_offline_harness_have_no_execution_escape_hatches():
    assert _offline_firewall_errors() == []


@pytest.mark.parametrize(
    "snippet",
    (
        "from socket import create_connection",
        "from subprocess import run\nrun(['true'])",
        "import subprocess as sp\nsp.run(['true'])",
        "import os\nos.system('true')",
        "import asyncio\nasyncio.open_connection('localhost', 1)",
        "from .provider import Client",
        (
            "from importlib import import_module as load\n"
            "load('scripts.provider_execution')"
        ),
        "import os\nos.getenv('PROVIDER_API_KEY')",
        "from google.cloud import secretmanager",
        "getattr(subprocess, 'run')(['true'])",
        "subprocess.__dict__['run'](['true'])",
        "__builtins__['__import__']('socket')",
        "__builtins__['__import__']('subprocess').run(['true'])",
        "getattr(__builtins__, '__import__')('socket')",
        "Path('.env').read_text()",
        "Path('/proc/self/environ').read_bytes()",
        "getattr(Path('.env'), 'read_text')()",
        "open('.env').read()",
    ),
)
def test_offline_ast_firewall_rejects_mutated_authority_paths(snippet: str):
    module_name = "scripts.run_voice_architecture_bakeoff"
    mutated = (
        _OFFLINE_SOURCE_PATHS[module_name].read_text(encoding="utf-8")
        + "\n"
        + snippet
        + "\n"
    )
    assert _offline_firewall_errors({module_name: mutated})


def test_execute_provider_is_rejected_before_inputs_or_subprocess(
    monkeypatch: pytest.MonkeyPatch,
):
    contacts: list[str] = []

    def forbidden_contact(*args: object, **kwargs: object) -> None:
        contacts.append("contact")
        raise AssertionError("provider execution refusal must happen during parsing")

    monkeypatch.setattr(runner, "_load", forbidden_contact)
    monkeypatch.setattr(runner.subprocess, "check_output", forbidden_contact)
    monkeypatch.setattr(runner, "run_offline_self_check", forbidden_contact)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(_SCRIPT),
            "--arm",
            "B1",
            "--manifest",
            "must_not_be_read.json",
            "--approval",
            "must_not_be_read.json",
            "--dry-run",
            "--nonce-ledger",
            "must_not_be_read_ledger.json",
            "--residue-destination",
            "must_not_be_read_residue",
            "--execute-provider",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2
    assert contacts == []


def test_repository_templates_remain_nonexecuting_and_unsealed():
    schema = json.loads(
        Path(
            "tests/fixtures/voice_architecture_bakeoff/provider_approval.schema.json"
        ).read_text(encoding="utf-8")
    )
    manifest = json.loads(
        Path("tests/fixtures/voice_architecture_bakeoff/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["x-execution"] == "unsupported"
    assert manifest["authorization_status"] == "template_only"
    assert manifest["cap_configuration"] == {
        "configuration_reference": (
            "REPLACE_WITH_SEALED_PER_WINDOW_CAP_CONFIGURATION_REFERENCE"
        ),
        "max_requests": 0,
        "max_concurrency": 0,
        "max_duration_seconds": 0,
        "max_bytes": 0,
        "max_audio_seconds": 0,
        "max_retries": 0,
        "max_output_tokens": 0,
        "max_spend_minor_units": 0,
    }


def test_all_offline_candidate_adapters_are_registered_without_importing_them():
    assert runner._OFFLINE_ADAPTERS == {
        "A": "app.services.voice_candidates.native_gemini:NativeGeminiAdapter",
        "B1": "app.services.voice_candidates.chained_streaming:ChainedStreamingAdapter",
        "B2": "app.services.voice_candidates.conversation_relay:ConversationRelayAdapter",
        "C": "app.services.voice_candidates.manual_native:ManualNativeAdapter",
    }
    manifest = _manifest()
    approval = _bound_approval(manifest)
    assert "not registered" in runner.validate(
        approval,
        manifest,
        "unknown",
        "a" * 40,
        now_ms=1_000,
    )[0]
    assert (
        runner._OFFLINE_HARNESS
        == "scripts/voice_bakeoff_caller.py:run_offline_self_check"
    )


def _write_valid_cli_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, str], str]:
    """Write a fully valid, genuinely-signed manifest/approval pair to disk.

    Returns (manifest_path, approval_path, nonprod_env_vars,
    trust_owner_public_key_hex) ready to drive the CLI end-to-end.
    """
    source_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    manifest = _manifest()
    manifest["candidate"]["source_sha"] = source_sha
    approval = _approval()
    approval["issued_at_ms"] = int(time.time() * 1000)
    approval["expires_at_ms"] = approval["issued_at_ms"] + 60_000
    approval["source_sha"] = source_sha
    approval["technical_review"]["source_sha"] = source_sha
    new_dependencies, env_vars = _nonprod_dependency_overrides(approval["dependencies"])
    approval["dependencies"] = new_dependencies
    approval["dependency_inventory_digest"] = runner._dependency_inventory_digest(
        new_dependencies
    )
    manifest["candidate"]["dependency_inventory_digest"] = approval[
        "dependency_inventory_digest"
    ]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    approval["manifest_digest"] = runner._manifest_digest_bytes(manifest_path.read_bytes())
    approval["technical_review"]["manifest_digest"] = approval["manifest_digest"]
    _resign(approval)

    private_key = ed25519.Ed25519PrivateKey.generate()
    trust_owner_public_key_hex = private_key.public_key().public_bytes_raw().hex()
    _sign_with_key(approval, private_key, trust_owner_public_key_hex)

    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(approval, separators=(",", ":")), encoding="utf-8")
    return manifest_path, approval_path, env_vars, trust_owner_public_key_hex


def test_cli_valid_local_envelope_stops_at_external_verification(tmp_path: Path):
    manifest_path, approval_path, env_vars, trust_owner_public_key_hex = (
        _write_valid_cli_fixture(tmp_path)
    )
    nonce_ledger_path = tmp_path / "ledger.json"
    residue_destination_path = tmp_path / "residue"

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--arm",
            "B1",
            "--manifest",
            str(manifest_path),
            "--approval",
            str(approval_path),
            "--dry-run",
            "--nonce-ledger",
            str(nonce_ledger_path),
            "--residue-destination",
            str(residue_destination_path),
            "--trust-owner-public-key",
            trust_owner_public_key_hex,
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **env_vars, "PYTHONPATH": str(_REPO_ROOT)},
    )
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["error_count"] == 0
    assert payload["verdict"] == "blocked_external_verification_required"
    assert payload["residue_audit"] == {
        "passed": True,
        "remaining_paths": [],
        "unreadable_paths": [],
    }
    assert result.stderr == ""


def test_cli_rejects_replayed_nonce_on_second_invocation(tmp_path: Path):
    manifest_path, approval_path, env_vars, trust_owner_public_key_hex = (
        _write_valid_cli_fixture(tmp_path)
    )
    nonce_ledger_path = tmp_path / "ledger.json"
    residue_destination_path = tmp_path / "residue"
    argv = [
        sys.executable,
        str(_SCRIPT),
        "--arm",
        "B1",
        "--manifest",
        str(manifest_path),
        "--approval",
        str(approval_path),
        "--dry-run",
        "--nonce-ledger",
        str(nonce_ledger_path),
        "--residue-destination",
        str(residue_destination_path),
        "--trust-owner-public-key",
        trust_owner_public_key_hex,
    ]
    run_env = {**os.environ, **env_vars, "PYTHONPATH": str(_REPO_ROOT)}

    first = subprocess.run(argv, check=False, capture_output=True, text=True, env=run_env)
    second = subprocess.run(argv, check=False, capture_output=True, text=True, env=run_env)

    assert first.returncode == 3
    assert second.returncode == 2
    second_payload = json.loads(second.stdout)
    assert second_payload["verdict"] == "rejected_local_preflight"
    assert second_payload["error_count"] == 1


def test_cli_shape_corrupted_nonce_ledger_fails_closed_not_crashes(tmp_path: Path):
    """Regression test for a reviewer-confirmed crash: a nonce-ledger file
    containing valid-JSON-but-wrong-shape content (e.g. a bare `[]` instead
    of the expected {"consumed_nonces": [...], ...} object) used to raise
    an uncaught AttributeError out of FileBackedNonceLedger.admit() —
    'list' object has no attribute 'get' — which propagated past main()'s
    own `except (OSError, ValueError, json.JSONDecodeError)` (AttributeError
    is not in that tuple), crashing the process with exit 1 and a raw
    traceback on stderr instead of the documented JSON-on-stdout contract.
    `_LedgerState.from_json`'s except clause now also catches
    (AttributeError, TypeError, ValueError), so this must produce the same
    clean `rejected_local_preflight` / exit 2 outcome truncated JSON already
    did before this fix — never a crash.
    """
    manifest_path, approval_path, env_vars, trust_owner_public_key_hex = (
        _write_valid_cli_fixture(tmp_path)
    )
    nonce_ledger_path = tmp_path / "ledger.json"
    nonce_ledger_path.write_text("[]", encoding="utf-8")
    residue_destination_path = tmp_path / "residue"

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--arm",
            "B1",
            "--manifest",
            str(manifest_path),
            "--approval",
            str(approval_path),
            "--dry-run",
            "--nonce-ledger",
            str(nonce_ledger_path),
            "--residue-destination",
            str(residue_destination_path),
            "--trust-owner-public-key",
            trust_owner_public_key_hex,
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **env_vars, "PYTHONPATH": str(_REPO_ROOT)},
    )
    assert result.returncode == 2
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "rejected_local_preflight"
    assert payload["error_count"] == 1


def test_cli_deeply_nested_nonce_ledger_fails_closed_not_crashes(tmp_path: Path):
    """Regression test for a confirmation-review-found crash: a nonce-ledger
    file containing deeply-nested JSON (e.g. 20,000 levels of `[[[...]]]`)
    makes `json.loads` itself raise RecursionError while parsing — before
    `_LedgerState.from_json` is even reached. RecursionError subclasses
    RuntimeError, not any of (json.JSONDecodeError, AttributeError,
    TypeError, ValueError), so it escaped both FileBackedNonceLedger.admit()'s
    except tuple and main()'s outer except tuple, crashing with exit 1 and a
    raw traceback instead of the documented JSON-on-stdout contract. Both
    tuples now include RecursionError; this must produce the same clean
    rejected_local_preflight / exit 2 outcome the other corruption shapes do.
    """
    manifest_path, approval_path, env_vars, trust_owner_public_key_hex = (
        _write_valid_cli_fixture(tmp_path)
    )
    nonce_ledger_path = tmp_path / "ledger.json"
    depth = 20_000
    nonce_ledger_path.write_text("[" * depth + "]" * depth, encoding="utf-8")
    residue_destination_path = tmp_path / "residue"

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--arm",
            "B1",
            "--manifest",
            str(manifest_path),
            "--approval",
            str(approval_path),
            "--dry-run",
            "--nonce-ledger",
            str(nonce_ledger_path),
            "--residue-destination",
            str(residue_destination_path),
            "--trust-owner-public-key",
            trust_owner_public_key_hex,
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **env_vars, "PYTHONPATH": str(_REPO_ROOT)},
    )
    assert result.returncode == 2
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "rejected_local_preflight"
    assert payload["error_count"] == 1


@pytest.mark.parametrize(
    "ledger_content",
    (
        '{"consumed_nonces": [1, "a"]}',
        '{"consumed_nonces": ["a", null]}',
        '{"binding_epochs": [[1, 2]]}',
    ),
    ids=(
        "consumed_nonces_int_element",
        "consumed_nonces_null_element",
        "binding_epochs_list_value",
    ),
)
def test_cli_shape_corrupted_nonce_ledger_field_types_fail_closed_not_crash(
    tmp_path: Path, ledger_content: str
):
    """Confirmation-review follow-up to
    test_cli_shape_corrupted_nonce_ledger_fails_closed_not_crashes above.
    That test proved the one reported shape (a bare `[]`) fails closed, but
    a confirmation reviewer then drove a broader exploratory matrix of
    malformed ledger shapes through the real CLI and found the underlying
    gap was NOT actually closed — only that one specific shape stopped
    crashing. These three shapes are syntactically-valid top-level objects
    (so `_LedgerState.from_json`'s old `payload.get(...)` calls succeeded)
    whose FIELDS held the wrong element/value types, so the crash only
    surfaced one layer deeper, inside admit()'s write-back path:
    `sorted(self.consumed_nonces)` inside `_write_atomic` raised an
    uncaught TypeError when a list element wasn't a str (int/None don't
    order against str), crashing the runner with exit 1 and a raw
    traceback — same class of bug as the `[]` case, just deferred past
    from_json instead of happening inside it.
    `_LedgerState.from_json` now validates every field's element/value
    types directly (see `_require_string_list`/`_require_string_mapping` in
    app/services/voice_bakeoff_nonce_ledger.py), so each of these must now
    produce the same clean `rejected_local_preflight` / exit 2 outcome the
    `[]` case already did, never a crash.
    """
    manifest_path, approval_path, env_vars, trust_owner_public_key_hex = (
        _write_valid_cli_fixture(tmp_path)
    )
    nonce_ledger_path = tmp_path / "ledger.json"
    nonce_ledger_path.write_text(ledger_content, encoding="utf-8")
    residue_destination_path = tmp_path / "residue"

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--arm",
            "B1",
            "--manifest",
            str(manifest_path),
            "--approval",
            str(approval_path),
            "--dry-run",
            "--nonce-ledger",
            str(nonce_ledger_path),
            "--residue-destination",
            str(residue_destination_path),
            "--trust-owner-public-key",
            trust_owner_public_key_hex,
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **env_vars, "PYTHONPATH": str(_REPO_ROOT)},
    )
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "rejected_local_preflight"
    assert payload["error_count"] == 1


def test_cli_shape_corrupted_nonce_ledger_string_field_does_not_fail_open(
    tmp_path: Path,
):
    """CLI-level regression test for a pre-existing (not prior-fix-
    introduced) fail-open gap the same confirmation-review sweep also
    found: before strict shape validation, `_LedgerState.from_json` did
    `frozenset(payload.get("consumed_nonces", []))`. Python's frozenset()
    silently iterates a STRING value character-by-character instead of
    raising, so a ledger file recording the real nonce as a bare string
    (instead of the expected single-element list) would be read back as a
    set of individual characters that the real (multi-character) nonce
    string can never equal — so `nonce_digest in state.consumed_nonces`
    would find no match and let the runner wrongly admit (and report
    success for) a nonce the ledger file was actually recording as already
    consumed. That is fail-OPEN on corrupted/tampered state, the opposite
    of what a one-use replay ledger exists to guarantee. This must now
    produce the same clean `rejected_local_preflight` / exit 2 outcome an
    actual detected replay already does — never a silent wrongful
    admission.
    """
    manifest_path, approval_path, env_vars, trust_owner_public_key_hex = (
        _write_valid_cli_fixture(tmp_path)
    )
    approval_nonce = json.loads(approval_path.read_text(encoding="utf-8"))["nonce"]
    nonce_ledger_path = tmp_path / "ledger.json"
    nonce_ledger_path.write_text(
        json.dumps({"consumed_nonces": approval_nonce}), encoding="utf-8"
    )
    residue_destination_path = tmp_path / "residue"

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--arm",
            "B1",
            "--manifest",
            str(manifest_path),
            "--approval",
            str(approval_path),
            "--dry-run",
            "--nonce-ledger",
            str(nonce_ledger_path),
            "--residue-destination",
            str(residue_destination_path),
            "--trust-owner-public-key",
            trust_owner_public_key_hex,
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **env_vars, "PYTHONPATH": str(_REPO_ROOT)},
    )
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "rejected_local_preflight"
    assert payload["error_count"] == 1


def test_residue_audit_filesystem_error_fails_closed_not_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """Regression test for a reviewer-confirmed crash: main() used to call
    audit_residue() entirely outside its try/except block — after that
    block had already closed — so any exception it raised escaped uncaught,
    crashing the process with exit 1 and a raw traceback on stderr instead
    of the documented always-print-JSON contract. Same bug class already
    fixed twice elsewhere in this file for the nonce ledger (see
    test_cli_shape_corrupted_nonce_ledger_fails_closed_not_crashes and
    test_cli_deeply_nested_nonce_ledger_fails_closed_not_crashes).

    audit_residue() now runs from inside the same try block that already
    wraps input loading/validation/nonce admission, so any
    OSError/ValueError/RecursionError it raises is caught the same way
    those steps' own errors already are. After the residue-audit module's
    own fail-closed fix (Finding 1), a merely-unreadable directory no
    longer makes audit_residue() raise at all — it fails closed via its
    return value instead — so this is deliberately about defense-in-depth
    against a DIFFERENT filesystem failure audit_residue() cannot itself
    convert into a result (e.g. a path vanishing mid-walk after os.walk's
    own listing of it already succeeded). Simulated directly by making
    audit_residue() raise, rather than trying to reproduce a real
    filesystem race, which would be slow and flaky.
    """
    manifest_path, approval_path, env_vars, trust_owner_public_key_hex = (
        _write_valid_cli_fixture(tmp_path)
    )
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)

    def _raise_mid_walk_error(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated: path removed mid-walk")

    monkeypatch.setattr(runner, "audit_residue", _raise_mid_walk_error)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(_SCRIPT),
            "--arm",
            "B1",
            "--manifest",
            str(manifest_path),
            "--approval",
            str(approval_path),
            "--dry-run",
            "--nonce-ledger",
            str(tmp_path / "ledger.json"),
            "--residue-destination",
            str(tmp_path / "residue"),
            "--trust-owner-public-key",
            trust_owner_public_key_hex,
        ],
    )

    exit_code = runner.main()

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["verdict"] == "rejected_local_preflight"
    assert payload["error_count"] == 1
    assert payload["residue_audit"] is None


def test_emit_signing_payload_rejection_also_reports_null_residue_audit(
    tmp_path: Path,
):
    """Companion to test_emit_signing_payload_closes_the_loop_with_the_real_
    signing_cli's happy-path shape assertion below: --emit-signing-payload's
    REJECTED path must report the same stable top-level JSON shape
    (error_count/verdict/residue_audit) its success path and normal mode
    both do — not have the key appear only when things go well. Omitting
    --trust-owner-public-key is the simplest way to force
    rejected_local_preflight here without constructing an otherwise-invalid
    envelope.
    """
    manifest_path, approval_path, env_vars, _trust_owner_public_key_hex = (
        _write_valid_cli_fixture(tmp_path)
    )
    payload_path = tmp_path / "signing_payload.json"

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--arm",
            "B1",
            "--manifest",
            str(manifest_path),
            "--approval",
            str(approval_path),
            "--dry-run",
            "--nonce-ledger",
            str(tmp_path / "unused_ledger.json"),
            "--residue-destination",
            str(tmp_path / "unused_residue"),
            "--emit-signing-payload",
            str(payload_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **env_vars, "PYTHONPATH": str(_REPO_ROOT)},
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "verdict": "rejected_local_preflight",
        "error_count": 1,
        "residue_audit": None,
    }
    assert not payload_path.exists()


def test_emit_signing_payload_closes_the_loop_with_the_real_signing_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end proof that --emit-signing-payload actually closes the gap:
    an operator with only scripts/sign_voice_bakeoff_approval.py's real
    sign_payload() — never reaching into runner-private helpers like
    _build_trust_material/_build_signed_approval the way this file's own
    _sign_with_key test fixture does — can produce a signature
    OfflineApprovalVerifier.verify() actually accepts.

    1. Write a valid approval/manifest pair to disk with a syntactically
       valid but not-yet-real placeholder owner_authorization.signature.
    2. Run the runner's real --emit-signing-payload CLI mode as a subprocess
       (the actual new interface, not an in-process shortcut) to produce a
       JSON payload file.
    3. Sign that exact payload with sign_voice_bakeoff_approval.sign_payload()
       — Task 4's real, independent signing function — under the real
       _APPROVAL_DOMAIN, exactly as the brief for this fix specifies.
    4. Embed the resulting signature into the approval dict's
       owner_authorization.signature and confirm runner.validate(...) now
       reaches errors == [] — the same "blocked_external_verification_required
       with zero errors" outcome the pre-existing valid-envelope tests check
       (test_valid_shape_still_requires_external_verification,
       test_cli_valid_local_envelope_stops_at_external_verification) —
       proving this is a genuinely verifiable signature, not just a
       plausible-looking one.
    """
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    manifest = _manifest()
    manifest["candidate"]["source_sha"] = source_sha
    approval = _approval()
    approval["issued_at_ms"] = int(time.time() * 1000)
    approval["expires_at_ms"] = approval["issued_at_ms"] + 60_000
    approval["source_sha"] = source_sha
    approval["technical_review"]["source_sha"] = source_sha
    new_dependencies, env_vars = _nonprod_dependency_overrides(approval["dependencies"])
    approval["dependencies"] = new_dependencies
    approval["dependency_inventory_digest"] = runner._dependency_inventory_digest(
        new_dependencies
    )
    manifest["candidate"]["dependency_inventory_digest"] = approval[
        "dependency_inventory_digest"
    ]

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    approval["manifest_digest"] = runner._manifest_digest_bytes(manifest_path.read_bytes())
    approval["technical_review"]["manifest_digest"] = approval["manifest_digest"]

    # Placeholder: syntactically valid 64-byte hex, so --emit-signing-payload's
    # _build_signed_approval (DetachedApprovalSignature construction)
    # succeeds. Its value is excluded from self_digest by _canonical_digest
    # and does not appear in approval_signature_message's payload at all —
    # only the fields that DO affect the message need to already be final,
    # and they are.
    approval["owner_authorization"]["signature"] = "0" * 128
    _resign(approval)

    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(approval, separators=(",", ":")), encoding="utf-8")

    private_key = ed25519.Ed25519PrivateKey.generate()
    trust_owner_public_key_hex = private_key.public_key().public_bytes_raw().hex()
    payload_path = tmp_path / "signing_payload.json"

    emit_result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--arm",
            "B1",
            "--manifest",
            str(manifest_path),
            "--approval",
            str(approval_path),
            "--dry-run",
            "--nonce-ledger",
            str(tmp_path / "unused_ledger.json"),
            "--residue-destination",
            str(tmp_path / "unused_residue"),
            "--trust-owner-public-key",
            trust_owner_public_key_hex,
            "--emit-signing-payload",
            str(payload_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
    )
    assert emit_result.returncode == 0, (emit_result.stdout, emit_result.stderr)
    assert json.loads(emit_result.stdout) == {
        "verdict": "signing_payload_emitted",
        "error_count": 0,
        "residue_audit": None,
    }
    assert emit_result.stderr == ""

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    signature = sign_payload(private_key, domain=_APPROVAL_DOMAIN, payload=payload)

    approval["owner_authorization"]["signature"] = signature.hex()
    _resign(approval)

    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)

    errors = runner.validate(
        approval,
        manifest,
        "B1",
        source_sha,
        trust_owner_public_key_hex=trust_owner_public_key_hex,
    )
    assert errors == []


def test_documented_step_order_technical_review_before_emit_produces_valid_signature(
    tmp_path: Path,
):
    """Regression test for a reviewer-confirmed doc bug: the workflow in
    docs/security/task-4-8-provider-approval-mechanism.md used to order its
    steps emit (1) -> sign (2) -> embed (3) -> obtain review receipt and
    populate technical_review (4) -> run (5). That is backwards on both
    ends:

    - `technical_review` is covered by `self_digest` (see
      `runner._canonical_digest`) AND by the signed message itself
      (`approval_signature_payload`'s `_approval_message_value` embeds
      `technical_review.canonical_value()` directly — see
      `app/services/voice_bakeoff_security_contracts.py`), so populating it
      after signing silently invalidates the signature.
    - `--emit-signing-payload` runs the same shape check `validate()` does,
      which requires a COMPLETE, valid `technical_review` already present
      (`unresolved_p1_count == 0`, `advisory_only is True`, matching
      `source_sha`/`manifest_digest`) before it will emit anything — so the
      old step 1 could not even succeed before the old step 4 ran.

    The doc now orders review-and-populate first. This test drives that
    corrected order literally — technical_review populated first, then
    emit, then sign, then embed, then run — as real subprocess CLI calls
    for the runner (mirroring
    test_emit_signing_payload_closes_the_loop_with_the_real_signing_cli),
    to prove the DOCUMENTED sequence itself produces a valid, verifiable
    approval the real runner CLI accepts end to end — not just that some
    working order exists. This is the test that should have existed before
    the doc bug shipped, and didn't.
    """
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    # --- Doc step 0 (unchanged): the owner already has an Ed25519 keypair.
    # (The doc's bootstrap dance is about minting
    # sign_voice_bakeoff_approval.py's on-disk key file the first time;
    # here, like the other CLI tests in this file, we only need the
    # keypair itself.)
    private_key = ed25519.Ed25519PrivateKey.generate()
    trust_owner_public_key_hex = private_key.public_key().public_bytes_raw().hex()

    # Everything the approval needs to state its terms — arm, caps,
    # dependencies, disabled features, the manifest binding — decided
    # before any review can happen, since that is what the reviewer
    # reviews. owner_authorization.signature stays a placeholder; it is
    # excluded from self_digest and the signed message either way, so it
    # does not matter yet.
    manifest = _manifest()
    manifest["candidate"]["source_sha"] = source_sha
    approval = _approval()
    approval["issued_at_ms"] = int(time.time() * 1000)
    approval["expires_at_ms"] = approval["issued_at_ms"] + 60_000
    approval["source_sha"] = source_sha
    new_dependencies, env_vars = _nonprod_dependency_overrides(approval["dependencies"])
    approval["dependencies"] = new_dependencies
    approval["dependency_inventory_digest"] = runner._dependency_inventory_digest(
        new_dependencies
    )
    manifest["candidate"]["dependency_inventory_digest"] = approval[
        "dependency_inventory_digest"
    ]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    approval["manifest_digest"] = runner._manifest_digest_bytes(manifest_path.read_bytes())

    owner_identity = "ref_documented_order_owner"
    reviewer_identity = "ref_documented_order_reviewer"
    approval["owner_authorization"] = {
        "role": "owner",
        "identity": owner_identity,
        "key_id": "ref_documented_order_key",
        "algorithm": "ed25519",
        "signature": "0" * 128,
    }

    # --- Doc step 1 (the corrected position — before emit): obtain the
    # independent technical review receipt and populate technical_review.
    #
    # technical_review does not exist yet, so the digest sent to the
    # reviewer for correlation is computed over the approval without it —
    # not the eventual self_digest, which cannot exist before the review
    # outcome does (exactly as the doc's step 1 now explains).
    predraft_digest = runner._canonical_digest(
        {key: value for key, value in approval.items() if key != "technical_review"}
    )
    request = build_receipt_request(
        predraft_digest,
        approval["manifest_digest"],
        source_sha=source_sha,
        manifest_digest=approval["manifest_digest"],
    )
    # Simulate the procedurally separate reviewer's response coming back.
    reviewer_response = {
        "review_digest": hashlib.sha256(
            b"documented-order regression review, no P1s"
        ).hexdigest(),
        "provenance_ref": reviewer_identity,
        "reviewed_payload_digest": request["payload_digest"],
        "reviewed_binding_digest": request["binding_digest"],
        "unresolved_p1_count": 0,
        "advisory_only": True,
    }
    receipt = parse_review_response(
        reviewer_response,
        expected_payload_digest=request["payload_digest"],
        expected_binding_digest=request["binding_digest"],
    )
    assert reviewer_is_procedurally_separate(
        signer_provenance_ref=owner_identity,
        reviewer_provenance_ref=receipt.provenance_ref,
    )
    approval["technical_review"] = {
        "review_digest": receipt.review_digest,
        "provenance_ref": receipt.provenance_ref,
        "source_sha": source_sha,
        "manifest_digest": approval["manifest_digest"],
        "unresolved_p1_count": receipt.unresolved_p1_count,
        "advisory_only": receipt.advisory_only,
    }
    # Only now, with technical_review final, can self_digest be computed —
    # exactly as the doc's step 1 says.
    _resign(approval)

    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(approval, separators=(",", ":")), encoding="utf-8")
    run_env = {**os.environ, **env_vars, "PYTHONPATH": str(_REPO_ROOT)}

    # --- Doc step 2 (was step 1): emit the signing payload. This is the
    # step that used to run BEFORE technical_review existed at all; it can
    # only succeed now because technical_review — and therefore self_digest
    # — is already final.
    payload_path = tmp_path / "signing_payload.json"
    emit_result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--arm",
            "B1",
            "--manifest",
            str(manifest_path),
            "--approval",
            str(approval_path),
            "--dry-run",
            "--nonce-ledger",
            str(tmp_path / "ledger.json"),
            "--residue-destination",
            str(tmp_path / "residue"),
            "--trust-owner-public-key",
            trust_owner_public_key_hex,
            "--emit-signing-payload",
            str(payload_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=run_env,
    )
    assert emit_result.returncode == 0, (emit_result.stdout, emit_result.stderr)
    assert json.loads(emit_result.stdout) == {
        "verdict": "signing_payload_emitted",
        "error_count": 0,
        "residue_audit": None,
    }
    assert emit_result.stderr == ""

    # --- Doc step 3 (was step 2): sign the payload with the owner's key,
    # using the real, independent signing function from
    # scripts/sign_voice_bakeoff_approval.py — not a runner-internal
    # shortcut.
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    signature = sign_payload(private_key, domain=_APPROVAL_DOMAIN, payload=payload)

    # --- Doc step 4 (was step 3): embed the signature — and, per the new
    # doc warning, nothing else.
    approval["owner_authorization"]["signature"] = signature.hex()
    _resign(approval)  # No-op on the value: signature is excluded from self_digest.
    approval_path.write_text(json.dumps(approval, separators=(",", ":")), encoding="utf-8")

    # --- Doc step 5: run the runner normally — a real subprocess CLI call,
    # end to end, on the exact documented order.
    run_result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--arm",
            "B1",
            "--manifest",
            str(manifest_path),
            "--approval",
            str(approval_path),
            "--dry-run",
            "--nonce-ledger",
            str(tmp_path / "final_ledger.json"),
            "--residue-destination",
            str(tmp_path / "final_residue"),
            "--trust-owner-public-key",
            trust_owner_public_key_hex,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=run_env,
    )
    assert run_result.returncode == 3, (run_result.stdout, run_result.stderr)
    assert run_result.stderr == ""
    run_payload = json.loads(run_result.stdout)
    assert run_payload["verdict"] == "blocked_external_verification_required"
    assert run_payload["error_count"] == 0
