"""Process and import boundaries for the offline-only Gate 0B slice."""

import ast
import json
import os
from pathlib import Path
import subprocess
import stat
import sys


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
APPROVED_UV_INSTALL = 'run: python -m pip install "uv==0.11.7"'


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
        [sys.executable, str(RUNNER), "--dry-run"],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    execute = subprocess.run(
        [sys.executable, str(RUNNER), "--execute"],
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
    assert "python scripts/verify_qualification_environment.py --phase before" in normalized
    assert "python scripts/verify_qualification_environment.py --phase after" in normalized
    assert "python -m compileall -q app/services scripts" in normalized
    assert "ruff check" in source
    assert "bandit -q -lll" in normalized
    assert 'pip install -e ".[dev]"' not in source
