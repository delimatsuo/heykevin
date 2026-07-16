"""Process and import boundaries for the offline-only Gate 0B slice."""

import ast
from pathlib import Path
import subprocess
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


def test_ci_installs_approved_uv_runtime_before_running_tests() -> None:
    source = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert source.count(APPROVED_UV_INSTALL) == 1
    assert source.index(APPROVED_UV_INSTALL) < source.index("- name: Run tests")
