#!/usr/bin/env python3
"""Deterministic test partitioner for Kevin CI."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

DEFAULT_TOTAL_SHARDS = 7
CONTROL_CHARS = set(range(0, 32)) | {127}


class PartitionError(Exception):
    """Raised when test discovery or partitioning contracts are violated."""


def _has_control_chars(text: str) -> bool:
    return any(ord(c) in CONTROL_CHARS for c in text)


def _validate_root_path_string(root_str: str) -> None:
    if not root_str:
        raise PartitionError("Root path cannot be empty.")
    if "\\" in root_str:
        raise PartitionError(f"Backslashes are not permitted in root path: {root_str!r}")
    if _has_control_chars(root_str):
        raise PartitionError(f"Control characters are not permitted in root path: {root_str!r}")
    p = Path(root_str)
    if ".." in p.parts:
        raise PartitionError(f"Directory traversal ('..') is not permitted in root path: {root_str!r}")


def discover_test_files(root: str | Path = "tests") -> list[str]:
    """Recursively discover and validate regular Python test files under root.

    Root arguments may be absolute or relative and must support pytest tmp_path.
    Returns sorted POSIX paths relative to the supplied root directory (e.g.
    'test_apple_auth.py' or 'unit/test_release_process.py'), never absolute
    and never prefixed by the root directory name.

    Missing, non-directory, empty, symlink roots, raw backslashes/control characters/
    traversal components, and symlinks anywhere below root are rejected.
    """
    root_str = str(root)
    _validate_root_path_string(root_str)

    root_path = Path(root_str)
    if not root_path.exists():
        raise PartitionError(f"Root path does not exist: {root_str}")
    if root_path.is_symlink() or os.path.islink(root_str):
        raise PartitionError(f"Root path cannot be a symlink: {root_str}")
    if not root_path.is_dir():
        raise PartitionError(f"Root path must be a directory: {root_str}")

    found_files: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(str(root_path), followlinks=False):
        if os.path.islink(dirpath):
            raise PartitionError(f"Symlink directory rejected: {dirpath}")

        dir_p = Path(dirpath)
        for d in sorted(dirnames):
            if "\\" in d:
                raise PartitionError(f"Backslashes are not permitted in directory name: {d!r}")
            if _has_control_chars(d):
                raise PartitionError(f"Control characters are not permitted in directory name: {d!r}")
            if d in ("..", "."):
                raise PartitionError(f"Directory traversal segment not permitted: {d!r}")
            sub_p = dir_p / d
            sub_str = str(sub_p)
            if sub_p.is_symlink() or os.path.islink(sub_str):
                raise PartitionError(f"Symlink directory rejected: {sub_p.as_posix()}")

        for f in sorted(filenames):
            if "\\" in f:
                raise PartitionError(f"Backslashes are not permitted in file name: {f!r}")
            if _has_control_chars(f):
                raise PartitionError(f"Control characters are not permitted in file name: {f!r}")
            if f in ("..", "."):
                raise PartitionError(f"Directory traversal segment not permitted: {f!r}")

            file_p = dir_p / f
            file_str = str(file_p)
            if file_p.is_symlink() or os.path.islink(file_str):
                raise PartitionError(f"Symlink file rejected: {file_p.as_posix()}")

            if not file_p.is_file():
                continue

            # Match test_*.py OR *_test.py
            if f.endswith(".py") and (f.startswith("test_") or f.endswith("_test.py")):
                rel_posix = file_p.relative_to(root_path).as_posix()
                if rel_posix in found_files:
                    raise PartitionError(f"Duplicate test file encountered: {rel_posix}")
                found_files.add(rel_posix)

    if not found_files:
        raise PartitionError(f"No test files found under root: {root_str}")

    return sorted(found_files)


def _validate_individual_test_file(file_str: str) -> None:
    if not isinstance(file_str, str):
        raise PartitionError(f"Test file entry must be a string, got {type(file_str).__name__}")
    if not file_str or not file_str.strip():
        raise PartitionError("Test file entry cannot be empty.")
    if "\\" in file_str:
        raise PartitionError(f"Backslashes are not permitted in test file: {file_str!r}")
    if _has_control_chars(file_str):
        raise PartitionError(f"Control characters are not permitted in test file: {file_str!r}")
    if file_str.startswith("/"):
        raise PartitionError(f"Absolute paths are not permitted: {file_str!r}")

    p = Path(file_str)
    if p.is_absolute():
        raise PartitionError(f"Absolute paths are not permitted: {file_str!r}")
    if ".." in p.parts or "." in p.parts:
        raise PartitionError(f"Directory traversal or '.' segments are not permitted: {file_str!r}")
    if file_str.startswith("./") or "/./" in file_str or "//" in file_str or file_str.endswith("/."):
        raise PartitionError(f"Test file path must be normalized POSIX relative: {file_str!r}")

    filename = p.name
    if not (filename.endswith(".py") and (filename.startswith("test_") or filename.endswith("_test.py"))):
        raise PartitionError(
            f"Test file does not match naming convention (test_*.py or *_test.py): {file_str!r}"
        )


def partition_test_files(
    test_files: Sequence[str], shard: int, total_shards: int = DEFAULT_TOTAL_SHARDS
) -> list[str]:
    """Partition test files round-robin across total_shards, returning files for shard (1-indexed).

    Independently validates every supplied file string: POSIX relative test filename only
    (test_*.py or *_test.py), no absolute/backslash/control/traversal/empty/duplicates,
    no bool shard counts, and detects existing aliases resolving to one target.
    Canonicalizes sorted inputs and validates coverage invariants.
    """
    if isinstance(total_shards, bool) or not isinstance(total_shards, int) or total_shards <= 0:
        raise PartitionError(f"total_shards must be a positive integer, got {total_shards!r}")
    if isinstance(shard, bool) or not isinstance(shard, int) or shard <= 0:
        raise PartitionError(f"shard must be a positive integer, got {shard!r}")
    if shard > total_shards:
        raise PartitionError(
            f"shard ({shard}) cannot exceed total_shards ({total_shards})"
        )

    if not isinstance(test_files, (list, tuple, set)):
        raise PartitionError(f"test_files must be a sequence of paths, got {type(test_files).__name__}")

    if not test_files:
        raise PartitionError("Cannot partition empty list of test files.")

    seen_strings: set[str] = set()
    seen_real_targets: dict[tuple[int, int], str] = {}
    seen_real_paths: dict[str, str] = {}

    for f in test_files:
        _validate_individual_test_file(f)
        if f in seen_strings:
            raise PartitionError(f"Duplicate test file in input list: {f!r}")
        seen_strings.add(f)

        file_p = Path(f)
        if file_p.exists():
            if file_p.is_symlink() or os.path.islink(f):
                raise PartitionError(f"Symlink test file rejected: {f!r}")
            try:
                st = file_p.stat()
                dev_ino = (st.st_dev, st.st_ino)
                if dev_ino in seen_real_targets:
                    raise PartitionError(
                        f"Test files resolve to the same target (alias detected): {f!r} and {seen_real_targets[dev_ino]!r}"
                    )
                seen_real_targets[dev_ino] = f
            except OSError:
                pass

            try:
                real_p = str(file_p.resolve())
                if real_p in seen_real_paths:
                    raise PartitionError(
                        f"Test files resolve to the same real path (alias detected): {f!r} and {seen_real_paths[real_p]!r}"
                    )
                seen_real_paths[real_p] = f
            except OSError:
                pass

    num_files = len(test_files)
    if num_files < total_shards:
        raise PartitionError(
            f"Cannot partition {num_files} test files into {total_shards} shards without empty shards."
        )

    sorted_files = sorted(test_files)

    # Invariant validation across all shards
    all_shards: list[list[str]] = []
    assigned_seen: set[str] = set()
    for s_idx in range(1, total_shards + 1):
        s_files = [f for i, f in enumerate(sorted_files) if (i % total_shards) == (s_idx - 1)]
        if not s_files:
            raise PartitionError(f"Shard {s_idx}/{total_shards} would be empty.")
        for f in s_files:
            if f in assigned_seen:
                raise PartitionError(f"Test file {f} assigned to multiple shards.")
            assigned_seen.add(f)
        all_shards.append(s_files)

    if len(assigned_seen) != num_files or set(sorted_files) != assigned_seen:
        raise PartitionError(
            "Partition failed coverage invariant: not exactly-once complete and disjoint."
        )

    return all_shards[shard - 1]


def render_test_path(rel_path: str, root: str | Path = "tests") -> str:
    """Render a root-relative test path for CLI execution.

    Joins rel_path back under root.
    - If the joined candidate is under the current working directory, returns
      a current-working-directory-relative POSIX path (e.g. 'tests/test_foo.py').
    - If the joined candidate is outside current working directory, returns
      its absolute POSIX path.
    - Fails closed if the rendered path escapes root.
    """
    root_str = str(root)
    _validate_root_path_string(root_str)
    _validate_individual_test_file(rel_path)

    root_p = Path(root_str)
    candidate_p = root_p / rel_path

    # Validate that candidate does not escape root
    try:
        abs_root = root_p.resolve()
        abs_candidate = candidate_p.resolve()
    except OSError as exc:
        raise PartitionError(f"Cannot resolve test path {candidate_p}: {exc}") from exc

    try:
        abs_candidate.relative_to(abs_root)
    except ValueError as exc:
        raise PartitionError(
            f"Rendered test path {rel_path!r} escapes root {root_str!r}"
        ) from exc

    # Check if joined candidate is under current working directory
    try:
        cwd = Path.cwd().resolve()
    except OSError as exc:
        raise PartitionError(f"Cannot determine current working directory: {exc}") from exc

    try:
        rel_to_cwd = abs_candidate.relative_to(cwd)
        return rel_to_cwd.as_posix()
    except ValueError:
        return abs_candidate.as_posix()


def render_test_paths(
    test_files: Sequence[str], root: str | Path = "tests"
) -> list[str]:
    """Render a sequence of root-relative test paths for CLI execution."""
    return [render_test_path(f, root=root) for f in test_files]


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deterministic test suite partitioner for Kevin CI.",
        exit_on_error=False,
    )
    parser.add_argument(
        "--root",
        default="tests",
        help="Root directory for test discovery (default: tests)",
    )
    parser.add_argument(
        "--shard",
        type=int,
        required=True,
        help="1-based shard index",
    )
    parser.add_argument(
        "--total-shards",
        type=int,
        default=DEFAULT_TOTAL_SHARDS,
        help=f"Total number of shards (default: {DEFAULT_TOTAL_SHARDS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print selected test files without executing",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Alias for --dry-run",
    )

    try:
        return parser.parse_args(args)
    except (argparse.ArgumentError, SystemExit) as exc:
        raise PartitionError(f"CLI argument error: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    try:
        opts = parse_args(argv)
        files = discover_test_files(opts.root)
        selected = partition_test_files(
            files, shard=opts.shard, total_shards=opts.total_shards
        )
        rendered_files = render_test_paths(selected, root=opts.root)
        for path in rendered_files:
            sys.stdout.write(f"{path}\n")
        sys.stdout.flush()
        return 0
    except PartitionError as err:
        sys.stderr.write(f"partition_tests error: {err}\n")
        sys.stderr.flush()
        return 2
    except Exception as err:
        sys.stderr.write(f"partition_tests unexpected error: {err}\n")
        sys.stderr.flush()
        return 2


if __name__ == "__main__":
    sys.exit(main())
