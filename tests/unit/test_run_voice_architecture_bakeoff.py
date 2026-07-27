"""Tests for the fail-closed, dry-run-only approval preflight."""

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from scripts.voice_bakeoff_caller import development_harness_manifest


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
        "authorization_model": "sole_owner",
        "owner_authorization": {
            "role": "owner",
            "identity": "owner_1",
            "key_id": "key_1",
            "algorithm": "ed25519",
            "signature": "detached_1",
        },
        "technical_review": {
            "review_digest": "d" * 64,
            "provenance_ref": "technical_review_1",
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


def test_valid_shape_still_requires_external_verification():
    manifest = _manifest()
    assert runner.validate(_bound_approval(manifest), manifest, "B1", "a" * 40, now_ms=1_000) == []


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
}
_OFFLINE_APPROVED_SOURCE_DIGESTS = {
    "scripts.run_voice_architecture_bakeoff": (
        "12756f6defdcc0c45f88921b3f12221db0ad4d858cc7e637f91b809ce9632272"
    ),
    "scripts.voice_bakeoff_caller": (
        "a4f2aab9e95bd27048dbec60268c109fee3362ee6a2fd6f33f77dc31d1f70c1b"
    ),
}
_OFFLINE_ALLOWED_IMPORTS = {
    "scripts.run_voice_architecture_bakeoff": {
        ("from", "__future__", "annotations", ""),
        ("import", "argparse", "", ""),
        ("import", "hashlib", "", ""),
        ("import", "json", "", ""),
        ("import", "re", "", ""),
        ("import", "subprocess", "", ""),
        ("import", "time", "", ""),
        ("from", "pathlib", "Path", ""),
        (
            "from",
            "scripts.voice_bakeoff_caller",
            "run_offline_self_check",
            "",
        ),
        ("from", "voice_bakeoff_caller", "run_offline_self_check", ""),
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
                if alias.name.startswith("scripts."):
                    local_dependencies.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolved_from_module(module_name, node)
            for alias in node.names:
                records.add(("from", base, alias.name, alias.asname or ""))
            if base == "voice_bakeoff_caller":
                local_dependencies.add("scripts.voice_bakeoff_caller")
            elif base.startswith("scripts."):
                local_dependencies.add(base)
    return records, local_dependencies


def _offline_firewall_errors(
    overrides: dict[str, str] | None = None,
) -> list[str]:
    source_overrides = overrides or {}
    errors: list[str] = []
    package_initializers = {
        path.parent / "__init__.py"
        for path in _OFFLINE_SOURCE_PATHS.values()
        if (path.parent / "__init__.py").exists()
    }
    if package_initializers:
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
    approval["technical_review"]["source_sha"] = source_sha
    approval["dependency_inventory_digest"] = runner._dependency_inventory_digest(approval["dependencies"])
    manifest["candidate"]["dependency_inventory_digest"] = approval["dependency_inventory_digest"]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    approval["manifest_digest"] = runner._manifest_digest_bytes(manifest_path.read_bytes())
    approval["technical_review"]["manifest_digest"] = approval["manifest_digest"]
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
