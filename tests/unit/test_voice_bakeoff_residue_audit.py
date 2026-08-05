import ast
import os
import pathlib
import sys

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


def test_passes_when_age_equals_ttl_boundary(tmp_path):
    # The threshold check is strict `>`, so a file whose age is exactly
    # equal to the TTL has not yet exceeded it.
    boundary = tmp_path / "boundary.json"
    boundary.write_text("{}")
    mtime_ms = int(boundary.stat().st_mtime * 1000)
    now_ms = mtime_ms + 1000

    result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=now_ms)
    assert result.passed is True
    assert result.remaining_paths == ()


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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX permission bits only; chmod(0o000) is not meaningful on Windows",
)
def test_unreadable_subdirectory_fails_closed_and_is_reported(tmp_path):
    """Reviewer-confirmed fail-open bug: the old implementation walked with
    Path.rglob("*"), which silently swallows PermissionError while listing a
    directory it cannot read — so an unreadable subdirectory was treated as
    if it simply did not exist, and the audit reported passed=True for a
    tree it never actually finished inspecting. An audit that cannot prove
    a directory is clean must not report it as clean.

    This is skipped when running as root: root bypasses POSIX permission
    bits on both Linux and macOS, so chmod(0o000) would not actually make
    the subdirectory unreadable and the test would spuriously fail.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("chmod(0o000) does not restrict root")

    old = tmp_path / "old.json"
    old.write_text("{}")
    mtime_ms = int(old.stat().st_mtime * 1000)

    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "secret.json").write_text("{}")

    os.chmod(locked, 0o000)
    try:
        now_ms = mtime_ms + 5000
        result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=now_ms)

        assert result.passed is False
        assert any("locked" in path for path in result.unreadable_paths)
        # The unreadable subdirectory must not silently suppress reporting
        # of residue the audit *could* inspect elsewhere in the same tree.
        assert str(old) in result.remaining_paths
    finally:
        # Restore permissions before tmp_path's own fixture teardown runs,
        # or pytest's cleanup of this directory will itself fail to remove
        # an unreadable subdirectory.
        os.chmod(locked, 0o755)


def test_dangling_symlink_past_ttl_is_reported(tmp_path):
    # A symlink whose target has been removed is still residue: it is a
    # filesystem entry left behind under `destination`. is_file()/stat()
    # would silently ignore it (they follow the link and find nothing),
    # so this proves the symlink itself is evaluated via lstat().
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "dangling.json"
    link.symlink_to(target)
    target.unlink()

    old_ts = 1_000_000
    os.utime(link, (old_ts, old_ts), follow_symlinks=False)
    now_ms = (old_ts * 1000) + 5000

    result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=now_ms)
    assert result.passed is False
    assert str(link) in result.remaining_paths


def test_dangling_symlink_within_ttl_is_not_reported(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "dangling.json"
    link.symlink_to(target)
    target.unlink()

    recent_ts = 1_000_000
    os.utime(link, (recent_ts, recent_ts), follow_symlinks=False)
    now_ms = (recent_ts * 1000) + 500

    result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=now_ms)
    assert result.passed is True
    assert result.remaining_paths == ()


def test_live_symlink_uses_its_own_age_not_the_targets(tmp_path, tmp_path_factory):
    # A live symlink pointing outside `destination` must be judged by how
    # long the *link* has sat under `destination` (lstat), not by the
    # target's mtime (stat) — the two can differ arbitrarily.
    external_dir = tmp_path_factory.mktemp("external")
    target = external_dir / "target.json"
    target.write_text("{}")
    old_target_ts = 1_000_000
    os.utime(target, (old_target_ts, old_target_ts))

    link = tmp_path / "live_link.json"
    link.symlink_to(target)
    recent_link_ts = 2_000_000
    os.utime(link, (recent_link_ts, recent_link_ts), follow_symlinks=False)

    # "now" is just past the link's own (recent) mtime. If age were taken
    # from the target's (much older) mtime, this would fail the TTL by a
    # wide margin instead of passing.
    now_ms = (recent_link_ts * 1000) + 500

    result = audit_residue(tmp_path, artifact_ttl_ms=1000, now_ms=now_ms)
    assert result.passed is True
    assert result.remaining_paths == ()


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
