"""Residue audit: proves whether artifacts remain past their TTL.

This module only inspects and reports — it never deletes. Deciding to
delete confirmed residue is a separate, explicit, human-reviewed step.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib


@dataclasses.dataclass(frozen=True, slots=True)
class ResidueAuditResult:
    passed: bool
    checked_at_ms: int
    remaining_paths: tuple[str, ...]
    unreadable_paths: tuple[str, ...]


def audit_residue(
    destination: pathlib.Path,
    *,
    artifact_ttl_ms: int,
    now_ms: int,
) -> ResidueAuditResult:
    remaining: list[str] = []
    unreadable: list[str] = []

    def _on_walk_error(error: OSError) -> None:
        # os.walk() cannot list this directory's contents (e.g. permission
        # denied) — it would otherwise silently swallow the error and
        # continue as if the directory were empty. Record it as unreadable
        # instead: an audit that could not inspect everything under
        # `destination` must never report passed=True.
        unreadable.append(error.filename or str(destination))

    if destination.exists():
        for dirpath, dirnames, filenames in os.walk(
            destination, onerror=_on_walk_error, followlinks=False
        ):
            base = pathlib.Path(dirpath)
            for name in sorted(dirnames) + sorted(filenames):
                path = base / name
                try:
                    if path.is_symlink():
                        # A symlink is residue in its own right, evaluated by
                        # its own age. lstat() reports the link entry itself
                        # and never follows it, so this is correct even for
                        # dangling symlinks (which is_file()/stat() would
                        # silently treat as absent).
                        mtime_ms = int(path.lstat().st_mtime * 1000)
                    elif path.is_file():
                        mtime_ms = int(path.stat().st_mtime * 1000)
                    else:
                        continue
                except OSError:
                    # Listing `base` succeeded but inspecting this specific
                    # entry failed (e.g. removed mid-walk, or unreadable in
                    # a way os.walk's own onerror callback never saw because
                    # scandir() itself succeeded). Same fail-closed rule as
                    # above: an entry we could not inspect is not proof of
                    # anything, so it must not be silently skipped.
                    unreadable.append(str(path))
                    continue
                age_ms = now_ms - mtime_ms
                if age_ms > artifact_ttl_ms:
                    remaining.append(str(path))

    return ResidueAuditResult(
        passed=not remaining and not unreadable,
        checked_at_ms=now_ms,
        remaining_paths=tuple(sorted(remaining)),
        unreadable_paths=tuple(sorted(unreadable)),
    )
