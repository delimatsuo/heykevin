"""Race-resistant owner-only filesystem boundaries for offline qualification artifacts."""

from __future__ import annotations

import os
from pathlib import Path
import stat


class PrivatePathError(ValueError):
    """Raised when a qualification artifact does not satisfy private custody rules."""


def validate_private_output_path(path: Path, *, repo_root: Path) -> Path:
    """Return one absent external output path under an owner-only directory."""
    canonical = _validate_external_path(path, repo_root=repo_root)
    if os.path.lexists(canonical):
        raise PrivatePathError("private output path must be absent")
    _validate_private_parent(canonical.parent)
    return canonical


def read_private_file(
    path: Path,
    *,
    repo_root: Path,
    maximum_bytes: int,
) -> bytes:
    """Read one owner-only regular file without following links."""
    canonical = _validate_external_path(path, repo_root=repo_root)
    if isinstance(maximum_bytes, bool) or not isinstance(maximum_bytes, int):
        raise PrivatePathError("private input size bound is invalid")
    if maximum_bytes <= 0:
        raise PrivatePathError("private input size bound is invalid")
    _validate_private_parent(canonical.parent)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(canonical, flags)
    except OSError as exc:
        raise PrivatePathError("private input file is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        _validate_private_file_metadata(metadata, maximum_bytes=maximum_bytes)
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > maximum_bytes:
            raise PrivatePathError("private input file exceeds its size bound")
        return value
    finally:
        os.close(descriptor)


def write_private_file(path: Path, payload: bytes, *, repo_root: Path) -> Path:
    """Create and durably write one owner-only external file."""
    if not isinstance(payload, bytes):
        raise TypeError("private output payload must be bytes")
    canonical = validate_private_output_path(path, repo_root=repo_root)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(canonical, flags, 0o600)
    except OSError as exc:
        raise PrivatePathError("private output path is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        _validate_private_file_metadata(metadata, maximum_bytes=None)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise PrivatePathError("private output write did not make progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    directory = os.open(canonical.parent, directory_flags)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return canonical


def _validate_external_path(path: Path, *, repo_root: Path) -> Path:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or path.name in {"", ".", ".."}
    ):
        raise PrivatePathError("private artifact path is invalid")
    for ancestor in path.parents:
        try:
            metadata = os.lstat(ancestor)
        except OSError as exc:
            raise PrivatePathError("private artifact ancestry is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PrivatePathError("private artifact ancestry must not contain symlinks")
    parent = path.parent.resolve(strict=True)
    canonical = parent / path.name
    try:
        canonical.relative_to(repo_root.resolve(strict=True))
    except ValueError:
        return canonical
    raise PrivatePathError("private artifact path must be outside the repository")


def _validate_private_parent(parent: Path) -> None:
    try:
        metadata = os.lstat(parent)
    except OSError as exc:
        raise PrivatePathError("private artifact parent is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise PrivatePathError("private artifact parent custody is invalid")


def _validate_private_file_metadata(
    metadata: os.stat_result,
    *,
    maximum_bytes: int | None,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise PrivatePathError("private artifact file custody is invalid")
    if maximum_bytes is not None and metadata.st_size > maximum_bytes:
        raise PrivatePathError("private input file exceeds its size bound")
