"""Process and import boundaries for the offline-only Gate 0B slice."""

import ast
from hashlib import sha256
import json
import os
from pathlib import Path
import py_compile
import subprocess
import stat
import sys

import pytest
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
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python), "-I", "-S", str(TRUSTED_STARTUP), "probe"],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


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
        "hashlib",
        "json",
        "os",
        "pathlib",
        "runpy",
        "sys",
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
        [sys.executable, "-I", str(TRUSTED_STARTUP), "probe"],
        [sys.executable, "-S", str(TRUSTED_STARTUP), "probe"],
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

    completed = _startup_probe(sys.executable, environment=environment)

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["target"] == "probe"
    assert report["neutralized_environment"] == ["PYTHONHOME", "PYTHONPATH"]
    assert set(report["startup_flags"]) == STARTUP_FLAG_NAMES
    assert report["startup_flags"]["isolated"] == 1
    assert report["startup_flags"]["no_site"] == 1
    assert report["startup_flags"]["ignore_environment"] == 1
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

    completed = _startup_probe(runtime_python)

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

    dry_run = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(TRUSTED_STARTUP),
            "run-qualification",
            "--dry-run",
        ],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    execute = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(TRUSTED_STARTUP),
            "run-qualification",
            "--execute",
        ],
        cwd=Path.cwd(),
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


def test_ci_uses_exact_locked_qualification_environment() -> None:
    source = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    assert source.count(APPROVED_UV_INSTALL) == 1
    assert source.index(APPROVED_UV_INSTALL) < source.index("- name: Run tests")
    assert "python-version: '3.12.13'" in source
    assert "run: uv lock --check" in source
    assert "run: uv sync --locked --extra dev --python 3.12.13" in source
    assert (
        "run: uv run --locked --no-sync --extra dev --python 3.12.13 "
        "python -m pytest --tb=short -q"
    ) in normalized
    assert (
        "python -I -S scripts/launch_qualification.py verify-environment --phase before"
        in normalized
    )
    assert (
        "python -I -S scripts/launch_qualification.py verify-environment --phase after"
        in normalized
    )
    assert "python -m compileall -q app/services scripts" in normalized
    assert "ruff check" in source
    assert "bandit -q -lll" in normalized
    assert 'pip install -e ".[dev]"' not in source
