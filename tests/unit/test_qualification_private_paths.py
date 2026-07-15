"""Owner-only qualification artifact path tests."""

import os
from pathlib import Path

import pytest

import app.services.qualification_private_paths as private_paths
from app.services.qualification_private_paths import (
    PrivatePathError,
    read_private_file,
    validate_private_output_path,
    write_private_file,
)


def _private_directory(tmp_path: Path) -> Path:
    path = tmp_path / "custody"
    path.mkdir(mode=0o700)
    return path


def test_owner_only_private_file_round_trip(tmp_path: Path) -> None:
    parent = _private_directory(tmp_path)
    output = parent / "evidence.json"

    written = write_private_file(output, b"evidence", repo_root=Path.cwd())

    assert written == output
    assert output.stat().st_mode & 0o777 == 0o600
    assert read_private_file(output, repo_root=Path.cwd(), maximum_bytes=8) == b"evidence"


@pytest.mark.parametrize("mode", (0o750, 0o755, 0o777))
def test_private_output_rejects_broad_parent_permissions(
    tmp_path: Path,
    mode: int,
) -> None:
    parent = _private_directory(tmp_path)
    parent.chmod(mode)

    with pytest.raises(PrivatePathError, match="parent custody"):
        validate_private_output_path(parent / "report.json", repo_root=Path.cwd())


def test_private_input_rejects_broad_mode_hardlink_and_symlink(tmp_path: Path) -> None:
    parent = _private_directory(tmp_path)
    source = parent / "source.json"
    source.write_bytes(b"evidence")
    source.chmod(0o644)

    with pytest.raises(PrivatePathError, match="file custody"):
        read_private_file(source, repo_root=Path.cwd(), maximum_bytes=32)

    source.chmod(0o600)
    hardlink = parent / "hardlink.json"
    os.link(source, hardlink)
    with pytest.raises(PrivatePathError, match="file custody"):
        read_private_file(source, repo_root=Path.cwd(), maximum_bytes=32)
    hardlink.unlink()

    symlink = parent / "symlink.json"
    symlink.symlink_to(source)
    with pytest.raises(PrivatePathError, match="file|custody|unavailable"):
        read_private_file(symlink, repo_root=Path.cwd(), maximum_bytes=32)


def test_private_paths_reject_wrong_owner_and_repository_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _private_directory(tmp_path)
    output = parent / "report.json"
    current_uid = os.getuid()
    monkeypatch.setattr(private_paths.os, "getuid", lambda: current_uid + 1)

    with pytest.raises(PrivatePathError, match="parent custody"):
        validate_private_output_path(output, repo_root=Path.cwd())

    monkeypatch.undo()
    with pytest.raises(PrivatePathError, match="outside"):
        validate_private_output_path(output, repo_root=tmp_path)


def test_private_paths_reject_symlinked_ancestor(tmp_path: Path) -> None:
    parent = _private_directory(tmp_path)
    linked_parent = tmp_path / "linked-custody"
    linked_parent.symlink_to(parent, target_is_directory=True)

    with pytest.raises(PrivatePathError, match="symlinks"):
        validate_private_output_path(linked_parent / "report.json", repo_root=Path.cwd())


def test_private_write_rejects_parent_swap_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _private_directory(tmp_path)
    displaced = tmp_path / "original-custody"
    substitute = tmp_path / "substitute-custody"
    substitute.mkdir(mode=0o700)
    output = parent / "report.json"
    original_validate = private_paths.validate_private_output_path

    def validate_then_swap(path: Path, *, repo_root: Path) -> Path:
        canonical = original_validate(path, repo_root=repo_root)
        parent.rename(displaced)
        parent.symlink_to(substitute, target_is_directory=True)
        return canonical

    monkeypatch.setattr(
        private_paths,
        "validate_private_output_path",
        validate_then_swap,
    )

    with pytest.raises(PrivatePathError, match="ancestry|parent|unavailable"):
        write_private_file(output, b"evidence", repo_root=Path.cwd())

    assert not (substitute / output.name).exists()


def test_private_read_rejects_parent_swap_after_path_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _private_directory(tmp_path)
    source = parent / "evidence.json"
    source.write_bytes(b"original")
    source.chmod(0o600)
    displaced = tmp_path / "original-custody"
    substitute = tmp_path / "substitute-custody"
    substitute.mkdir(mode=0o700)
    substitute_source = substitute / source.name
    substitute_source.write_bytes(b"substitute")
    substitute_source.chmod(0o600)
    original_validate = private_paths._validate_external_path

    def validate_then_swap(path: Path, *, repo_root: Path) -> Path:
        canonical = original_validate(path, repo_root=repo_root)
        parent.rename(displaced)
        parent.symlink_to(substitute, target_is_directory=True)
        return canonical

    monkeypatch.setattr(private_paths, "_validate_external_path", validate_then_swap)

    with pytest.raises(PrivatePathError, match="ancestry|parent|unavailable"):
        read_private_file(source, repo_root=Path.cwd(), maximum_bytes=32)
