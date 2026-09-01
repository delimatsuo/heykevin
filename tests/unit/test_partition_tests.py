"""Unit tests for test suite partitioner."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from scripts.partition_tests import (
    DEFAULT_TOTAL_SHARDS,
    PartitionError,
    discover_test_files,
    main,
    parse_args,
    partition_test_files,
    render_test_path,
    render_test_paths,
)


def test_discover_test_files_recursive_and_naming_conventions(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    # Subdirectories
    unit_dir = tests_dir / "unit"
    integration_dir = tests_dir / "integration"
    deep_dir = tests_dir / "nested" / "deep"
    unit_dir.mkdir()
    integration_dir.mkdir()
    deep_dir.mkdir(parents=True)

    # Valid test files matching test_*.py
    t1 = tests_dir / "test_root.py"
    t2 = unit_dir / "test_unit_a.py"
    t3 = deep_dir / "test_nested.py"

    # Valid test files matching *_test.py
    t4 = integration_dir / "integration_test.py"
    t5 = unit_dir / "api_test.py"

    # Valid test file matching BOTH test_* and *_test.py (must be deduplicated)
    t6 = unit_dir / "test_both_test.py"

    for f in (t1, t2, t3, t4, t5, t6):
        f.write_text("# test\n")

    # Non-test files (must be ignored)
    (tests_dir / "conftest.py").write_text("# conftest\n")
    (unit_dir / "helper.py").write_text("# helper\n")
    (unit_dir / "test_helper.txt").write_text("# not python\n")
    (deep_dir / "test_data.json").write_text("{}\n")

    discovered = discover_test_files(tests_dir)

    # Must return root-relative POSIX paths, never absolute, never prefixed by root dir
    expected = sorted(
        [
            "test_root.py",
            "unit/test_unit_a.py",
            "nested/deep/test_nested.py",
            "integration/integration_test.py",
            "unit/api_test.py",
            "unit/test_both_test.py",
        ]
    )

    assert discovered == expected
    assert len(discovered) == 6
    # Verify deduplication for test_both_test.py
    assert len([p for p in discovered if "test_both_test.py" in p]) == 1
    # Verify paths are strictly relative to root
    assert all(not p.startswith("/") and not p.startswith("tests/") for p in discovered)


def test_discover_test_files_rejects_symlinked_root(tmp_path: Path):
    real_dir = tmp_path / "real_tests"
    real_dir.mkdir()
    (real_dir / "test_example.py").write_text("# test\n")

    symlink_dir = tmp_path / "symlink_tests"
    symlink_dir.symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(PartitionError, match="cannot be a symlink"):
        discover_test_files(symlink_dir)


def test_discover_test_files_rejects_symlink_directory(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("# test\n")

    external_dir = tmp_path / "external"
    external_dir.mkdir()
    (external_dir / "test_b.py").write_text("# test\n")

    symlinked_sub = tests_dir / "sym_sub"
    symlinked_sub.symlink_to(external_dir, target_is_directory=True)

    with pytest.raises(PartitionError, match="Symlink directory rejected"):
        discover_test_files(tests_dir)


def test_discover_test_files_rejects_symlink_file(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("# test\n")

    external_file = tmp_path / "test_external.py"
    external_file.write_text("# test\n")

    symlinked_file = tests_dir / "test_sym.py"
    symlinked_file.symlink_to(external_file)

    with pytest.raises(PartitionError, match="Symlink file rejected"):
        discover_test_files(tests_dir)


def test_discover_test_files_rejects_invalid_root_paths(tmp_path: Path):
    # Non-existent path
    with pytest.raises(PartitionError, match="Root path does not exist"):
        discover_test_files(tmp_path / "non_existent")

    # File instead of directory
    a_file = tmp_path / "file.txt"
    a_file.write_text("hello")
    with pytest.raises(PartitionError, match="Root path must be a directory"):
        discover_test_files(a_file)

    # Empty directory (no tests)
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(PartitionError, match="No test files found"):
        discover_test_files(empty_dir)

    # Backslashes
    with pytest.raises(PartitionError, match="Backslashes are not permitted"):
        discover_test_files("tests\\unit")

    # Control characters
    with pytest.raises(PartitionError, match="Control characters are not permitted"):
        discover_test_files("tests\x00unit")

    # Traversal (..)
    with pytest.raises(PartitionError, match="Directory traversal"):
        discover_test_files("tests/../tests")

    # Empty root path string
    with pytest.raises(PartitionError, match="Root path cannot be empty"):
        discover_test_files("")


def test_partition_round_robin_balanced_and_deterministic():
    files = [f"tests/unit/test_{i:02d}.py" for i in range(14)]
    total_shards = 7

    # 14 files into 7 shards -> exactly 2 files per shard
    for shard in range(1, total_shards + 1):
        assigned = partition_test_files(files, shard=shard, total_shards=total_shards)
        assert len(assigned) == 2
        assert assigned[0] == files[shard - 1]
        assert assigned[1] == files[shard - 1 + 7]

    # 15 files into 7 shards -> shard 1 gets 3 files, shards 2-7 get 2 files
    files_15 = [f"tests/unit/test_{i:02d}.py" for i in range(15)]
    shard_1 = partition_test_files(files_15, shard=1, total_shards=7)
    assert len(shard_1) == 3
    assert shard_1 == [files_15[0], files_15[7], files_15[14]]

    for s in range(2, 8):
        shard_s = partition_test_files(files_15, shard=s, total_shards=7)
        assert len(shard_s) == 2
        assert shard_s == [files_15[s - 1], files_15[s - 1 + 7]]


def test_partition_invariants_complete_disjoint_and_exactly_once():
    files = [f"tests/test_{i}.py" for i in range(23)]
    total_shards = 7

    all_assigned: list[str] = []
    seen_sets: list[set[str]] = []

    for s in range(1, total_shards + 1):
        assigned = partition_test_files(files, shard=s, total_shards=total_shards)
        assert len(assigned) > 0
        s_set = set(assigned)
        # Check disjoint with all previous shards
        for prev in seen_sets:
            assert s_set.isdisjoint(prev)
        seen_sets.append(s_set)
        all_assigned.extend(assigned)

    # Exactly-once and complete coverage
    assert sorted(all_assigned) == sorted(files)
    assert len(all_assigned) == len(files)


def test_partition_rejects_invalid_shards_and_empty_shards():
    files = ["tests/test_1.py", "tests/test_2.py", "tests/test_3.py"]

    # Shard 0 or negative
    with pytest.raises(PartitionError, match="shard must be a positive integer"):
        partition_test_files(files, shard=0, total_shards=3)

    with pytest.raises(PartitionError, match="shard must be a positive integer"):
        partition_test_files(files, shard=-1, total_shards=3)

    # Total shards 0 or negative
    with pytest.raises(PartitionError, match="total_shards must be a positive integer"):
        partition_test_files(files, shard=1, total_shards=0)

    # Boolean shard counts (bool is subclass of int in Python, must be explicitly rejected)
    with pytest.raises(PartitionError, match="total_shards must be a positive integer"):
        partition_test_files(files, shard=1, total_shards=True)

    with pytest.raises(PartitionError, match="shard must be a positive integer"):
        partition_test_files(files, shard=True, total_shards=3)

    # Shard exceeds total shards
    with pytest.raises(PartitionError, match="cannot exceed total_shards"):
        partition_test_files(files, shard=4, total_shards=3)

    # Total shards exceeds number of files (would leave empty shards)
    with pytest.raises(PartitionError, match="without empty shards"):
        partition_test_files(files, shard=1, total_shards=5)

    # Empty file list
    with pytest.raises(PartitionError, match="Cannot partition empty list"):
        partition_test_files([], shard=1, total_shards=1)


def test_partition_test_files_validates_individual_file_strings(tmp_path: Path, monkeypatch):
    valid_base = ["tests/test_a.py", "tests/test_b.py"]

    # Reject absolute path
    with pytest.raises(PartitionError, match="Absolute paths are not permitted"):
        partition_test_files(["/tmp/tests/test_a.py", "tests/test_b.py"], shard=1, total_shards=2)

    # Reject backslash
    with pytest.raises(PartitionError, match="Backslashes are not permitted"):
        partition_test_files(["tests\\test_a.py", "tests/test_b.py"], shard=1, total_shards=2)

    # Reject control characters
    with pytest.raises(PartitionError, match="Control characters are not permitted"):
        partition_test_files(["tests/test_\x00a.py", "tests/test_b.py"], shard=1, total_shards=2)

    # Reject directory traversal (..)
    with pytest.raises(PartitionError, match="Directory traversal"):
        partition_test_files(["tests/../tests/test_a.py", "tests/test_b.py"], shard=1, total_shards=2)

    # Reject non-normalized relative segments
    with pytest.raises(PartitionError, match="normalized POSIX relative"):
        partition_test_files(["./tests/test_a.py", "tests/test_b.py"], shard=1, total_shards=2)

    # Reject empty string
    with pytest.raises(PartitionError, match="cannot be empty"):
        partition_test_files(["", "tests/test_b.py"], shard=1, total_shards=2)

    # Reject non-test naming convention
    with pytest.raises(PartitionError, match="naming convention"):
        partition_test_files(["tests/helper.py", "tests/test_b.py"], shard=1, total_shards=2)

    with pytest.raises(PartitionError, match="naming convention"):
        partition_test_files(["tests/test_a.txt", "tests/test_b.py"], shard=1, total_shards=2)

    # Reject duplicate input strings
    with pytest.raises(PartitionError, match="Duplicate test file"):
        partition_test_files(["tests/test_a.py", "tests/test_a.py"], shard=1, total_shards=2)

    # Detect existing aliases resolving to same real file target
    monkeypatch.chdir(tmp_path)
    tests_dir = Path("tests")
    tests_dir.mkdir()
    real_file = tests_dir / "test_real.py"
    real_file.write_text("# test\n")
    alias_file = tests_dir / "test_alias.py"
    os.link(real_file, alias_file)

    with pytest.raises(PartitionError, match="alias detected"):
        partition_test_files(["tests/test_real.py", "tests/test_alias.py"], shard=1, total_shards=2)


def test_partition_cli_dry_run_and_collect_only(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    tests_dir = Path("tests")
    tests_dir.mkdir()
    for i in range(14):
        (tests_dir / f"test_{i:02d}.py").write_text("# test\n")

    # Run CLI with --dry-run (relative root)
    ret = main(["--root", "tests", "--shard", "1", "--total-shards", "7", "--dry-run"])
    assert ret == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line]
    assert len(lines) == 2
    assert lines[0] == "tests/test_00.py"
    assert lines[1] == "tests/test_07.py"

    # Run CLI with --collect-only alias
    ret_collect = main(
        ["--root", "tests", "--shard", "2", "--total-shards", "7", "--collect-only"]
    )
    assert ret_collect == 0
    captured_collect = capsys.readouterr()
    lines_collect = [line for line in captured_collect.out.strip().splitlines() if line]
    assert len(lines_collect) == 2
    assert lines_collect[0] == "tests/test_01.py"
    assert lines_collect[1] == "tests/test_08.py"


def test_partition_cli_absolute_root_inside_cwd_prints_cwd_relative_paths(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for i in range(14):
        (tests_dir / f"test_{i:02d}.py").write_text("# test\n")

    # Pass absolute root inside cwd: str(tests_dir)
    ret = main(["--root", str(tests_dir), "--shard", "1", "--total-shards", "7", "--dry-run"])
    assert ret == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line]
    assert len(lines) == 2
    assert lines[0] == "tests/test_00.py"
    assert lines[1] == "tests/test_07.py"


def test_partition_cli_absolute_root_outside_cwd_prints_absolute_paths(
    tmp_path: Path, monkeypatch, capsys
):
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    external_tests_dir = tmp_path / "external_tests"
    external_tests_dir.mkdir()
    t_files = []
    for i in range(14):
        f = external_tests_dir / f"test_{i:02d}.py"
        f.write_text("# test\n")
        t_files.append(f.resolve().as_posix())

    ret = main(
        [
            "--root",
            str(external_tests_dir),
            "--shard",
            "1",
            "--total-shards",
            "7",
            "--dry-run",
        ]
    )
    assert ret == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line]
    assert len(lines) == 2
    assert lines[0] == t_files[0]
    assert lines[1] == t_files[7]


def test_render_test_path_escapes_root_rejected(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_ok.py").write_text("# test\n")

    # Normal render
    rendered = render_test_path("test_ok.py", root=tests_dir)
    assert "test_ok.py" in rendered

    # Traversal in rel_path is rejected
    with pytest.raises(PartitionError):
        render_test_path("../escaped_test.py", root=tests_dir)


def test_partition_cli_errors_exit_2_and_print_stderr(capsys):
    # Missing required --shard
    ret = main(["--root", "tests"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "error" in captured.err.lower()

    # Invalid shard index
    ret2 = main(["--root", "tests", "--shard", "10", "--total-shards", "7"])
    assert ret2 == 2
    captured2 = capsys.readouterr()
    assert "cannot exceed total_shards" in captured2.err


def test_live_repository_test_discovery_and_seven_shard_partition():
    # Verify the real repository tests directory
    discovered = discover_test_files("tests")
    assert len(discovered) >= DEFAULT_TOTAL_SHARDS

    # Discovered paths must be root-relative POSIX paths
    assert all(not p.startswith("/") and not p.startswith("tests/") for p in discovered)
    assert "test_apple_auth.py" in discovered
    assert "unit/test_release_process.py" in discovered

    all_sharded: list[str] = []
    for s in range(1, DEFAULT_TOTAL_SHARDS + 1):
        selected = partition_test_files(
            discovered, shard=s, total_shards=DEFAULT_TOTAL_SHARDS
        )
        assert len(selected) > 0
        all_sharded.extend(selected)

    assert sorted(all_sharded) == sorted(discovered)
    assert len(all_sharded) == len(discovered)
