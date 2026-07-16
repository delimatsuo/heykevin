"""Process and import boundaries for the offline-only Gate 0B slice."""

import ast
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import stat
import sys
from types import SimpleNamespace

import pytest
from app.services.qualification_environment import EXECUTION_DEPENDENCY_PATHS
import scripts.launch_qualification as launcher_module


GATE0B_MODULES = {
    "app.services.caller_turn_alignment",
    "app.services.caller_turn_measurement",
    "app.services.caller_turn_qualification",
    "app.services.caller_turns",
    "app.services.gemini_turn_events",
    "app.services.qualification_allocation",
    "app.services.qualification_environment",
    "app.services.qualification_identity",
    "app.services.qualification_ledger",
    "app.services.qualification_privacy",
    "app.services.qualification_private_paths",
    "app.services.voice_turn_replay",
    "scripts.evaluate_gemini_caller_turn_qualification",
    "scripts.run_gemini_caller_turn_qualification",
}
LIVE_PIPELINES = (
    Path("app/services/gemini_pipeline.py"),
    Path("app/services/voice_pipeline.py"),
)
DEPLOY_WORKFLOW = Path(".github/workflows/deploy.yml")
RUNBOOK = Path("docs/gemini-caller-turn-qualification-gate-0b.md")
IMPLEMENTATION_PLAN = Path(
    "docs/superpowers/plans/2026-07-15-gemini-caller-turn-qualification-gate-0b.md"
)
RUNNER = Path("scripts/run_gemini_caller_turn_qualification.py")
EVALUATOR = Path("scripts/evaluate_gemini_caller_turn_qualification.py")
ENVIRONMENT_VERIFIER = Path("scripts/verify_qualification_environment.py")
TRUSTED_STARTUP = Path("scripts/launch_qualification.py")
APPROVED_UV_INSTALL = 'run: python -m pip install "uv==0.11.7"'
STARTUP_FLAG_NAMES = {
    "bytes_warning",
    "debug",
    "dev_mode",
    "dont_write_bytecode",
    "hash_randomization",
    "ignore_environment",
    "inspect",
    "int_max_str_digits",
    "interactive",
    "isolated",
    "no_site",
    "no_user_site",
    "optimize",
    "quiet",
    "safe_path",
    "utf8_mode",
    "verbose",
    "warn_default_encoding",
}


def _startup_probe(
    python: str | Path,
    *,
    environment: dict[str, str] | None = None,
    launcher: Path = TRUSTED_STARTUP,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python), "-B", "-I", "-S", str(launcher), "probe"],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _qualification_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "qualification-repo"
    repo.mkdir()
    for relative in EXECUTION_DEPENDENCY_PATHS:
        source = Path.cwd() / relative
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    _git(repo, "init")
    _git(repo, "config", "user.email", "qualification@example.invalid")
    _git(repo, "config", "user.name", "Qualification Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "qualification fixture")
    return repo


def _install_sentinel_target(repo: Path, target: Path, sentinel: Path) -> None:
    destination = repo / target
    destination.write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('imported', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _git(repo, "add", str(target))
    _git(repo, "commit", "-m", "install target sentinel")


def _runtime_site_packages(runtime: Path) -> Path:
    return (
        runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )


def _create_runtime(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    runtime = tmp_path / "qualification-runtime"
    created = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(runtime)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert created.returncode == 0, created.stderr
    site_packages = _runtime_site_packages(runtime)
    package = site_packages / "approved_package"
    package.mkdir()
    source = package / "__init__.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    bytecode = package / "__pycache__" / "__init__.cpython-312.pyc"
    bytecode.parent.mkdir()
    py_compile.compile(str(source), cfile=str(bytecode), doraise=True)
    native = package / "approved_native.so"
    native_source = Path(math.__file__ or "")
    assert native_source.is_file()
    shutil.copyfile(native_source, native)
    metadata = site_packages / "approved_package-1.0.0.dist-info" / "METADATA"
    metadata.parent.mkdir()
    metadata.write_text("Name: approved-package\nVersion: 1.0.0\n", encoding="utf-8")
    data = site_packages / "approved_package-1.0.0.dist-info" / "entry_points.txt"
    data.write_text("[approved]\nfixture = approved_package:VALUE\n", encoding="utf-8")
    return runtime / "bin" / "python", {
        "bytecode": bytecode,
        "data": data,
        "metadata": metadata,
        "native": native,
        "source": source,
    }


def _approved_target_command(
    python: str | Path,
    repo: Path,
    *,
    target: str,
    source_sha: str,
    site_manifest_sha256: str,
    target_args: tuple[str, ...] = (),
) -> list[str]:
    return [
        str(python),
        "-B",
        "-I",
        "-S",
        str(repo / TRUSTED_STARTUP),
        target,
        "--expected-source-sha",
        source_sha,
        "--expected-runtime-site-packages-sha256",
        site_manifest_sha256,
        *target_args,
    ]


@pytest.fixture
def qualification_repo(tmp_path: Path) -> Path:
    return _qualification_repo(tmp_path)


def _hook_source(sentinel: Path) -> str:
    return (
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
    )


def test_trusted_startup_launcher_is_stdlib_only_and_target_allowlisted() -> None:
    source = TRUSTED_STARTUP.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots <= {
        "__future__",
        "ctypes",
        "hashlib",
        "importlib",
        "json",
        "os",
        "pathlib",
        "platform",
        "stat",
        "subprocess",
        "sys",
        "types",
        "typing",
    }
    assert "site.addsitedir" not in source
    assert set(ast.literal_eval(next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "TARGETS" for target in node.targets)
    ))) == {
        "evaluate-qualification",
        "run-qualification",
        "verify-environment",
    }


def test_verified_target_executes_captured_bytes_after_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    target = repo / "scripts" / "target.py"
    target.parent.mkdir(parents=True)
    approved_sentinel = tmp_path / "approved"
    replacement_sentinel = tmp_path / "replacement"
    approved_source = (
        "from pathlib import Path\n"
        f"Path({str(approved_sentinel)!r}).write_text('approved', encoding='utf-8')\n"
    ).encode("utf-8")
    target.write_bytes(approved_source)
    snapshot = {"scripts/target.py": approved_source}
    target.write_text(
        "from pathlib import Path\n"
        f"Path({str(replacement_sentinel)!r}).write_text('replacement', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        launcher_module,
        "TARGETS",
        {"test-target": "scripts/target.py"},
    )

    launcher_module._execute_snapshot_target(
        str(repo),
        target="test-target",
        target_args=(),
        snapshot=snapshot,
    )

    assert approved_sentinel.read_text(encoding="utf-8") == "approved"
    assert replacement_sentinel.exists() is False


def test_verified_target_bounds_unapproved_project_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    target = repo / "scripts" / "target.py"
    target.parent.mkdir(parents=True)
    source = b"import app.not_in_approved_snapshot\n"
    target.write_bytes(source)
    monkeypatch.setattr(
        launcher_module,
        "TARGETS",
        {"test-target": "scripts/target.py"},
    )

    with pytest.raises(
        launcher_module.BootstrapError,
        match="outside the approved runtime",
    ):
        launcher_module._execute_snapshot_target(
            str(repo),
            target="test-target",
            target_args=(),
            snapshot={"scripts/target.py": source},
        )


def test_trusted_startup_preflight_binds_current_committed_component_bytes(
    qualification_repo: Path,
) -> None:
    completed = _startup_probe(
        sys.executable,
        launcher=qualification_repo / TRUSTED_STARTUP,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    source = report["source_preflight"]
    assert source["source_sha"] == _git(qualification_repo, "rev-parse", "HEAD")
    assert source["clean"] is True
    dependencies = source["dependencies"]
    for relative in (
        "scripts/launch_qualification.py",
        "scripts/run_gemini_caller_turn_qualification.py",
        "scripts/evaluate_gemini_caller_turn_qualification.py",
        "scripts/verify_qualification_environment.py",
        "app/services/qualification_environment.py",
        "app/services/qualification_identity.py",
    ):
        path_key = sha256(relative.encode("utf-8")).hexdigest()
        assert dependencies[path_key]["worktree_sha256"] == sha256(
            (qualification_repo / relative).read_bytes()
        ).hexdigest()
        assert dependencies[path_key]["git_blob_id"] == _git(
            qualification_repo,
            "rev-parse",
            f"HEAD:{relative}",
        )
    assert str(qualification_repo) not in completed.stdout
    assert report["interpreter_installation"]["installation_sha256"]
    site_manifest = report["runtime_site_packages_manifest"]
    assert site_manifest["manifest_sha256"]
    assert site_manifest["source_count"] > 0
    assert site_manifest["native_extension_count"] > 0


@pytest.mark.parametrize(
    "target",
    ("evaluate-qualification", "run-qualification", "verify-environment"),
)
def test_executable_targets_require_external_source_and_site_approvals(
    qualification_repo: Path,
    target: str,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            "-S",
            str(qualification_repo / TRUSTED_STARTUP),
            target,
        ],
        cwd=qualification_repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "error_code": "qualification_startup_invalid",
        "status": "blocked",
    }


def test_trusted_startup_rejects_clean_wrong_head_before_target_import(
    tmp_path: Path,
) -> None:
    repo = _qualification_repo(tmp_path)
    approved_source_sha = _git(repo, "rev-parse", "HEAD")
    sentinel = tmp_path / "wrong-head-imported"
    _install_sentinel_target(repo, ENVIRONMENT_VERIFIER, sentinel)
    probe = _startup_probe(
        sys.executable,
        launcher=repo / TRUSTED_STARTUP,
    )
    assert probe.returncode == 0, probe.stderr
    probe_report = json.loads(probe.stdout)

    completed = subprocess.run(
        _approved_target_command(
            sys.executable,
            repo,
            target="verify-environment",
            source_sha=approved_source_sha,
            site_manifest_sha256=probe_report["runtime_site_packages_manifest"][
                "manifest_sha256"
            ],
        ),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "error_code": "qualification_startup_invalid",
        "status": "blocked",
    }
    assert sentinel.exists() is False


def test_trusted_startup_rejects_third_party_byte_drift_before_target_import(
    tmp_path: Path,
) -> None:
    repo = _qualification_repo(tmp_path)
    sentinel = tmp_path / "site-drift-imported"
    _install_sentinel_target(repo, ENVIRONMENT_VERIFIER, sentinel)
    runtime_python, runtime_files = _create_runtime(tmp_path)
    probe = _startup_probe(
        runtime_python,
        launcher=repo / TRUSTED_STARTUP,
    )
    assert probe.returncode == 0, probe.stderr
    probe_report = json.loads(probe.stdout)
    runtime_files["source"].write_text("VALUE = 2\n", encoding="utf-8")

    completed = subprocess.run(
        _approved_target_command(
            runtime_python,
            repo,
            target="verify-environment",
            source_sha=_git(repo, "rev-parse", "HEAD"),
            site_manifest_sha256=probe_report["runtime_site_packages_manifest"][
                "manifest_sha256"
            ],
        ),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "error_code": "qualification_startup_invalid",
        "status": "blocked",
    }
    assert sentinel.exists() is False


def test_trusted_startup_rejects_dirty_dependency_before_import_sentinel(
    tmp_path: Path,
) -> None:
    repo = _qualification_repo(tmp_path)
    probe = _startup_probe(
        sys.executable,
        launcher=repo / TRUSTED_STARTUP,
    )
    assert probe.returncode == 0, probe.stderr
    probe_report = json.loads(probe.stdout)
    sentinel = tmp_path / "project-imported"
    dependency = repo / "app/services/qualification_environment.py"
    dependency.write_text(
        dependency.read_text(encoding="utf-8")
        + "\nPath("
        + repr(str(sentinel))
        + ").write_text('imported', encoding='utf-8')\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        _approved_target_command(
            sys.executable,
            repo,
            target="verify-environment",
            source_sha=probe_report["source_preflight"]["source_sha"],
            site_manifest_sha256=probe_report["runtime_site_packages_manifest"][
                "manifest_sha256"
            ],
            target_args=("--phase", "before"),
        ),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "error_code": "qualification_startup_invalid",
        "status": "blocked",
    }
    assert sentinel.exists() is False


def test_trusted_startup_rejects_preloaded_executable_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_path = Path.cwd() / "app/services/qualification_environment.py"
    monkeypatch.setitem(
        sys.modules,
        "qualification_preloaded_alias",
        SimpleNamespace(__file__=str(executable_path)),
    )

    with pytest.raises(launcher_module.BootstrapError, match="preloaded executable"):
        launcher_module._reject_preloaded_executable_modules(str(Path.cwd()))


def test_trusted_startup_rejects_sourceless_bytecode_in_repository_import_roots(
    tmp_path: Path,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "app" / "shadow.pyc").write_bytes(b"untrusted")

    with pytest.raises(
        launcher_module.BootstrapError,
        match="sourceless repository bytecode",
    ):
        launcher_module._reject_sourceless_repository_bytecode(str(tmp_path))


def test_trusted_startup_rejects_sourceless_runtime_bytecode(
    tmp_path: Path,
    qualification_repo: Path,
) -> None:
    runtime = tmp_path / "qualification-runtime"
    created = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(runtime)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert created.returncode == 0, created.stderr
    site_packages = (
        runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    (site_packages / "ambient_only.pyc").write_bytes(b"untrusted")

    completed = _startup_probe(
        runtime / "bin" / "python",
        launcher=qualification_repo / TRUSTED_STARTUP,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "error_code": "qualification_startup_invalid",
        "status": "blocked",
    }


def test_trusted_startup_rejects_ambient_container_digest_claim(
    qualification_repo: Path,
) -> None:
    environment = os.environ.copy()
    environment["QUALIFICATION_CONTAINER_IMAGE_DIGEST"] = "sha256:" + "a" * 64

    completed = _startup_probe(
        sys.executable,
        environment=environment,
        launcher=qualification_repo / TRUSTED_STARTUP,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "error_code": "qualification_startup_invalid",
        "status": "blocked",
    }


def test_every_gate0b_module_is_absent_from_live_pipeline_imports_and_source() -> None:
    for path in LIVE_PIPELINES:
        source = path.read_text(encoding="utf-8")
        imported_modules: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
                imported_modules.update(
                    f"{node.module}.{alias.name}" for alias in node.names
                )

        assert GATE0B_MODULES.isdisjoint(imported_modules), path
        for module in GATE0B_MODULES:
            assert module.rsplit(".", 1)[-1] not in source, (path, module)


def test_gate0b_imports_and_dry_run_succeed_with_process_wide_network_denial() -> None:
    imports = "\n".join(
        f"importlib.import_module({module!r})" for module in sorted(GATE0B_MODULES)
    )
    program = f"""
import asyncio
import importlib
import socket

def deny(*_args, **_kwargs):
    raise AssertionError("network access is forbidden in Gate 0B offline tests")

socket.socket = deny
socket.create_connection = deny
socket.getaddrinfo = deny
{imports}
from scripts.run_gemini_caller_turn_qualification import main
assert main(["--dry-run"]) == 0
"""

    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "provider_execution_authorized" in completed.stdout


def test_trusted_startup_rejects_nonisolated_or_site_enabled_invocation() -> None:
    for command in (
        [sys.executable, str(TRUSTED_STARTUP), "probe"],
        [sys.executable, "-B", "-I", str(TRUSTED_STARTUP), "probe"],
        [sys.executable, "-B", "-S", str(TRUSTED_STARTUP), "probe"],
    ):
        completed = subprocess.run(
            command,
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert completed.returncode == 2
        assert json.loads(completed.stdout) == {
            "error_code": "qualification_startup_not_isolated",
            "status": "blocked",
        }


def test_trusted_startup_requires_bytecode_disabled_at_interpreter_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flags = {
        name: getattr(sys.flags, name)
        for name in launcher_module.STARTUP_FLAG_NAMES
    }
    flags.update(
        dont_write_bytecode=0,
        ignore_environment=1,
        isolated=1,
        no_site=1,
        no_user_site=1,
        safe_path=True,
    )
    monkeypatch.setattr(launcher_module.sys, "flags", SimpleNamespace(**flags))
    for module_name in launcher_module.AUTOMATIC_STARTUP_MODULES:
        monkeypatch.delitem(launcher_module.sys.modules, module_name, raising=False)

    with pytest.raises(launcher_module.BootstrapError, match="python -B -I -S"):
        launcher_module._require_isolated_no_site_startup()


def test_ci_removes_hosted_python_loader_override_from_gate0b_processes() -> None:
    source = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert source.count("/usr/bin/env -u LD_LIBRARY_PATH") == 4
    assert "unset LD_LIBRARY_PATH" in RUNBOOK.read_text(encoding="utf-8")


@pytest.mark.parametrize("target", (RUNNER, EVALUATOR, ENVIRONMENT_VERIFIER))
def test_gate0b_targets_reject_forged_marker_under_normal_python(target: Path) -> None:
    environment = os.environ.copy()
    environment["KEVIN_GATE0B_TRUSTED_STARTUP"] = "{}"

    completed = subprocess.run(
        [sys.executable, str(target)],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "error_code": "qualification_startup_required",
        "status": "blocked",
    }


def test_trusted_startup_neutralizes_ambient_python_paths_and_customize_hooks(
    tmp_path: Path,
    qualification_repo: Path,
) -> None:
    injection_root = tmp_path / "external-python-path"
    injection_root.mkdir()
    sentinels: list[Path] = []
    for module_name in ("sitecustomize", "usercustomize"):
        source_sentinel = tmp_path / f"{module_name}-source-executed"
        (injection_root / f"{module_name}.py").write_text(
            _hook_source(source_sentinel),
            encoding="utf-8",
        )
        sentinels.append(source_sentinel)

    sourceless_root = tmp_path / "external-bytecode-path"
    sourceless_root.mkdir()
    for module_name in ("sitecustomize", "usercustomize"):
        bytecode_sentinel = tmp_path / f"{module_name}-bytecode-executed"
        source = tmp_path / f"{module_name}-compile-source.py"
        source.write_text(_hook_source(bytecode_sentinel), encoding="utf-8")
        py_compile.compile(
            str(source),
            cfile=str(sourceless_root / f"{module_name}.pyc"),
            doraise=True,
        )
        source.unlink()
        sentinels.append(bytecode_sentinel)

    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(injection_root), str(sourceless_root))
    )
    environment["PYTHONHOME"] = str(tmp_path / "untrusted-python-home")

    completed = _startup_probe(
        sys.executable,
        environment=environment,
        launcher=qualification_repo / TRUSTED_STARTUP,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["target"] == "probe"
    assert report["neutralized_environment"] == ["PYTHONHOME", "PYTHONPATH"]
    assert set(report["startup_flags"]) == STARTUP_FLAG_NAMES
    assert report["startup_flags"]["isolated"] == 1
    assert report["startup_flags"]["no_site"] == 1
    assert report["startup_flags"]["ignore_environment"] == 1
    assert report["startup_flags"]["dont_write_bytecode"] == 1
    assert report["startup_flags"]["no_user_site"] == 1
    assert report["startup_flags"]["safe_path"] is True
    assert report["bytecode_write_disabled"] is True
    assert len(report["pycache_prefix_location_sha256"]) == 64
    path_digests = report["effective_sys_path_entry_sha256"]
    assert sha256(str(injection_root.resolve()).encode("utf-8")).hexdigest() not in path_digests
    assert sha256(str(sourceless_root.resolve()).encode("utf-8")).hexdigest() not in path_digests
    assert all(not sentinel.exists() for sentinel in sentinels)


def test_trusted_startup_recognizes_but_never_executes_runtime_hook_artifacts(
    tmp_path: Path,
    qualification_repo: Path,
) -> None:
    runtime = tmp_path / "qualification-runtime"
    created = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(runtime)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert created.returncode == 0, created.stderr
    runtime_python = runtime / "bin" / "python"
    site_packages = (
        runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    injection_root = tmp_path / "pth-injection"
    injection_root.mkdir()
    pth_sentinel = tmp_path / "pth-executed"
    pth_path = site_packages / "qualification-injection.pth"
    pth_path.write_text(
        f"{injection_root}\n"
        "import pathlib; "
        f"pathlib.Path({str(pth_sentinel)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )

    hook_sentinels: list[Path] = []
    for module_name in ("sitecustomize", "usercustomize"):
        source_sentinel = tmp_path / f"runtime-{module_name}-source-executed"
        source_path = site_packages / f"{module_name}.py"
        source_path.write_text(_hook_source(source_sentinel), encoding="utf-8")
        hook_sentinels.append(source_sentinel)

        bytecode_sentinel = tmp_path / f"runtime-{module_name}-bytecode-executed"
        py_compile.compile(
            str(source_path),
            cfile=str(site_packages / f"{module_name}.pyc"),
            doraise=True,
        )
        source_path.write_text(_hook_source(source_sentinel), encoding="utf-8")
        hook_sentinels.append(bytecode_sentinel)

    completed = _startup_probe(
        runtime_python,
        launcher=qualification_repo / TRUSTED_STARTUP,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    pth_path_digest = sha256(str(pth_path.resolve()).encode("utf-8")).hexdigest()
    assert report["runtime_pth_files_sha256"][pth_path_digest] == sha256(
        pth_path.read_bytes()
    ).hexdigest()
    assert sha256(str(injection_root.resolve()).encode("utf-8")).hexdigest() not in report[
        "effective_sys_path_entry_sha256"
    ]
    assert pth_sentinel.exists() is False
    assert all(not sentinel.exists() for sentinel in hook_sentinels)
    assert len(report["ignored_startup_hook_files_sha256"]) == 4


def test_documented_runner_entrypoint_is_offline_and_execute_stays_blocked(
    tmp_path: Path,
    qualification_repo: Path,
) -> None:
    guard_root = tmp_path / "network-denial"
    guard_root.mkdir()
    (guard_root / "sitecustomize.py").write_text(
        """
import socket

def deny(*_args, **_kwargs):
    raise AssertionError("network access is forbidden in Gate 0B offline tests")

class DeniedSocket(socket.socket):
    def connect(self, *_args, **_kwargs):
        deny()

    def connect_ex(self, *_args, **_kwargs):
        deny()

socket.socket = DeniedSocket
socket.create_connection = deny
socket.getaddrinfo = deny
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(guard_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        environment.pop(key, None)
    probe = _startup_probe(
        sys.executable,
        environment=environment,
        launcher=qualification_repo / TRUSTED_STARTUP,
    )
    assert probe.returncode == 0, probe.stderr
    probe_report = json.loads(probe.stdout)
    approved_source_sha = probe_report["source_preflight"]["source_sha"]
    approved_site_sha = probe_report["runtime_site_packages_manifest"][
        "manifest_sha256"
    ]

    dry_run = subprocess.run(
        _approved_target_command(
            sys.executable,
            qualification_repo,
            target="run-qualification",
            source_sha=approved_source_sha,
            site_manifest_sha256=approved_site_sha,
            target_args=("--dry-run",),
        ),
        cwd=qualification_repo,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    execute = subprocess.run(
        _approved_target_command(
            sys.executable,
            qualification_repo,
            target="run-qualification",
            source_sha=approved_source_sha,
            site_manifest_sha256=approved_site_sha,
            target_args=("--execute",),
        ),
        cwd=qualification_repo,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert dry_run.returncode == 0, dry_run.stderr
    assert json.loads(dry_run.stdout)["provider_execution_authorized"] is False
    assert execute.returncode == 2, execute.stderr
    assert json.loads(execute.stdout) == {
        "error_code": "provider_execution_not_authorized",
        "status": "blocked",
    }
    direct = subprocess.run(
        [sys.executable, str(RUNNER), "--dry-run"],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert direct.returncode == 2
    assert json.loads(direct.stdout) == {
        "error_code": "qualification_startup_required",
        "status": "blocked",
    }


def test_provisioned_sensitive_target_rejects_user_mutable_runtime_before_import(
    tmp_path: Path,
    qualification_repo: Path,
) -> None:
    sentinel = tmp_path / "sensitive-target-imported"
    approval_root = (
        qualification_repo
        / "config/qualification/gate_0b_approval_root.ed25519.pub"
    )
    approval_root.write_bytes(b"a" * 32)
    _install_sentinel_target(qualification_repo, RUNNER, sentinel)
    _git(qualification_repo, "add", str(approval_root))
    _git(qualification_repo, "commit", "-m", "provision approval root")
    probe = _startup_probe(
        sys.executable,
        launcher=qualification_repo / TRUSTED_STARTUP,
    )
    assert probe.returncode == 0, probe.stderr
    report = json.loads(probe.stdout)

    completed = subprocess.run(
        _approved_target_command(
            sys.executable,
            qualification_repo,
            target="run-qualification",
            source_sha=report["source_preflight"]["source_sha"],
            site_manifest_sha256=report["runtime_site_packages_manifest"][
                "manifest_sha256"
            ],
            target_args=("--execute",),
        ),
        cwd=qualification_repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "error_code": "qualification_startup_invalid",
        "status": "blocked",
    }
    assert sentinel.exists() is False


def test_runbook_creates_operator_owned_private_directories(tmp_path: Path) -> None:
    source = RUNBOOK.read_text(encoding="utf-8")
    dry_run_section = source.split("## Dry-Run Template", 1)[1]
    setup_block = dry_run_section.split("```bash", 1)[1].split("```", 1)[0].strip()
    state_root = tmp_path / "state"
    home = tmp_path / "home"
    state_root.mkdir()
    home.mkdir()
    environment = os.environ.copy()
    environment["XDG_STATE_HOME"] = str(state_root)
    environment["HOME"] = str(home)

    completed = subprocess.run(
        ["/bin/sh", "-eu", "-c", setup_block],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    qualification_root = state_root / "hey-kevin-qualification"
    for relative in ("", "preregistration", "evidence", "capsules", "ledger"):
        path = qualification_root / relative
        metadata = path.stat()
        assert metadata.st_uid == os.getuid()
        assert stat.S_IMODE(metadata.st_mode) == 0o700
    assert "gate0b-startup-probe.json" in source
    assert "REVIEWED_GATE0B_SOURCE_SHA" in source
    assert "REVIEWED_GATE0B_RUNTIME_SITE_PACKAGES_SHA256" in source
    assert "must not be rediscovered" in source


def test_ci_uses_exact_locked_qualification_environment() -> None:
    source = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    normalized = " ".join(source.split())
    runbook_normalized = " ".join(RUNBOOK.read_text(encoding="utf-8").split())

    assert source.count(APPROVED_UV_INSTALL) == 1
    assert source.index(APPROVED_UV_INSTALL) < source.index("- name: Run tests")
    assert "python-version: '3.12.13'" in source
    assert "run: uv lock --check" in source
    assert "run: uv sync --locked --extra dev --python 3.12.13" in source
    assert (
        "run: /usr/bin/env -u LD_LIBRARY_PATH uv run --locked --no-sync "
        "--extra dev --python 3.12.13 python -m pytest --tb=short -q"
    ) in normalized
    assert "python -m pytest --tb=short -q" in runbook_normalized
    assert "python -m pytest tests/unit" not in runbook_normalized
    assert "python -B -I -S scripts/launch_qualification.py probe" in normalized
    assert "QUALIFICATION_EXPECTED_SOURCE_SHA" in source
    assert "QUALIFICATION_EXPECTED_RUNTIME_SITE_PACKAGES_SHA256" in source
    assert normalized.count("--expected-source-sha") == 2
    assert normalized.count("--expected-runtime-site-packages-sha256") == 2
    assert "python -B -I -S scripts/launch_qualification.py verify-environment" in normalized
    assert "--phase before" in normalized
    assert "--phase after" in normalized
    assert "python -m compileall -q app/services scripts" in normalized
    assert "ruff check" in source
    assert "bandit -q -lll" in normalized
    assert 'pip install -e ".[dev]"' not in source


def test_plan_routes_environment_verification_through_trusted_launcher() -> None:
    source = IMPLEMENTATION_PLAN.read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    assert "python scripts/verify_qualification_environment.py" not in normalized
    assert normalized.count(
        "python -B -I -S scripts/launch_qualification.py verify-environment"
    ) == 2
    assert "python -m pytest --tb=short -q" in normalized


def test_runbook_sensitive_launch_starts_from_external_absolute_python() -> None:
    source = RUNBOOK.read_text(encoding="utf-8")
    actual_execution = source.split(
        "The probe-derived values above are limited to CI and offline dry-run discovery.",
        1,
    )[1].split("## Approval Sequence", 1)[0]

    assert "externally provisioned, root-owned" in actual_execution
    assert (
        "/opt/hey-kevin-gate0b/bin/python3.12 -B -I -S "
        "scripts/launch_qualification.py run-qualification"
    ) in " ".join(actual_execution.split())
    assert "\npython -B -I -S scripts/launch_qualification.py" not in actual_execution
