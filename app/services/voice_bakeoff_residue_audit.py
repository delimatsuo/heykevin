"""Residue audit: proves whether artifacts remain past their TTL.

This module only inspects and reports — it never deletes. Deciding to
delete confirmed residue is a separate, explicit, human-reviewed step.
"""

from __future__ import annotations

import dataclasses
import pathlib


@dataclasses.dataclass(frozen=True, slots=True)
class ResidueAuditResult:
    passed: bool
    checked_at_ms: int
    remaining_paths: tuple[str, ...]


def audit_residue(
    destination: pathlib.Path,
    *,
    artifact_ttl_ms: int,
    now_ms: int,
) -> ResidueAuditResult:
    remaining: list[str] = []
    if destination.exists():
        for path in sorted(destination.rglob("*")):
            if path.is_symlink():
                # A symlink is residue in its own right, evaluated by its own
                # age. lstat() reports the link entry itself and never
                # follows it, so this is correct even for dangling symlinks
                # (which is_file()/stat() would silently treat as absent).
                mtime_ms = int(path.lstat().st_mtime * 1000)
            elif path.is_file():
                mtime_ms = int(path.stat().st_mtime * 1000)
            else:
                continue
            age_ms = now_ms - mtime_ms
            if age_ms > artifact_ttl_ms:
                remaining.append(str(path))

    return ResidueAuditResult(
        passed=not remaining,
        checked_at_ms=now_ms,
        remaining_paths=tuple(remaining),
    )
