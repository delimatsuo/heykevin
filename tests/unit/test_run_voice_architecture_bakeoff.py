"""Tests for the fail-closed, dry-run-only approval preflight."""

import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time


_SCRIPT = Path("scripts/run_voice_architecture_bakeoff.py")
_SPEC = importlib.util.spec_from_file_location("bakeoff_runner", _SCRIPT)
assert _SPEC and _SPEC.loader
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


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
        "signatures": [
            {"role": "staff", "identity": "staff_1", "key_id": "key_1", "algorithm": "ed25519", "signature": "detached_1"},
            {"role": "security_privacy", "identity": "security_1", "key_id": "key_2", "algorithm": "ed25519", "signature": "detached_2"},
            {"role": "conversation_product", "identity": "product_1", "key_id": "key_3", "algorithm": "ed25519", "signature": "detached_3"},
        ],
    }
    value["self_digest"] = runner._canonical_digest(value)
    return value


def _manifest(template_only: bool = False) -> dict[str, object]:
    manifest = {
        "authorization_status": "template_only" if template_only else "sealed",
        "environment": "bakeoff",
        "candidate": {"arm": "B1", "source_sha": "a" * 40, "dependency_inventory_digest": "0" * 64},
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
    _resign(approval)
    return approval


def _rebind_manifest(approval: dict[str, object], manifest: dict[str, object]) -> None:
    approval["manifest_digest"] = runner._manifest_digest_bytes(
        __import__("json").dumps(manifest, separators=(",", ":")).encode("utf-8")
    )
    _resign(approval)


def test_valid_shape_still_requires_external_verification():
    manifest = _manifest()
    assert runner.validate(_bound_approval(manifest), manifest, "B1", "a" * 40, now_ms=1_000) == []


def test_rejects_template_wrong_binding_digest_roles_caps_and_risky_features():
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
    approval["signatures"] = [{"role": "staff", "identity": "same"}] * 3
    _resign(approval)
    assert "roles" in runner.validate(approval, manifest, "B1", "a" * 40)[0]
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


def test_rejects_expired_unsigned_and_altered_manifest():
    manifest = _manifest()
    approval = _bound_approval(manifest)
    assert "expired" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=2_000)[0]
    approval = _bound_approval(manifest)
    approval["signatures"][0].pop("signature")
    _resign(approval)
    assert "unsigned" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]
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


def test_rejects_inventory_mismatch_and_mutated_signer_metadata():
    manifest = _manifest()
    approval = _bound_approval(manifest)
    approval["dependency_inventory_digest"] = "d" * 64
    _resign(approval)
    assert "inventory" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]

    approval = _bound_approval(manifest)
    approval["signatures"][0]["key_id"] = "other_key"
    assert "self digest" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]

    approval = _bound_approval(manifest)
    approval["signatures"] = ["invalid"] * 3
    _resign(approval)
    assert "roles" in runner.validate(approval, manifest, "B1", "a" * 40, now_ms=1_000)[0]


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


def test_cli_valid_local_envelope_stops_at_external_verification(tmp_path: Path):
    source_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    manifest = _manifest()
    manifest["candidate"]["source_sha"] = source_sha
    approval = _approval()
    approval["issued_at_ms"] = int(time.time() * 1000)
    approval["expires_at_ms"] = approval["issued_at_ms"] + 60_000
    approval["source_sha"] = source_sha
    approval["dependency_inventory_digest"] = runner._dependency_inventory_digest(approval["dependencies"])
    manifest["candidate"]["dependency_inventory_digest"] = approval["dependency_inventory_digest"]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    approval["manifest_digest"] = runner._manifest_digest_bytes(manifest_path.read_bytes())
    _resign(approval)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(approval, separators=(",", ":")), encoding="utf-8")

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
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3
    assert json.loads(result.stdout) == {
        "error_count": 0,
        "verdict": "blocked_external_verification_required",
    }
    assert result.stderr == ""
