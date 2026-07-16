from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha1, sha256
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
import app.services.qualification_identity as identity_module
import scripts.launch_qualification as launcher_module

from app.services.qualification_identity import (
    EXECUTION_DEPENDENCY_PATHS,
    DependencyIdentity,
    IdentityError,
    SourceIdentity,
    STARTUP_FLAG_NAMES,
    STARTUP_MARKER_ENV,
    TRUSTED_STARTUP_SCHEMA_ID,
    canonical_json_bytes,
    capture_environment_identity,
    capture_source_identity,
    capture_trusted_startup_identity,
    ledger_location_sha256,
    read_identity_bound_file,
    verify_attempt_authorization,
    verify_campaign_approval,
)


NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
PREREGISTRATION_SHA = "a" * 64
SOURCE_SHA = "b" * 40
KEY_ID = "qualification-reviewer-v1"


def _trusted_startup_flags() -> dict[str, int | bool]:
    return {
        "bytes_warning": 0,
        "debug": 0,
        "dev_mode": False,
        "dont_write_bytecode": 1,
        "hash_randomization": 1,
        "ignore_environment": 1,
        "inspect": 0,
        "int_max_str_digits": 4300,
        "interactive": 0,
        "isolated": 1,
        "no_site": 1,
        "no_user_site": 1,
        "optimize": 0,
        "quiet": 0,
        "safe_path": True,
        "utf8_mode": 0,
        "verbose": 0,
        "warn_default_encoding": 0,
    }


def _interpreter_report() -> dict[str, object]:
    closure: dict[str, object] = {
        "schema_id": "gate_0b_native_runtime_closure_v1",
        "regular_file_count": 2,
        "regular_files_sha256": "5" * 64,
        "virtual_dependency_count": 1,
        "virtual_dependencies_sha256": "6" * 64,
        "system_loader_identity_sha256": "7" * 64,
    }
    closure["closure_sha256"] = sha256(canonical_json_bytes(closure)).hexdigest()
    report: dict[str, object] = {
        "schema_id": "gate_0b_interpreter_installation_v2",
        "python_executable_sha256": sha256(b"python").hexdigest(),
        "stdlib_source_bytecode_sha256": "1" * 64,
        "stdlib_source_bytecode_count": 1,
        "stdlib_archive_sha256": "2" * 64,
        "stdlib_archive_count": 0,
        "native_extension_sha256": "3" * 64,
        "native_extension_count": 1,
        "native_runtime_closure": closure,
    }
    report["installation_sha256"] = sha256(canonical_json_bytes(report)).hexdigest()
    return report


def _runtime_site_report() -> dict[str, object]:
    report: dict[str, object] = {
        "schema_id": "gate_0b_runtime_site_packages_v1",
        "source_count": 1,
        "bytecode_count": 0,
        "native_extension_count": 1,
        "metadata_data_count": 1,
        "file_count": 3,
        "files_sha256": "4" * 64,
    }
    report["manifest_sha256"] = sha256(canonical_json_bytes(report)).hexdigest()
    return report


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.mark.parametrize("module", (identity_module, launcher_module))
def test_native_dependency_parser_handles_linux_ldd_output(
    tmp_path: Path,
    module: object,
) -> None:
    libpython = tmp_path / "libpython3.12.so.1.0"
    loader = tmp_path / "ld-linux-x86-64.so.2"
    libpython.write_bytes(b"python")
    loader.write_bytes(b"loader")
    output = (
        "linux-vdso.so.1 (0x00007fff)\n"
        f"libpython3.12.so.1.0 => {libpython} (0x00007f00)\n"
        f"{loader} (0x00007f01)\n"
    )

    resolved, virtual = module._parse_linux_dependencies(output)

    assert resolved == [libpython, loader]
    assert virtual == {"linux-vdso.so.1"}


@pytest.mark.parametrize(
    ("module", "error_type"),
    (
        (identity_module, IdentityError),
        (launcher_module, launcher_module.BootstrapError),
    ),
)
def test_native_dependency_parser_rejects_missing_linux_dependency(
    module: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type, match="native runtime dependency is unavailable"):
        module._parse_linux_dependencies("libmissing.so =>\tnot found\n")


@pytest.mark.parametrize("module", (identity_module, launcher_module))
def test_native_dependency_parser_handles_darwin_fat_binary_output(
    tmp_path: Path,
    module: object,
) -> None:
    binary = tmp_path / "libnative.dylib"
    executable = tmp_path / "bin" / "python"
    loader_dependency = tmp_path / "libloader.dylib"
    executable_dependency = executable.parent / "libexecutable.dylib"
    rpath_dependency = tmp_path / "librpath.dylib"
    absolute_dependency = tmp_path / "libabsolute.dylib"
    same_name_dependency = tmp_path / "other" / binary.name
    executable.parent.mkdir()
    same_name_dependency.parent.mkdir()
    for path in (
        binary,
        executable,
        loader_dependency,
        executable_dependency,
        rpath_dependency,
        absolute_dependency,
        same_name_dependency,
    ):
        path.write_bytes(path.name.encode("ascii"))
    output = (
        f"{binary} (architecture x86_64):\n"
        f"\t{binary} (compatibility version 0.0.0, current version 0.0.0)\n"
        "\t@loader_path/libloader.dylib (compatibility version 1.0.0)\n"
        "\t@executable_path/libexecutable.dylib (compatibility version 1.0.0)\n"
        "\t@rpath/librpath.dylib (compatibility version 1.0.0)\n"
        f"\t{absolute_dependency} (compatibility version 1.0.0)\n"
        f"\t{same_name_dependency} (compatibility version 1.0.0)\n"
        "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        f"{binary} (architecture arm64):\n"
        "\t@rpath/libmissing.dylib (compatibility version 1.0.0)\n"
        "\tlibrelative-install-name.dylib (compatibility version 1.0.0)\n"
    )

    resolved, virtual = module._parse_darwin_dependencies(
        output,
        binary=binary,
        executable=executable,
    )

    assert resolved == [
        loader_dependency,
        executable_dependency,
        rpath_dependency,
        absolute_dependency,
        same_name_dependency,
    ]
    assert virtual == {
        "/usr/lib/libSystem.B.dylib",
        "@rpath/libmissing.dylib",
        "librelative-install-name.dylib",
    }


def _redacted_source_identity_for(
    relative_path: str,
    contents: bytes,
) -> dict[str, object]:
    dependencies = {
        sha256(path.encode("utf-8")).hexdigest(): {
            "worktree_sha256": sha256(f"fixture:{path}".encode("utf-8")).hexdigest(),
            "git_blob_id": "1" * 40,
        }
        for path in EXECUTION_DEPENDENCY_PATHS
    }
    dependencies[sha256(relative_path.encode("utf-8")).hexdigest()] = {
        "worktree_sha256": sha256(contents).hexdigest(),
        "git_blob_id": sha1(
            b"blob "
            + str(len(contents)).encode("ascii")
            + b"\0"
            + contents,
            usedforsecurity=False,
        ).hexdigest(),
    }
    return {
        "source_sha": SOURCE_SHA,
        "clean": True,
        "dependencies": dependencies,
    }


def test_identity_bound_file_reads_captured_regular_file(tmp_path: Path) -> None:
    relative = "config/qualification/gate_0b_approval_root.ed25519.pub"
    contents = b"a" * 32
    root = tmp_path / "repo"
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(contents)

    loaded = read_identity_bound_file(
        root,
        relative_path=relative,
        source_identity=_redacted_source_identity_for(relative, contents),
        maximum_bytes=32,
    )

    assert loaded == contents


def test_identity_bound_file_rejects_content_replaced_after_capture(
    tmp_path: Path,
) -> None:
    relative = "config/qualification/gate_0b_approval_root.ed25519.pub"
    captured = b"a" * 32
    root = tmp_path / "repo"
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"b" * 32)

    with pytest.raises(IdentityError, match="source identity mismatch"):
        read_identity_bound_file(
            root,
            relative_path=relative,
            source_identity=_redacted_source_identity_for(relative, captured),
            maximum_bytes=32,
        )


def test_identity_bound_file_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    relative = "config/qualification/gate_0b_approval_root.ed25519.pub"
    contents = b"a" * 32
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "gate_0b_approval_root.ed25519.pub").write_bytes(contents)
    (root / "config" / "qualification").symlink_to(external)

    with pytest.raises(IdentityError, match="unavailable"):
        read_identity_bound_file(
            root,
            relative_path=relative,
            source_identity=_redacted_source_identity_for(relative, contents),
            maximum_bytes=32,
        )


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "qualification@example.invalid")
    _git(repo, "config", "user.name", "Qualification Test")
    (repo / "tracked.py").write_text("VALUE = 1\n")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _interpreter_fixture(
    tmp_path: Path,
) -> tuple[Path, tuple[str, ...], dict[str, Path]]:
    executable = tmp_path / "runtime" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"python-executable")
    stdlib = tmp_path / "runtime" / "lib" / "python3.12"
    cache = stdlib / "package" / "__pycache__"
    cache.mkdir(parents=True)
    source = stdlib / "package" / "module.py"
    source.write_bytes(b"VALUE = 1\n")
    bytecode = cache / "module.cpython-312.pyc"
    bytecode.write_bytes(b"bytecode")
    native = stdlib / "lib-dynload"
    native.mkdir()
    extension = native / "_runtime.cpython-312-darwin.so"
    extension.write_bytes(b"native-extension")
    archive = tmp_path / "runtime" / "lib" / "python312.zip"
    archive.write_bytes(b"stdlib-archive")
    return executable, (str(archive), str(stdlib), str(native)), {
        "bytecode": bytecode,
        "executable": executable,
        "native": extension,
        "source": source,
    }


def _runtime_site_fixture(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    site_packages = tmp_path / "runtime" / "site-packages"
    package = site_packages / "approved_package"
    cache = package / "__pycache__"
    cache.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_bytes(b"VALUE = 1\n")
    bytecode = cache / "__init__.cpython-312.pyc"
    bytecode.write_bytes(b"bytecode")
    native = package / "approved_native.so"
    native.write_bytes(b"native")
    metadata = site_packages / "approved_package-1.0.0.dist-info" / "METADATA"
    metadata.parent.mkdir()
    metadata.write_bytes(b"Name: approved-package\nVersion: 1.0.0\n")
    data = site_packages / "approved_package-1.0.0.dist-info" / "entry_points.txt"
    data.write_bytes(b"[approved]\nfixture = approved_package:VALUE\n")
    return site_packages, {
        "bytecode": bytecode,
        "data": data,
        "metadata": metadata,
        "native": native,
        "source": source,
    }


def _key_pair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, public


def _signed(private: Ed25519PrivateKey, payload: dict[str, object]) -> dict[str, object]:
    return {
        "key_id": KEY_ID,
        "payload": payload,
        "signature": base64.b64encode(private.sign(canonical_json_bytes(payload))).decode("ascii"),
    }


def _campaign_payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_id": "gate_0b_campaign_approval_v1",
        "scope": "gate_0b_purpose_recorded_turn_assembly",
        "campaign_id": "campaign_001",
        "authorization_id": "authorization_001",
        "nonce": "nonce_001",
        "preregistration_sha256": PREREGISTRATION_SHA,
        "source_sha": SOURCE_SHA,
        "issued_at": "2026-07-15T14:59:00Z",
        "expires_at": "2026-07-15T16:00:00Z",
        "max_attempts": 3,
        "max_provider_requests": 384,
        "max_cost_microusd": 30_000_000,
        "ledger_instance_id": "ledger_instance_1",
        "ledger_custodian_key_id": "ledger_custodian_1",
        "ledger_custodian_public_key_sha256": "f" * 64,
        "ledger_location_sha256": "c" * 64,
        "real_caller_data_authorized": False,
        "runtime_wiring_authorized": False,
        "deployment_authorized": False,
        "production_authorized": False,
        "release_authorized": False,
    }
    values.update(overrides)
    return values


def _attempt_payload(attempt_index: int = 1, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_id": "gate_0b_attempt_authorization_v1",
        "campaign_id": "campaign_001",
        "authorization_id": "authorization_001",
        "attempt_id": f"attempt_{attempt_index:03d}",
        "attempt_index": attempt_index,
        "prior_attempt_id": None,
        "outage_enum": None,
        "preregistration_sha256": PREREGISTRATION_SHA,
        "source_sha": SOURCE_SHA,
        "issued_at": "2026-07-15T14:59:00Z",
        "expires_at": "2026-07-15T16:00:00Z",
        "provider_request_reservation": 128,
        "cost_reservation_microusd": 10_000_000,
    }
    values.update(overrides)
    return values


def test_clean_source_identity_binds_head_blob_and_file(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")

    identity = capture_source_identity(
        repo,
        expected_source_sha=head,
        dependency_paths=("tracked.py",),
    )

    assert identity.source_sha == head
    assert identity.clean is True
    assert identity.dependencies["tracked.py"].worktree_sha256
    assert identity.dependencies["tracked.py"].git_blob_id == _git(
        repo,
        "rev-parse",
        "HEAD:tracked.py",
    )
    redacted = identity.redacted_report_dict()
    assert "tracked.py" not in canonical_json_bytes(redacted).decode("ascii")
    assert sha256(b"tracked.py").hexdigest() in redacted["dependencies"]


@pytest.mark.parametrize("dirty_kind", ["unstaged", "staged", "untracked"])
def test_source_identity_rejects_every_dirty_tree_state(tmp_path: Path, dirty_kind: str) -> None:
    repo = _git_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    if dirty_kind == "untracked":
        (repo / "shadow.py").write_text("VALUE = 2\n")
    else:
        (repo / "tracked.py").write_text("VALUE = 2\n")
        if dirty_kind == "staged":
            _git(repo, "add", "tracked.py")

    with pytest.raises(IdentityError, match="worktree is not clean"):
        capture_source_identity(
            repo,
            expected_source_sha=head,
            dependency_paths=("tracked.py",),
        )


def test_source_identity_rejects_sha_blob_and_symlink_drift(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)

    with pytest.raises(IdentityError, match="source SHA mismatch"):
        capture_source_identity(
            repo,
            expected_source_sha="0" * 40,
            dependency_paths=("tracked.py",),
        )

    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n")
    (repo / "linked.py").symlink_to(outside)
    _git(repo, "add", "linked.py")
    _git(repo, "commit", "-m", "link")
    linked_head = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(IdentityError, match="symlink"):
        capture_source_identity(
            repo,
            expected_source_sha=linked_head,
            dependency_paths=("linked.py",),
        )


def test_environment_identity_binds_exact_runtime_and_imports() -> None:
    identity = capture_environment_identity(
        repo_root=Path.cwd(),
        expected_python="3.12.13",
        expected_uv="0.11.7",
        import_names=("websockets", "app.utils.audio"),
    )

    assert identity.python_version == "3.12.13"
    assert identity.uv_version == "0.11.7"
    assert identity.python_executable_sha256
    assert identity.uv_executable_sha256
    assert identity.python_executable_location_sha256
    assert identity.uv_executable_location_sha256
    assert identity.interpreter_installation["installation_sha256"]
    assert identity.runtime_site_packages_manifest["manifest_sha256"]
    assert identity.monotonic_clock_implementation
    assert identity.monotonic_clock_resolution_ns > 0
    assert isinstance(identity.bytecode_write_disabled, bool)
    assert identity.codec_golden_sha256
    assert identity.lock_sha256
    assert set(identity.import_sha256) == {"websockets", "app.utils.audio"}
    assert identity.distribution_files_sha256["websockets"]
    assert all("/" not in value for value in identity.redacted_report_dict().values() if isinstance(value, str))


def test_launcher_and_runtime_capture_the_same_complete_path_redacted_interpreter_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, stdlib_paths, _files = _interpreter_fixture(tmp_path)
    closure = _interpreter_report()["native_runtime_closure"]
    allowed = {str(executable.resolve()): sha256(executable.read_bytes()).hexdigest()}
    monkeypatch.setattr(
        launcher_module,
        "_capture_native_runtime_closure",
        lambda *_args, **_kwargs: (closure, allowed),
    )
    monkeypatch.setattr(
        identity_module,
        "_capture_native_runtime_closure",
        lambda *_args, **_kwargs: (closure, allowed),
    )
    monkeypatch.setattr(
        launcher_module,
        "_require_loaded_native_images_within_closure",
        lambda _allowed: None,
    )
    monkeypatch.setattr(
        identity_module,
        "_require_loaded_native_images_within_closure",
        lambda _allowed: None,
    )

    launcher = launcher_module._capture_interpreter_installation_identity(
        stdlib_paths=stdlib_paths,
        python_executable=str(executable),
    )
    runtime = identity_module.capture_interpreter_installation_identity(
        stdlib_paths=stdlib_paths,
        python_executable=str(executable),
    )

    assert launcher == runtime
    assert launcher["schema_id"] == "gate_0b_interpreter_installation_v2"
    assert launcher["stdlib_source_bytecode_count"] == 2
    assert launcher["stdlib_archive_count"] == 1
    assert launcher["native_extension_count"] == 1
    assert launcher["installation_sha256"]
    assert str(tmp_path) not in canonical_json_bytes(launcher).decode("ascii")


@pytest.mark.parametrize("component", ["executable", "source", "bytecode", "native"])
def test_interpreter_installation_identity_detects_every_runtime_byte_drift(
    tmp_path: Path,
    component: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, stdlib_paths, files = _interpreter_fixture(tmp_path)
    closure = _interpreter_report()["native_runtime_closure"]
    monkeypatch.setattr(
        launcher_module,
        "_capture_native_runtime_closure",
        lambda *_args, **_kwargs: (closure, {}),
    )
    monkeypatch.setattr(
        launcher_module,
        "_require_loaded_native_images_within_closure",
        lambda _allowed: None,
    )
    before = launcher_module._capture_interpreter_installation_identity(
        stdlib_paths=stdlib_paths,
        python_executable=str(executable),
    )
    files[component].write_bytes(files[component].read_bytes() + b"-drift")

    after = launcher_module._capture_interpreter_installation_identity(
        stdlib_paths=stdlib_paths,
        python_executable=str(executable),
    )

    assert after["installation_sha256"] != before["installation_sha256"]


def test_interpreter_installation_identity_detects_linked_core_runtime_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, stdlib_paths, _files = _interpreter_fixture(tmp_path)
    core_runtime = tmp_path / "runtime" / "lib" / "Python"
    core_runtime.write_bytes(b"core-runtime-v1")

    def closure(*_args, **_kwargs):
        records_sha256 = sha256(core_runtime.read_bytes()).hexdigest()
        unsigned: dict[str, object] = {
            "schema_id": "gate_0b_native_runtime_closure_v1",
            "regular_file_count": 2,
            "regular_files_sha256": records_sha256,
            "virtual_dependency_count": 0,
            "virtual_dependencies_sha256": sha256(b"[]").hexdigest(),
            "system_loader_identity_sha256": "7" * 64,
        }
        return (
            {
                **unsigned,
                "closure_sha256": sha256(canonical_json_bytes(unsigned)).hexdigest(),
            },
            {},
        )

    monkeypatch.setattr(
        launcher_module,
        "_capture_native_runtime_closure",
        closure,
    )
    monkeypatch.setattr(
        launcher_module,
        "_require_loaded_native_images_within_closure",
        lambda _allowed: None,
    )
    before = launcher_module._capture_interpreter_installation_identity(
        stdlib_paths=stdlib_paths,
        python_executable=str(executable),
    )

    core_runtime.write_bytes(b"core-runtime-v2")
    after = launcher_module._capture_interpreter_installation_identity(
        stdlib_paths=stdlib_paths,
        python_executable=str(executable),
    )

    assert after["native_runtime_closure"] != before["native_runtime_closure"]
    assert after["installation_sha256"] != before["installation_sha256"]


def test_loaded_native_image_outside_closure_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = tmp_path / "approved.so"
    injected = tmp_path / "injected.so"
    approved.write_bytes(b"approved")
    injected.write_bytes(b"injected")
    monkeypatch.setattr(
        identity_module,
        "_loaded_native_image_paths",
        lambda: (str(approved), str(injected)),
    )

    with pytest.raises(IdentityError, match="outside the approved closure"):
        identity_module._require_loaded_native_images_within_closure(
            {
                str(approved.resolve()): sha256(approved.read_bytes()).hexdigest(),
            }
        )


@pytest.mark.parametrize("variable", ("LD_PRELOAD", "DYLD_INSERT_LIBRARIES"))
def test_native_loader_environment_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    monkeypatch.setenv(variable, "/unapproved/native-hook")

    with pytest.raises(IdentityError, match="native loader environment"):
        identity_module._reject_native_loader_environment()


def test_launcher_and_runtime_capture_the_same_complete_path_redacted_site_manifest(
    tmp_path: Path,
) -> None:
    site_packages, _files = _runtime_site_fixture(tmp_path)

    launcher = launcher_module._capture_runtime_site_packages_identity(
        str(site_packages)
    )
    runtime = identity_module.capture_runtime_site_packages_identity(site_packages)

    assert launcher == runtime
    assert launcher["schema_id"] == "gate_0b_runtime_site_packages_v1"
    assert launcher["source_count"] == 1
    assert launcher["bytecode_count"] == 1
    assert launcher["native_extension_count"] == 1
    assert launcher["metadata_data_count"] == 2
    assert launcher["file_count"] == 5
    assert launcher["manifest_sha256"]
    assert str(tmp_path) not in canonical_json_bytes(launcher).decode("ascii")


@pytest.mark.parametrize(
    "component",
    ["source", "bytecode", "native", "metadata", "data"],
)
def test_runtime_site_manifest_detects_every_executable_dependency_byte_drift(
    tmp_path: Path,
    component: str,
) -> None:
    site_packages, files = _runtime_site_fixture(tmp_path)
    before = launcher_module._capture_runtime_site_packages_identity(
        str(site_packages)
    )
    files[component].write_bytes(files[component].read_bytes() + b"-drift")

    after = launcher_module._capture_runtime_site_packages_identity(
        str(site_packages)
    )

    assert after["manifest_sha256"] != before["manifest_sha256"]


def test_environment_identity_rejects_self_asserted_container_image_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUALIFICATION_CONTAINER_IMAGE_DIGEST", "sha256:" + "a" * 64)

    with pytest.raises(IdentityError, match="self-asserted runtime image"):
        capture_environment_identity(
            repo_root=Path.cwd(),
            expected_python="3.12.13",
            expected_uv="0.11.7",
            import_names=("websockets", "app.utils.audio"),
        )


def test_trusted_startup_identity_revalidates_live_flags_paths_and_bounded_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    runtime_site = tmp_path / "runtime" / "site-packages"
    repo_root.mkdir()
    runtime_site.mkdir(parents=True)
    executable = tmp_path / "runtime" / "bin" / "python"
    executable.parent.mkdir()
    executable.write_bytes(b"python")
    pth_path = runtime_site / "recognized.pth"
    pth_path.write_text("import should_never_execute\n", encoding="utf-8")
    hook_path = runtime_site / "sitecustomize.py"
    hook_path.write_text("raise AssertionError('never execute')\n", encoding="utf-8")
    effective_sys_path = [str(repo_root.resolve()), str(runtime_site.resolve())]
    pycache_prefix = tmp_path / "disabled-pycache"
    flags = _trusted_startup_flags()
    dependencies = {
        path: DependencyIdentity(
            worktree_sha256="1" * 64,
            git_blob_id="2" * 40,
        )
        for path in EXECUTION_DEPENDENCY_PATHS
    }
    source = SourceIdentity(
        source_sha="b" * 40,
        clean=True,
        dependencies=dependencies,
    )
    source_marker = {
        "source_sha": source.source_sha,
        "clean": True,
        "dependencies": {
            path: {
                "worktree_sha256": dependency.worktree_sha256,
                "git_blob_id": dependency.git_blob_id,
            }
            for path, dependency in dependencies.items()
        },
    }
    interpreter = _interpreter_report()
    runtime_site_manifest = _runtime_site_report()
    marker = {
        "schema_id": TRUSTED_STARTUP_SCHEMA_ID,
        "target": "verify-environment",
        "startup_flags": flags,
        "bytecode_write_disabled": True,
        "pycache_prefix": str(pycache_prefix.resolve()),
        "repo_root": str(repo_root.resolve()),
        "python_executable": str(executable.resolve()),
        "runtime_site_packages": str(runtime_site.resolve()),
        "effective_sys_path": effective_sys_path,
        "neutralized_environment": ["PYTHONHOME", "PYTHONPATH"],
        "runtime_pth_files_sha256": {
            str(pth_path.resolve()): sha256(pth_path.read_bytes()).hexdigest()
        },
        "ignored_startup_hook_files_sha256": {
            str(hook_path.resolve()): sha256(hook_path.read_bytes()).hexdigest()
        },
        "source_preflight": source_marker,
        "interpreter_installation": interpreter,
        "runtime_site_packages_manifest": runtime_site_manifest,
    }
    monkeypatch.setattr(identity_module.sys, "flags", SimpleNamespace(**flags))
    monkeypatch.setattr(identity_module.sys, "path", effective_sys_path.copy())
    monkeypatch.setattr(identity_module.sys, "executable", str(executable))
    monkeypatch.setattr(identity_module.sys, "dont_write_bytecode", True)
    monkeypatch.setattr(
        identity_module.sys,
        "pycache_prefix",
        str(pycache_prefix.resolve()),
    )
    for module_name in ("site", "sitecustomize", "usercustomize"):
        monkeypatch.delitem(identity_module.sys.modules, module_name, raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("QUALIFICATION_CONTAINER_IMAGE_DIGEST", raising=False)
    monkeypatch.setattr(
        identity_module,
        "capture_source_identity",
        lambda *_args, **_kwargs: source,
    )
    monkeypatch.setattr(
        identity_module,
        "capture_interpreter_installation_identity",
        lambda **_kwargs: interpreter,
    )
    monkeypatch.setattr(
        identity_module,
        "capture_runtime_site_packages_identity",
        lambda *_args, **_kwargs: runtime_site_manifest,
    )
    monkeypatch.setenv(
        STARTUP_MARKER_ENV,
        json.dumps(marker, sort_keys=True, separators=(",", ":")),
    )

    startup = capture_trusted_startup_identity(
        repo_root,
        expected_target="verify-environment",
    )

    report = startup.redacted_report_dict()
    assert set(startup.startup_flags) == set(STARTUP_FLAG_NAMES)
    assert report["effective_sys_path_sha256"] == sha256(
        canonical_json_bytes(effective_sys_path)
    ).hexdigest()
    assert report["runtime_pth_files_sha256"] == {
        sha256(str(pth_path.resolve()).encode("utf-8")).hexdigest(): sha256(
            pth_path.read_bytes()
        ).hexdigest()
    }
    assert str(repo_root) not in canonical_json_bytes(report).decode("ascii")

    identity_module.sys.path.append(str(tmp_path / "injected"))
    with pytest.raises(IdentityError, match="effective sys.path"):
        capture_trusted_startup_identity(
            repo_root,
            expected_target="verify-environment",
        )


def test_signed_campaign_and_attempt_are_exact_short_lived_and_non_authorizing() -> None:
    private, public = _key_pair()
    campaign = verify_campaign_approval(
        _signed(private, _campaign_payload()),
        public_key=public,
        expected_key_id=KEY_ID,
        expected_preregistration_sha256=PREREGISTRATION_SHA,
        expected_source_sha=SOURCE_SHA,
        now=NOW,
    )
    attempt = verify_attempt_authorization(
        _signed(private, _attempt_payload()),
        public_key=public,
        expected_key_id=KEY_ID,
        campaign=campaign,
        now=NOW,
    )

    assert campaign.max_attempts == 3
    assert campaign.ledger_instance_id == "ledger_instance_1"
    assert campaign.ledger_custodian_key_id == "ledger_custodian_1"
    assert campaign.ledger_custodian_public_key_sha256 == "f" * 64
    assert attempt.attempt_index == 1
    assert campaign.production_authorized is False


def test_signature_expiry_unknown_fields_and_identity_mismatch_fail_closed() -> None:
    private, public = _key_pair()
    other_private, _ = _key_pair()

    with pytest.raises(IdentityError, match="signature"):
        verify_campaign_approval(
            _signed(other_private, _campaign_payload()),
            public_key=public,
            expected_key_id=KEY_ID,
            expected_preregistration_sha256=PREREGISTRATION_SHA,
            expected_source_sha=SOURCE_SHA,
            now=NOW,
        )

    expired = _campaign_payload(expires_at="2026-07-15T14:59:30Z")
    with pytest.raises(IdentityError, match="expired"):
        verify_campaign_approval(
            _signed(private, expired),
            public_key=public,
            expected_key_id=KEY_ID,
            expected_preregistration_sha256=PREREGISTRATION_SHA,
            expected_source_sha=SOURCE_SHA,
            now=NOW,
        )

    unknown = _campaign_payload(unexpected=True)
    with pytest.raises(IdentityError, match="unknown campaign approval field"):
        verify_campaign_approval(
            _signed(private, unknown),
            public_key=public,
            expected_key_id=KEY_ID,
            expected_preregistration_sha256=PREREGISTRATION_SHA,
            expected_source_sha=SOURCE_SHA,
            now=NOW,
        )


def test_ledger_location_digest_binds_one_canonical_external_custody_path(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "custody" / "ledger.json"
    same = approved.parent / "." / approved.name
    different = tmp_path / "custody" / "other-ledger.json"

    assert ledger_location_sha256(approved) == ledger_location_sha256(same)
    assert ledger_location_sha256(approved) != ledger_location_sha256(different)
    assert not hasattr(identity_module, "AttemptLedger")
    assert not hasattr(identity_module, "validate_ledger_snapshot")
