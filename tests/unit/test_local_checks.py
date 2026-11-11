"""Unit tests and fault probes for scripts/run_local_checks.py and maintenance contracts."""

from __future__ import annotations

import ast
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import tomllib

import pytest

from scripts import run_local_checks
from scripts.run_local_checks import (
    CLASSIFICATION_DOC_REL_PATH,
    MACOS_SANDBOX_PROFILE,
    ContractViolationError,
    EnvironmentContractError,
    SandboxUnavailableError,
    build_pristine_child_env,
    canonicalize_package_name,
    main,
    parse_classification_manifest,
    parse_pep508_requirement,
    probe_os_network_sandbox,
    resolve_python312,
    verify_contracts,
    verify_module_classification_contract,
    verify_pyproject_contract,
    verify_test_partition_contract,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_NON_PREFIXED_OFFLINE_MODULES = frozenset(
    {
        "app/services/caller_observation_extractor.py",
        "app/services/caller_turns.py",
        "app/services/gemini_turn_events.py",
        "app/services/receptionist_replay.py",
        "app/services/urgency_replay.py",
        "app/services/visual_diagnosis_contracts.py",
        "app/services/visual_diagnosis_state.py",
        "app/services/voice_call_lifecycle.py",
        "app/services/voice_lifecycle.py",
        "app/services/voice_session_auth.py",
        "app/services/voice_speech_control.py",
        "app/services/voice_telemetry.py",
        "app/services/voice_turn_replay.py",
    }
)

_FORBIDDEN_ENV_SAMPLE = (
    "ANTHROPIC_API_KEY",
    "ADMIN_API_TOKEN",
    "APNS_KEY_CONTENT",
    "APNS_KEY_ID",
    "APNS_TEAM_ID",
    "API_BEARER_TOKEN",
    "APPSTORE_ISSUER_ID",
    "APPSTORE_KEY_ID",
    "APPSTORE_PRIVATE_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "FISH_AUDIO_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CALENDAR_CLIENT_ID",
    "GOOGLE_CALENDAR_CLIENT_SECRET",
    "INTEGRATION_TOKEN_ACTIVE_KEY_VERSION",
    "INTEGRATION_TOKEN_ENCRYPTION_KEYS",
    "JOBBER_CLIENT_ID",
    "JOBBER_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "RECEPTIONIST_OBSERVATION_SHADOW_CALLER_HMAC_KEY",
    "TRANSCRIPT_ENCRYPTION_KEY",
    "TWILIO_ACCOUNT_SID",
    "PRODUCTION_TWILIO_ACCOUNT_SID",
    "TWILIO_API_KEY_SECRET",
    "TWILIO_API_KEY_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_TWIML_APP_SID",
    "VAPI_API_KEY",
    "VAPI_PUBLIC_KEY",
    "VAPI_WEBHOOK_SECRET",
    "VCARD_HMAC_SECRET",
    "APNS_BUNDLE_ID",
    "APPSTORE_BUNDLE_ID",
    "CLOUD_RUN_URL",
    "DIAL_IN_NUMBER",
    "DIAL_IN_NUMBERS",
    "FIREBASE_DATABASE_URL",
    "FIRESTORE_PROJECT_ID",
    "GCLOUD_PROJECT",
    "GCP_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
    "TELEGRAM_CHAT_ID",
    "TWILIO_PHONE_NUMBER",
    "USER_NAME",
    "USER_PHONE",
    "VAPI_PHONE_NUMBER_ID",
)


def _write_manifest(repo_root: Path, rows: list[tuple[str, str, str, str]]) -> Path:
    docs_dir = repo_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Synthetic Module Manifest",
        "",
        "| Module Path | Category | Reachability | Notes |",
        "| --- | --- | --- | --- |",
    ]
    for mod_path, cat, reach, note in rows:
        lines.append(f"| {mod_path} | {cat} | {reach} | {note} |")
    manifest = docs_dir / "backend-module-classification.md"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def _build_synthetic_repo(tmp_path: Path, *, with_git: bool = True) -> Path:
    repo = tmp_path / "mini_repo"
    (repo / "app" / "services").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)

    (repo / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "mini-kevin"',
                'version = "0.1.0"',
                'requires-python = ">=3.11"',
                "dependencies = [",
                '    "httpx[http2]==0.28.1",',
                '    "fastapi==0.104.1",',
                "]",
                "",
                "[project.optional-dependencies]",
                "dev = [",
                '    "pytest>=8.3.0",',
                '    "ruff>=0.8.0",',
                "]",
                "",
                "[tool.ruff]",
                'target-version = "py312"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "services" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "services" / "shared.py").write_text(
        "VALUE = 42\n", encoding="utf-8"
    )
    (repo / "app" / "services" / "shim.py").write_text(
        "from app.services.shared import VALUE\nSHIM_VALUE = VALUE\n",
        encoding="utf-8",
    )
    (repo / "app" / "services" / "feature_gate.py").write_text(
        "ENABLED = False\n", encoding="utf-8"
    )
    (repo / "app" / "services" / "offline_eval.py").write_text(
        "from app.services.shared import VALUE\nEVAL_VALUE = VALUE + 1\n",
        encoding="utf-8",
    )
    (repo / "app" / "services" / "demo_only.py").write_text(
        "DEMO_MODE = True\n", encoding="utf-8"
    )
    (repo / "app" / "services" / "dormant_audit.py").write_text(
        "AUDIT_SCHEMA = 1\n", encoding="utf-8"
    )
    (repo / "app" / "services" / "legacy_v1.py").write_text(
        "LEGACY_V1 = True\n", encoding="utf-8"
    )
    (repo / "app" / "main.py").write_text(
        "\n".join(
            [
                "from app.services import feature_gate, shared, shim",
                "",
                "LIVE = (shared.VALUE, shim.SHIM_VALUE, feature_gate.ENABLED)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / "app" / "worker_main.py").write_text(
        "from app.services.shared import VALUE\nWORKER = VALUE\n",
        encoding="utf-8",
    )
    (repo / "app" / "worker_runner.py").write_text(
        "from app.services.shared import VALUE\nRUNNER = VALUE\n",
        encoding="utf-8",
    )
    (repo / "app" / "public_demo_main.py").write_text(
        "from app.services import demo_only, shared\nDEMO = (demo_only.DEMO_MODE, shared.VALUE)\n",
        encoding="utf-8",
    )
    (repo / "app" / "public_demo_breaker_main.py").write_text(
        "from app.services import demo_only\nBREAKER = demo_only.DEMO_MODE\n",
        encoding="utf-8",
    )

    _write_manifest(
        repo,
        [
            ("app/__init__.py", "a", "live_or_worker", "Package marker."),
            ("app/main.py", "a", "live_or_worker", "Live entrypoint."),
            (
                "app/public_demo_breaker_main.py",
                "d",
                "isolated_demo_entrypoint",
                "Isolated demo breaker.",
            ),
            (
                "app/public_demo_main.py",
                "d",
                "isolated_demo_entrypoint",
                "Isolated public demo.",
            ),
            ("app/services/__init__.py", "a", "live_or_worker", "Services marker."),
            (
                "app/services/demo_only.py",
                "d",
                "isolated_demo_entrypoint",
                "Demo helper.",
            ),
            ("app/services/dormant_audit.py", "d", "unmounted", "Unmounted audit."),
            ("app/services/feature_gate.py", "d", "live_or_worker", "Feature gate."),
            ("app/services/legacy_v1.py", "b", "unmounted", "Unmounted legacy."),
            ("app/services/offline_eval.py", "c", "unmounted", "Offline eval."),
            ("app/services/shared.py", "a", "live_or_worker", "Shared core."),
            ("app/services/shim.py", "b", "live_or_worker", "Live compat shim."),
            ("app/worker_main.py", "a", "live_or_worker", "Worker HTTP."),
            ("app/worker_runner.py", "a", "live_or_worker", "Worker runner."),
        ],
    )

    (repo / "scripts" / "helper_tool.py").write_text(
        "def noop() -> int:\n    return 0\n", encoding="utf-8"
    )

    # Top-level test + 6 unit tests => 7 test files covering all 7 shards.
    (repo / "tests" / "test_top_level.py").write_text(
        "from app.services.shared import VALUE\n\n\ndef test_top() -> None:\n    assert VALUE == 42\n",
        encoding="utf-8",
    )
    for idx in range(1, 7):
        (repo / "tests" / "unit" / f"test_shard_{idx}.py").write_text(
            f"def test_ok_{idx}() -> None:\n    assert {idx} > 0\n",
            encoding="utf-8",
        )

    if with_git:
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    return repo


# ---------------------------------------------------------------------------
# Group 1: Live pyproject.toml Uniqueness & Contract Verification
# ---------------------------------------------------------------------------


def test_live_pyproject_toml_uniqueness_and_httpx_deduplicated() -> None:
    pyproject_path = REPO_ROOT / "pyproject.toml"
    parsed_runtime = verify_pyproject_contract(pyproject_path)

    assert "httpx" in parsed_runtime
    httpx_req = parsed_runtime["httpx"]
    assert httpx_req.extras == ("http2",)
    assert httpx_req.specifier == "==0.28.1"

    raw_toml = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    assert raw_toml["project"]["requires-python"] == ">=3.11"
    raw_deps = raw_toml["project"]["dependencies"]
    assert "httpx==0.28.1" not in raw_deps
    assert "httpx[http2]==0.28.1" in raw_deps


def test_live_verify_contracts_only_cli_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--repo-root", str(REPO_ROOT), "--verify-contracts-only"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "[contracts-ok]" in captured.out


# ---------------------------------------------------------------------------
# Group 2: Synthetic pyproject.toml Duplicate & PEP 508 Specifier Fault Probes
# ---------------------------------------------------------------------------


def test_pyproject_rejects_duplicate_runtime_and_pep503_normalized_names(
    tmp_path: Path,
) -> None:
    assert canonicalize_package_name("Pydantic_Settings") == "pydantic-settings"
    assert canonicalize_package_name("google..cloud__tasks") == "google-cloud-tasks"

    dup_httpx = tmp_path / "dup_httpx.toml"
    dup_httpx.write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\ndependencies = [\n'
        '  "httpx==0.28.1",\n  "httpx[http2]==0.28.1",\n]\n',
        encoding="utf-8",
    )
    with pytest.raises(ContractViolationError, match="Duplicate canonical dependency 'httpx'"):
        verify_pyproject_contract(dup_httpx)

    dup_normalized = tmp_path / "dup_norm.toml"
    dup_normalized.write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\ndependencies = [\n'
        '  "Pydantic_Settings==2.11.0",\n  "pydantic.settings>=2.0.0",\n]\n',
        encoding="utf-8",
    )
    with pytest.raises(
        ContractViolationError, match="Duplicate canonical dependency 'pydantic-settings'"
    ):
        verify_pyproject_contract(dup_normalized)


def test_pyproject_rejects_optional_group_duplicates_and_cross_group_redeclarations(
    tmp_path: Path,
) -> None:
    dup_opt = tmp_path / "dup_opt.toml"
    dup_opt.write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\ndependencies = ["fastapi==0.104.1"]\n'
        '[project.optional-dependencies]\ndev = ["pytest-asyncio>=0.24.0", "PyTest_Asyncio>=0.23.0"]\n',
        encoding="utf-8",
    )
    with pytest.raises(
        ContractViolationError, match="Duplicate canonical dependency 'pytest-asyncio'"
    ):
        verify_pyproject_contract(dup_opt)

    cross_runtime = tmp_path / "cross_runtime.toml"
    cross_runtime.write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\ndependencies = ["httpx[http2]==0.28.1"]\n'
        '[project.optional-dependencies]\ndev = ["HTTPX>=0.28.0"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ContractViolationError, match="redundantly redeclares runtime dependency"):
        verify_pyproject_contract(cross_runtime)

    cross_optional_conflict = tmp_path / "cross_opt_conflict.toml"
    cross_optional_conflict.write_text(
        '[project]\nname = "x"\nversion = "0.1.0"\ndependencies = ["fastapi==0.104.1"]\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8.3.0"]\nlint = ["pytest==7.4.0"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ContractViolationError, match="Conflicting optional dependency 'pytest'"):
        verify_pyproject_contract(cross_optional_conflict)


@pytest.mark.parametrize(
    "bad_spec",
    [
        "",
        "   ",
        "-bad==1.0.0",
        "bad-==1.0.0",
        "==1.0.0",
        "httpx=0.28.1",
        "httpx>>0.28.1",
        "httpx --invalid-- 0.28.1",
        "httpx[http2==0.28.1",
        "httpx[http2,]==0.28.1",
        "httpx[,http2]==0.28.1",
        "httpx>=1.0.0,",
        "httpx ()",
        "httpx (>=1.0.0",
        "httpx >=1.0.0)",
        "httpx~=1",
        "httpx>=1.0.*",
        "httpx @ not-a-valid-uri",
        "httpx==0.28.1 @ https://example.com/httpx.whl",
        "httpx==0.28.1;",
        "httpx==0.28.1; unknown_marker == '3.12'",
        "httpx==0.28.1; '3.12' == '3.12'",
        "httpx\n==0.28.1",
    ],
)
def test_pep508_parser_rejects_malformed_specifiers(bad_spec: str) -> None:
    with pytest.raises(ContractViolationError):
        parse_pep508_requirement(bad_spec)


def test_pep508_parser_accepts_extras_markers_parens_and_urls() -> None:
    req = parse_pep508_requirement(
        'PyJWT[crypto, extra_two] (>=2.10.0, !=2.10.2.*); '
        '(python_version >= "3.11" and sys_platform != "win32") or "arm" in platform_machine'
    )
    assert req.canonical_name == "pyjwt"
    assert req.extras == ("crypto", "extra-two")
    assert req.specifier == ">=2.10.0,!=2.10.2.*"
    assert req.marker is not None

    url_req = parse_pep508_requirement(
        "custom_pkg @ https://example.com/custom_pkg-1.0.0-py3-none-any.whl ; os_name == 'posix'"
    )
    assert url_req.canonical_name == "custom-pkg"
    assert url_req.url == "https://example.com/custom_pkg-1.0.0-py3-none-any.whl"


# ---------------------------------------------------------------------------
# Group 3: Live 196-Module Classification & Closure Verification
# ---------------------------------------------------------------------------


def test_live_module_classification_bijection_and_closures() -> None:
    report = verify_module_classification_contract(REPO_ROOT)
    disk_py = {
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "app").rglob("*.py")
    }
    assert set(report.records.keys()) == disk_py
    assert len(report.records) == 196

    # Newly added transport module and package root are live_or_worker in (a).
    for live_mod in (
        "app/__init__.py",
        "app/main.py",
        "app/worker_main.py",
        "app/worker_runner.py",
        "app/webhooks/stream_transport.py",
        "app/services/dialogue_planner.py",
        "app/services/receptionist_state.py",
    ):
        rec = report.records[live_mod]
        assert (rec.category, rec.reachability) == ("a", "live_or_worker")
        assert live_mod in report.live_closure

    # Live migration shim in (b).
    shim_rec = report.records["app/services/legacy_call_commands.py"]
    assert (shim_rec.category, shim_rec.reachability) == ("b", "live_or_worker")
    assert "app/services/legacy_call_commands.py" in report.live_closure

    # All 13 non-bakeoff-prefixed offline modules + total 40 category (c) modules.
    cat_c = {k for k, v in report.records.items() if v.category == "c"}
    assert len(cat_c) == 40
    assert _NON_PREFIXED_OFFLINE_MODULES <= cat_c
    assert cat_c.isdisjoint(report.live_closure)
    assert cat_c.isdisjoint(report.demo_only_closure)
    assert all(report.records[m].reachability == "unmounted" for m in cat_c)

    # Isolated public demo closure has exact 7 category (d) modules.
    assert len(report.demo_only_closure) == 7
    assert "app/public_demo_main.py" in report.demo_only_closure
    assert "app/public_demo_breaker_main.py" in report.demo_only_closure
    assert all(
        (report.records[m].category, report.records[m].reachability)
        == ("d", "isolated_demo_entrypoint")
        for m in report.demo_only_closure
    )

    doc_text = (REPO_ROOT / CLASSIFICATION_DOC_REL_PATH).read_text(encoding="utf-8")
    assert 'requires-python = ">=3.11"' in doc_text
    assert "Python 3.12" in doc_text
    assert "Module Retirement" in doc_text


# ---------------------------------------------------------------------------
# Group 4: Synthetic tmp_path Repo AST / Reachability / Symlink Fault Probes
# ---------------------------------------------------------------------------


def test_synthetic_repo_passes_and_rejects_unclassified_stale_duplicate_or_invalid_rows(
    tmp_path: Path,
) -> None:
    repo = _build_synthetic_repo(tmp_path, with_git=False)
    report = verify_contracts(repo)
    assert "app/services/shared.py" in report.live_closure
    assert "app/services/demo_only.py" in report.demo_only_closure
    assert "app/services/offline_eval.py" in report.unmounted_modules

    # Unclassified file on disk
    extra_py = repo / "app" / "services" / "rogue.py"
    extra_py.write_text("ROGUE = 1\n", encoding="utf-8")
    with pytest.raises(ContractViolationError, match="unclassified on disk"):
        verify_module_classification_contract(repo)
    extra_py.unlink()

    # Stale row missing on disk
    manifest_path = repo / CLASSIFICATION_DOC_REL_PATH
    orig_manifest = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        orig_manifest + "| app/services/ghost.py | b | unmounted | Missing. |\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractViolationError, match="missing from disk"):
        verify_module_classification_contract(repo)

    # Duplicate row
    manifest_path.write_text(
        orig_manifest + "| app/main.py | a | live_or_worker | Dup. |\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractViolationError, match="Duplicate module path"):
        parse_classification_manifest(manifest_path)

    # Invalid category
    manifest_path.write_text(
        orig_manifest.replace("| app/services/legacy_v1.py | b |", "| app/services/legacy_v1.py | z |"),
        encoding="utf-8",
    )
    with pytest.raises(ContractViolationError, match="Invalid category"):
        parse_classification_manifest(manifest_path)

    # Invalid reachability
    manifest_path.write_text(
        orig_manifest.replace("unmounted", "floating", 1),
        encoding="utf-8",
    )
    with pytest.raises(ContractViolationError, match="Invalid reachability"):
        parse_classification_manifest(manifest_path)

    manifest_path.write_text(orig_manifest, encoding="utf-8")
    verify_module_classification_contract(repo)


def test_synthetic_repo_rejects_symlinks_and_dynamic_imports(tmp_path: Path) -> None:
    repo = _build_synthetic_repo(tmp_path, with_git=False)

    outside = tmp_path / "outside.py"
    outside.write_text("X = 1\n", encoding="utf-8")
    sym_file = repo / "app" / "services" / "sym.py"
    sym_file.symlink_to(outside)
    with pytest.raises(ContractViolationError, match="Symlink file under app/ rejected"):
        verify_module_classification_contract(repo)
    sym_file.unlink()

    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    sym_dir = repo / "app" / "sym_pkg"
    sym_dir.symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(ContractViolationError, match="Symlink directory under app/ rejected"):
        verify_module_classification_contract(repo)
    sym_dir.unlink()

    # Dynamic import inside app/ fails closed
    shim_file = repo / "app" / "services" / "shim.py"
    orig_shim = shim_file.read_text(encoding="utf-8")
    shim_file.write_text(
        "from importlib import import_module as dyn_load\n"
        "SHIM_VALUE = dyn_load('math')\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractViolationError, match="Dynamic import call"):
        verify_module_classification_contract(repo)
    shim_file.write_text(orig_shim, encoding="utf-8")


def test_synthetic_repo_enforces_two_way_ast_firewall_and_closure_tags(
    tmp_path: Path,
) -> None:
    repo = _build_synthetic_repo(tmp_path, with_git=False)
    main_file = repo / "app" / "main.py"
    shim_file = repo / "app" / "services" / "shim.py"
    demo_file = repo / "app" / "services" / "demo_only.py"
    orig_main = main_file.read_text(encoding="utf-8")
    orig_shim = shim_file.read_text(encoding="utf-8")
    orig_demo = demo_file.read_text(encoding="utf-8")

    # 1. Category (a) importing (c) via absolute import
    main_file.write_text(
        orig_main + "import app.services.offline_eval\n", encoding="utf-8"
    )
    with pytest.raises(ContractViolationError, match="Forbidden AST import"):
        verify_module_classification_contract(repo)
    main_file.write_text(orig_main, encoding="utf-8")

    # 2. Category (b) importing (c) via relative import
    shim_file.write_text(orig_shim + "from . import offline_eval\n", encoding="utf-8")
    with pytest.raises(ContractViolationError, match="Forbidden AST import"):
        verify_module_classification_contract(repo)
    shim_file.write_text(orig_shim, encoding="utf-8")

    # 3. Category (d) importing (c) via parent subpackage __init__.py chain
    exp_dir = repo / "app" / "exp_pkg"
    exp_dir.mkdir()
    (exp_dir / "__init__.py").write_text("MARKER = 1\n", encoding="utf-8")
    (exp_dir / "leaf.py").write_text("LEAF = 2\n", encoding="utf-8")
    manifest_path = repo / CLASSIFICATION_DOC_REL_PATH
    base_manifest = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        base_manifest
        + "| app/exp_pkg/__init__.py | c | unmounted | Offline pkg. |\n"
        + "| app/exp_pkg/leaf.py | d | unmounted | Misplaced leaf. |\n",
        encoding="utf-8",
    )
    # Simply existing under a category 'c' package means leaf.py (d) loads exp_pkg/__init__.py (c)!
    with pytest.raises(ContractViolationError, match="Forbidden AST import"):
        verify_module_classification_contract(repo)
    (exp_dir / "leaf.py").unlink()
    (exp_dir / "__init__.py").unlink()
    exp_dir.rmdir()
    manifest_path.write_text(base_manifest, encoding="utf-8")

    # 4. Unreachable category (a) module fails closed
    (repo / "app" / "services" / "dead_live.py").write_text("DEAD = 1\n", encoding="utf-8")
    manifest_path.write_text(
        base_manifest
        + "| app/services/dead_live.py | a | live_or_worker | Unwired a. |\n",
        encoding="utf-8",
    )
    with pytest.raises(
        ContractViolationError, match="unreachable from live/worker entrypoints"
    ):
        verify_module_classification_contract(repo)
    (repo / "app" / "services" / "dead_live.py").unlink()
    manifest_path.write_text(base_manifest, encoding="utf-8")

    # 5. Live-imported module falsely marked 'unmounted' fails closed
    main_file.write_text(
        orig_main + "from app.services import dormant_audit\n", encoding="utf-8"
    )
    with pytest.raises(ContractViolationError, match="live_or_worker reachability mismatch"):
        verify_module_classification_contract(repo)
    main_file.write_text(orig_main, encoding="utf-8")

    # 6. Demo-only closure module with wrong category or wrong reachability fails closed
    manifest_path.write_text(
        base_manifest.replace(
            "| app/services/demo_only.py | d | isolated_demo_entrypoint |",
            "| app/services/demo_only.py | b | unmounted |",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ContractViolationError, match="Demo-only closure contains non-'d'"):
        verify_module_classification_contract(repo)
    manifest_path.write_text(base_manifest, encoding="utf-8")
    demo_file.write_text(orig_demo, encoding="utf-8")
    verify_module_classification_contract(repo)


# ---------------------------------------------------------------------------
# Group 5: Static Isolation & Repository Hygiene Firewalls (C1, C7)
# ---------------------------------------------------------------------------


def test_scripts_namespace_and_static_isolation_hygiene() -> None:
    assert not (REPO_ROOT / "scripts" / "__init__.py").exists()

    runner_path = REPO_ROOT / "scripts" / "run_local_checks.py"
    runner_source = runner_path.read_text(encoding="utf-8")

    forbidden_bakeoff_names = {
        "voice_bakeoff_" + "control_admission_projection",
        "voice_bakeoff_" + "control_store_assembly",
        "voice_bakeoff_" + "firestore_transaction_port",
        "voice_bakeoff_" + "google_firestore_runner",
        "voice_bakeoff_" + "control_composition",
    }
    forbidden_token_fields = {
        "jobber_" + "access_token",
        "jobber_" + "refresh_token",
        "google_calendar_" + "access_token",
        "google_calendar_" + "refresh_token",
    }
    for term in forbidden_bakeoff_names | forbidden_token_fields:
        assert term not in runner_source, f"Forbidden literal {term!r} in run_local_checks.py"

    tree = ast.parse(runner_source, filename="scripts/run_local_checks.py")
    forbidden_calls = {"__import__", "import_module", "find_spec", "reload"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_calls
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] != "packaging"
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] != "packaging"

    # Public-repository path & provenance hygiene across all Slice 6 authored files
    forbidden_provenance = (
        "/" + "Volumes" + "/",
        "/" + "Users" + "/",
        "." + "super" + "vision",
        "HAND" + "OFF",
    )
    uuid_re = re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    )
    checked_files = (
        REPO_ROOT / CLASSIFICATION_DOC_REL_PATH,
        REPO_ROOT / "scripts" / "run_local_checks.py",
        Path(__file__).resolve(),
    )
    for fpath in checked_files:
        content = fpath.read_text(encoding="utf-8")
        for marker in forbidden_provenance:
            assert marker not in content, f"Forbidden marker in {fpath.name}"
        assert uuid_re.search(content) is None, f"UUID literal found in {fpath.name}"


# ---------------------------------------------------------------------------
# Group 6: Pristine Environment Scrubbing & Python 3.12 Resolution (C4)
# ---------------------------------------------------------------------------


def test_build_pristine_child_env_scrubs_all_credentials_and_prepends_venv(
    tmp_path: Path,
) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)

    dirty_env: dict[str, str] = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp/fake-home",
        "LANG": "en_US.UTF-8",
        "PYTHONPATH": "/tmp/injected",
        "PYTHONSTARTUP": "/tmp/startup.py",
        "VIRTUAL_ENV": "/tmp/other-venv",
        "_VISUAL_DIAG_PRISTINE_ENVIRON_NAMES": "TWILIO_ACCOUNT_SID\x1fGEMINI_API_KEY",
        "VISUAL_DIAG_LOCAL_ISOLATION_CHECK": "1",
        "BAKEOFF_NONPROD_CREDENTIAL__TWILIO": "secret-1",
        "bakeoff_nonprod_account_region__us": "us-east1",
    }
    for key in _FORBIDDEN_ENV_SAMPLE:
        dirty_env[key] = "leaked-upper"
        dirty_env[key.lower()] = "leaked-lower"

    clean = build_pristine_child_env(tmp_path, base_env=dirty_env)

    assert clean["KEVIN_DISABLE_DOTENV"] == "1"
    assert clean["PYTHONDONTWRITEBYTECODE"] == "1"
    assert clean["PYTHONNOUSERSITE"] == "1"
    assert clean["HOME"] == "/tmp/fake-home"
    assert clean["LANG"] == "en_US.UTF-8"
    assert clean["PATH"].split(os.pathsep)[0] == os.path.abspath(str(venv_bin))

    allowed_total = set(run_local_checks.ALLOWED_SYSTEM_ENV_KEYS) | {
        "KEVIN_DISABLE_DOTENV",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
    }
    assert set(clean.keys()) <= allowed_total
    lowered_keys = {k.casefold() for k in clean}
    assert lowered_keys.isdisjoint({k.casefold() for k in _FORBIDDEN_ENV_SAMPLE})


def test_python312_resolution_prefers_venv_on_fallback_and_fails_closed(
    tmp_path: Path,
) -> None:
    def make_stub_python(path: Path, version_out: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"#!/bin/sh\nprintf '%s' '{version_out}'\n", encoding="utf-8"
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    py311 = make_stub_python(tmp_path / "bin" / "python3.11", "3.11")
    venv_py312 = make_stub_python(tmp_path / ".venv" / "bin" / "python", "3.12")

    resolved = resolve_python312(tmp_path, current_executable=py311)
    assert resolved == os.path.abspath(str(venv_py312))

    # When both current and .venv are non-3.12, fail closed with actionable error.
    make_stub_python(venv_py312, "3.11")
    with pytest.raises(EnvironmentContractError, match="Python 3.12 is required"):
        resolve_python312(tmp_path, current_executable=py311)


def test_child_pytest_receives_scrubbed_env_and_ignores_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _build_synthetic_repo(tmp_path, with_git=False)
    (repo / ".env").write_text("TWILIO_ACCOUNT_SID=from-dotenv-file\n", encoding="utf-8")
    env_test = repo / "tests" / "unit" / "test_env_probe.py"
    env_test.write_text(
        "\n".join(
            [
                "import os",
                "",
                "def test_child_env_is_clean() -> None:",
                '    assert os.environ.get("KEVIN_DISABLE_DOTENV") == "1"',
                '    assert os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"',
                '    assert os.environ.get("PYTHONNOUSERSITE") == "1"',
                '    assert "TWILIO_ACCOUNT_SID" not in os.environ',
                '    assert "GEMINI_API_KEY" not in os.environ',
                '    assert "_VISUAL_DIAG_PRISTINE_ENVIRON_NAMES" not in os.environ',
                '    assert "VISUAL_DIAG_LOCAL_ISOLATION_CHECK" not in os.environ',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "leaked-parent-sid")
    monkeypatch.setenv("GEMINI_API_KEY", "leaked-parent-gemini")
    monkeypatch.setenv("VISUAL_DIAG_LOCAL_ISOLATION_CHECK", "1")
    monkeypatch.setenv("_VISUAL_DIAG_PRISTINE_ENVIRON_NAMES", "TWILIO_ACCOUNT_SID")

    rc = main(["--repo-root", str(repo), "--mode", "fast", "tests/unit/test_env_probe.py"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Group 7: Two-Layer Offline Network Guard -- Socket Layer (C5)
# ---------------------------------------------------------------------------


def test_offline_socket_guard_blocks_inet_allows_unix_and_monkeypatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _build_synthetic_repo(tmp_path, with_git=False)
    net_test = repo / "tests" / "unit" / "test_network_guard_probe.py"
    net_test.write_text(
        "\n".join(
            [
                "import asyncio",
                "import errno",
                "import socket",
                "import pytest",
                "",
                "def test_inet_and_dns_blocked() -> None:",
                "    s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
                "    try:",
                "        with pytest.raises(PermissionError) as exc_info:",
                "            s4.connect(('127.0.0.1', 80))",
                "        assert exc_info.value.errno == errno.EPERM",
                "        with pytest.raises(PermissionError) as exc_ex:",
                "            s4.connect_ex(('127.0.0.1', 80))",
                "        assert exc_ex.value.errno == errno.EPERM",
                "    finally:",
                "        s4.close()",
                "",
                "    u4 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)",
                "    try:",
                "        with pytest.raises(PermissionError) as exc_udp:",
                "            u4.sendto(b'ping', ('127.0.0.1', 53))",
                "        assert exc_udp.value.errno == errno.EPERM",
                "    finally:",
                "        u4.close()",
                "",
                "    if socket.has_ipv6:",
                "        s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)",
                "        try:",
                "            with pytest.raises(PermissionError) as exc6:",
                "                s6.connect(('::1', 80))",
                "            assert exc6.value.errno == errno.EPERM",
                "        finally:",
                "            s6.close()",
                "",
                "    with pytest.raises(PermissionError) as exc_cc:",
                "        socket.create_connection(('1.1.1.1', 443), timeout=0.2)",
                "    assert exc_cc.value.errno == errno.EPERM",
                "",
                "    with pytest.raises(PermissionError) as exc_dns:",
                "        socket.getaddrinfo('example.invalid', 443)",
                "    assert exc_dns.value.errno == errno.EPERM",
                "",
                "def test_numeric_getaddrinfo_unix_socketpair_and_asyncio_allowed() -> None:",
                "    info4 = socket.getaddrinfo('127.0.0.1', 80, socket.AF_INET, socket.SOCK_STREAM)",
                "    assert info4[0][4][0] == '127.0.0.1'",
                "    a, b = socket.socketpair()",
                "    try:",
                "        a.send(b'ok')",
                "        assert b.recv(2) == b'ok'",
                "    finally:",
                "        a.close()",
                "        b.close()",
                "    asyncio.run(asyncio.sleep(0))",
                "",
                "def test_monkeypatch_getaddrinfo_supported(monkeypatch: pytest.MonkeyPatch) -> None:",
                "    def fake_resolver(host, port, family=0, type=0, proto=0, flags=0):",
                "        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]",
                "    monkeypatch.setattr(socket, 'getaddrinfo', fake_resolver)",
                "    res = socket.getaddrinfo('example.com', 443)",
                "    assert res[0][4][0] == '93.184.216.34'",
                "",
                "def test_guard_restored_after_monkeypatch() -> None:",
                "    with pytest.raises(PermissionError):",
                "        socket.getaddrinfo('example.com', 443)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    temp_root = Path(tempfile.gettempdir())
    before_dirs = {p.name for p in temp_root.glob("kevin-local-checks-*")}

    # Force fallback mode first so the Python socket guard is tested directly without OS sandbox masking.
    monkeypatch.setattr(
        run_local_checks,
        "probe_os_network_sandbox",
        lambda **kwargs: run_local_checks.SandboxResolution(
            prefix=(), mode="python-socket-guard-fallback", reason="forced-test"
        ),
    )
    rc = main(
        [
            "--repo-root",
            str(repo),
            "--mode",
            "fast",
            "tests/unit/test_network_guard_probe.py",
        ]
    )
    assert rc == 0

    after_dirs = {p.name for p in temp_root.glob("kevin-local-checks-*")}
    assert after_dirs - before_dirs == set()


# ---------------------------------------------------------------------------
# Group 8: macOS sandbox-exec Probe / Nested Fallback / Require Modes (C5)
# ---------------------------------------------------------------------------


def test_probe_os_network_sandbox_auto_fallback_and_require(tmp_path: Path) -> None:
    fake_sandbox = tmp_path / "sandbox-exec"
    fake_sandbox.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sandbox.chmod(fake_sandbox.stat().st_mode | stat.S_IXUSR)

    # 1. macOS permitted -> wraps with sandbox-exec
    buf_ok = io.StringIO()
    res_ok = probe_os_network_sandbox(
        require_os_sandbox=False,
        platform_name="darwin",
        sandbox_bin=fake_sandbox,
        runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""),
        status_stream=buf_ok,
    )
    assert res_ok.prefix == (str(fake_sandbox), "-p", MACOS_SANDBOX_PROFILE)
    assert res_ok.mode == "sandbox-exec+python-socket-guard"
    assert "active" in buf_ok.getvalue()

    # 2. macOS nested EPERM -> clean fallback when require_os_sandbox=False
    buf_eperm = io.StringIO()
    res_eperm = probe_os_network_sandbox(
        require_os_sandbox=False,
        platform_name="darwin",
        sandbox_bin=fake_sandbox,
        runner=lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 71, "", "sandbox-exec: sandbox_apply: Operation not permitted"
        ),
        status_stream=buf_eperm,
    )
    assert res_eperm.prefix == ()
    assert res_eperm.mode == "python-socket-guard-fallback"
    assert "Operation not permitted" in buf_eperm.getvalue()

    # 3. macOS nested EPERM + --require-os-sandbox -> fails closed
    with pytest.raises(SandboxUnavailableError, match="Operation not permitted"):
        probe_os_network_sandbox(
            require_os_sandbox=True,
            platform_name="darwin",
            sandbox_bin=fake_sandbox,
            runner=lambda cmd, **kw: subprocess.CompletedProcess(
                cmd, 71, "", "sandbox-exec: sandbox_apply: Operation not permitted"
            ),
            status_stream=io.StringIO(),
        )

    # 4. Linux -> clean fallback unless --require-os-sandbox
    buf_linux = io.StringIO()
    res_linux = probe_os_network_sandbox(
        require_os_sandbox=False,
        platform_name="linux",
        sandbox_bin=fake_sandbox,
        status_stream=buf_linux,
    )
    assert res_linux.prefix == ()
    assert res_linux.mode == "python-socket-guard-only"

    with pytest.raises(SandboxUnavailableError, match="unsupported on 'linux'"):
        probe_os_network_sandbox(
            require_os_sandbox=True,
            platform_name="linux",
            sandbox_bin=fake_sandbox,
            status_stream=io.StringIO(),
        )


# ---------------------------------------------------------------------------
# Group 9: CLI --mode fast & --mode ci-parity Against Synthetic tmp_path Repo (C6)
# ---------------------------------------------------------------------------


def test_cli_fast_mode_and_argument_validation_on_synthetic_repo(
    tmp_path: Path,
) -> None:
    repo = _build_synthetic_repo(tmp_path, with_git=True)

    # Fast mode without targets runs contract checks + ruff
    assert main(["--repo-root", str(repo), "--mode", "fast"]) == 0

    # Fast mode with explicit file target and node selector
    assert (
        main(
            [
                "--repo-root",
                str(repo),
                "--mode",
                "fast",
                "tests/test_top_level.py::test_top",
            ]
        )
        == 0
    )

    # Invalid flag combinations and escaping targets fail closed with exit code 2
    assert main(["--repo-root", str(repo), "--mode", "fast", "--shard", "1"]) == 2
    assert (
        main(
            [
                "--repo-root",
                str(repo),
                "--verify-contracts-only",
                "tests/test_top_level.py",
            ]
        )
        == 2
    )
    assert main(["--repo-root", str(repo), "--mode", "fast", "app/main.py"]) == 2
    assert (
        main(["--repo-root", str(repo), "--mode", "fast", "tests/../app/main.py"]) == 2
    )
    assert (
        main(["--repo-root", str(repo), "--mode", "fast", "tests/unit/missing.py"]) == 2
    )


def test_cli_ci_parity_mode_shards_strict_xfail_and_gates_on_synthetic_repo(
    tmp_path: Path,
) -> None:
    repo = _build_synthetic_repo(tmp_path, with_git=True)

    shard_map = verify_test_partition_contract(repo)
    assert set(shard_map.keys()) == {1, 2, 3, 4, 5, 6, 7}
    union_paths = {p for paths in shard_map.values() for p in paths}
    assert "tests/test_top_level.py" in union_paths
    assert len(union_paths) == 7

    # Single-shard and full 7-shard ci-parity execution on synthetic mini-repo
    assert main(["--repo-root", str(repo), "--mode", "ci-parity", "--shard", "1"]) == 0
    assert main(["--repo-root", str(repo), "--mode", "ci-parity"]) == 0

    # Strict xfail XPASS must fail closed (proving --runxfail is NOT passed)
    shard1_target = repo / shard_map[1][0]
    orig_shard1 = shard1_target.read_text(encoding="utf-8")
    shard1_target.write_text(
        "import pytest\n\n@pytest.mark.xfail(strict=True)\ndef test_unexpected_pass() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    assert main(["--repo-root", str(repo), "--mode", "ci-parity", "--shard", "1"]) != 0
    shard1_target.write_text(orig_shard1, encoding="utf-8")

    # Fatal Ruff selector (F821 undefined name) fails ci-parity
    helper_script = repo / "scripts" / "helper_tool.py"
    orig_helper = helper_script.read_text(encoding="utf-8")
    helper_script.write_text("def broken() -> int:\n    return undefined_symbol_xyz\n", encoding="utf-8")
    assert main(["--repo-root", str(repo), "--mode", "ci-parity", "--shard", "1"]) != 0
    helper_script.write_text(orig_helper, encoding="utf-8")

    # git diff --check catches trailing whitespace against indexed blob
    blob = subprocess.check_output(
        ["git", "hash-object", "-w", "scripts/helper_tool.py"],
        cwd=repo,
        text=True,
    ).strip()
    subprocess.run(
        [
            "git",
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{blob},scripts/helper_tool.py",
        ],
        cwd=repo,
        check=True,
    )
    helper_script.write_text("def noop() -> int:   \n    return 0\n", encoding="utf-8")
    assert main(["--repo-root", str(repo), "--mode", "ci-parity", "--shard", "1"]) != 0
    helper_script.write_text(orig_helper, encoding="utf-8")

    # Fewer than 7 test files breaks the 7-shard partition invariant
    (repo / "tests" / "unit" / "test_shard_6.py").unlink()
    assert main(["--repo-root", str(repo), "--mode", "ci-parity", "--shard", "1"]) == 2
