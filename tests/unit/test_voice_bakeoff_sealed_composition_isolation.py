"""Static proof that the future bakeoff control composition remains absent."""

from __future__ import annotations

import ast
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_PROPOSED_COMPOSITION = (
    _ROOT / "app/experiments/voice_bakeoff_control_composition.py"
)
_UNMOUNTED_MODULES = {
    "voice_bakeoff_control_admission_projection",
    "voice_bakeoff_control_store_assembly",
    "voice_bakeoff_firestore_transaction_port",
    "voice_bakeoff_google_firestore_runner",
    "voice_bakeoff_control_composition",
}
_SOURCE_ONLY_MODULE_FILES = {
    _ROOT / "app/services/voice_bakeoff_control_admission_projection.py",
    _ROOT / "app/services/voice_bakeoff_control_store_assembly.py",
    _ROOT / "app/services/voice_bakeoff_firestore_transaction_port.py",
    _ROOT / "app/services/voice_bakeoff_google_firestore_runner.py",
}
_DEPLOYMENT_ARTIFACT_ROOTS = (_ROOT / ".github", _ROOT / ".Codex")


def _module_references(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.update(node.module.split("."))
            for alias in node.names:
                imported.update(alias.name.split("."))
    return imported


def _dynamic_import_calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"__import__", "import_module", "find_spec", "reload"}
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in forbidden
    } | {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden
    }


def _runtime_and_deployment_files() -> tuple[Path, ...]:
    runtime = (
        *(_ROOT / "app").rglob("*.py"),
        *(_ROOT / "scripts").rglob("*.py"),
    )
    deployment_from_roots = tuple(
        path
        for root in _DEPLOYMENT_ARTIFACT_ROOTS
        if root.exists()
        for path in root.rglob("*")
    )
    deployment_top_level = (
        *_ROOT.glob("Dockerfile*"),
        _ROOT / "Procfile",
        _ROOT / "Makefile",
        _ROOT / "pyproject.toml",
    )
    deployment = tuple(
        path
        for path in (*deployment_from_roots, *deployment_top_level)
        if path.is_file()
    )
    return tuple(
        path
        for path in (*runtime, *deployment)
        if path not in _SOURCE_ONLY_MODULE_FILES
    )


def test_sealed_control_composition_file_is_not_present_or_mounted() -> None:
    assert not _PROPOSED_COMPOSITION.exists()
    for path in _runtime_and_deployment_files():
        source = path.read_text(encoding="utf-8")
        assert not any(module in source for module in _UNMOUNTED_MODULES), (
            path.relative_to(_ROOT)
        )
        if path.suffix == ".py":
            assert _module_references(path).isdisjoint(_UNMOUNTED_MODULES), (
                path.relative_to(_ROOT)
            )
            assert not _dynamic_import_calls(path), path.relative_to(_ROOT)


def test_control_composition_packet_remains_apply_prohibited() -> None:
    packet = (
        _ROOT / "docs/security/voice-bakeoff-sealed-composition-and-iam-packet.md"
    ).read_text(encoding="utf-8")
    assert "**Status:** apply-prohibited, source/reference-only preparation." in packet
    assert "The current gate remains `execution_status: not_authorized`." in packet
    assert packet.count("# DO NOT RUN: future gated") == 3
    assert "roles/datastore.user` is prohibited" in packet
    assert "voiceBakeoffControlTransaction" in packet
    assert "datastore.entities.delete" not in packet
    assert 'resource.type=="firestore.googleapis.com"' in packet
    assert 'resource.type=="firestore.googleapis.com/Database"' not in packet
    assert "Pre-auth |" not in packet
    assert "there is no pre-auth composition, pre-auth sdk runner" in packet.lower()
