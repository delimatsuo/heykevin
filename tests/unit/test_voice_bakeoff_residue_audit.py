import ast
import os
import pathlib

import pytest

from app.services.voice_bakeoff_residue_audit import audit_residue


def test_passes_when_no_files_present(tmp_path):
    result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=10_000)
    assert result.passed is True
    assert result.remaining_paths == ()


def test_passes_when_files_are_within_ttl(tmp_path):
    stale = tmp_path / "fresh.json"
    stale.write_text("{}")
    now_ms = int(stale.stat().st_mtime * 1000) + 500

    result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=now_ms)
    assert result.passed is True


def test_fails_when_a_file_exceeds_ttl(tmp_path):
    old = tmp_path / "old.json"
    old.write_text("{}")
    mtime_ms = int(old.stat().st_mtime * 1000)
    now_ms = mtime_ms + 5000

    result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=now_ms)
    assert result.passed is False
    assert str(old) in result.remaining_paths


def test_checks_nested_directories(tmp_path):
    nested = tmp_path / "nested" / "deep"
    nested.mkdir(parents=True)
    old = nested / "old.json"
    old.write_text("{}")
    mtime_ms = int(old.stat().st_mtime * 1000)
    now_ms = mtime_ms + 5000

    result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=now_ms)
    assert result.passed is False
    assert str(old) in result.remaining_paths


def test_module_performs_no_network_or_subprocess_calls():
    source = pathlib.Path("app/services/voice_bakeoff_residue_audit.py").read_text()
    tree = ast.parse(source)
    banned = {"socket", "subprocess", "urllib", "httpx", "requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned, node.module
