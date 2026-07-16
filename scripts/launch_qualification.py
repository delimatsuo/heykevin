#!/usr/bin/env python3
"""Start Gate 0B tools behind an isolated, no-site Python import boundary."""

from __future__ import annotations

import ctypes
import sys
from hashlib import sha256
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import platform
import stat
import subprocess  # nosec B404
from types import ModuleType
from typing import Any, Mapping, Sequence


sys.dont_write_bytecode = True


TRUSTED_STARTUP_SCHEMA_ID = "gate_0b_trusted_startup_v5"
INTERPRETER_INSTALLATION_SCHEMA_ID = "gate_0b_interpreter_installation_v2"
NATIVE_RUNTIME_CLOSURE_SCHEMA_ID = "gate_0b_native_runtime_closure_v1"
RUNTIME_SITE_PACKAGES_SCHEMA_ID = "gate_0b_runtime_site_packages_v1"
STARTUP_MARKER_ENV = "KEVIN_GATE0B_TRUSTED_STARTUP"
STARTUP_FLAG_NAMES = (
    "bytes_warning",
    "debug",
    "dev_mode",
    "dont_write_bytecode",
    "hash_randomization",
    "ignore_environment",
    "inspect",
    "int_max_str_digits",
    "interactive",
    "isolated",
    "no_site",
    "no_user_site",
    "optimize",
    "quiet",
    "safe_path",
    "utf8_mode",
    "verbose",
    "warn_default_encoding",
)
AMBIENT_PYTHON_PATH_ENV = ("PYTHONHOME", "PYTHONPATH")
AUTOMATIC_STARTUP_MODULES = ("site", "sitecustomize", "usercustomize")
SELF_ASSERTED_RUNTIME_ENV = "QUALIFICATION_CONTAINER_IMAGE_DIGEST"
MAX_STARTUP_ARTIFACT_BYTES = 1024 * 1024
MAX_EXECUTABLE_SOURCE_BYTES = 2 * 1024 * 1024
GIT_BINARY = "/usr/bin/git"
EXECUTION_DEPENDENCY_PATHS = (
    "config/qualification/gate_0b_approval_root.ed25519.pub",
    "app/__init__.py",
    "app/services/__init__.py",
    "app/utils/__init__.py",
    "app/services/caller_turn_qualification.py",
    "app/services/qualification_environment.py",
    "app/services/qualification_identity.py",
    "app/services/qualification_ledger.py",
    "app/services/qualification_allocation.py",
    "app/services/qualification_privacy.py",
    "app/services/qualification_private_paths.py",
    "app/services/caller_turn_alignment.py",
    "app/services/caller_turn_measurement.py",
    "app/services/caller_turns.py",
    "app/services/gemini_turn_events.py",
    "app/services/voice_turn_replay.py",
    "app/utils/audio.py",
    "scripts/run_gemini_caller_turn_qualification.py",
    "scripts/evaluate_gemini_caller_turn_qualification.py",
    "scripts/launch_qualification.py",
    "scripts/verify_qualification_environment.py",
    "app/services/gemini_pipeline.py",
    "app/services/voice_pipeline.py",
    "app/config.py",
    "tests/fixtures/caller_turn_qualification/pricing.json",
    "uv.lock",
)
EXECUTABLE_MODULE_ROOTS = ("app", "cryptography", "scripts", "websockets")
STDLIB_BYTECODE_SUFFIXES = (".pyc", ".pyo")
STDLIB_SOURCE_BYTECODE_SUFFIXES = (".py", *STDLIB_BYTECODE_SUFFIXES)
NATIVE_EXTENSION_SUFFIXES = (".dll", ".dylib", ".pyd", ".so")
NATIVE_LOADER_ENV_PREFIXES = ("DYLD_", "LD_")
DARWIN_LINK_TOOL = "/usr/bin/otool"
LINUX_LINK_TOOL = "/usr/bin/ldd"
TARGETS = {
    "evaluate-qualification": "scripts/evaluate_gemini_caller_turn_qualification.py",
    "run-qualification": "scripts/run_gemini_caller_turn_qualification.py",
    "verify-environment": "scripts/verify_qualification_environment.py",
}


class BootstrapError(RuntimeError):
    """Raised before any project or third-party import can be trusted."""


class _SnapshotLoader(importlib.abc.Loader):
    def __init__(
        self,
        *,
        source: bytes,
        origin: str,
        is_package: bool,
    ) -> None:
        self._source = source
        self._origin = origin
        self._is_package = is_package

    def create_module(self, _spec: object) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        code = compile(
            self._source,
            self._origin,
            "exec",
            dont_inherit=True,
        )
        exec(code, module.__dict__)


class _SnapshotFinder(importlib.abc.MetaPathFinder):
    def __init__(
        self,
        repo_root: str,
        snapshot: Mapping[str, bytes],
    ) -> None:
        self._modules: dict[str, tuple[bytes, str, bool]] = {}
        for relative, source in snapshot.items():
            module_name, is_package = _module_name_for_source(relative)
            self._modules[module_name] = (
                source,
                str(Path(repo_root, relative)),
                is_package,
            )
        self._modules.setdefault(
            "scripts",
            (b"", str(Path(repo_root, "scripts", "__init__.py")), True),
        )

    def find_spec(
        self,
        fullname: str,
        _path: object = None,
        _target: object = None,
    ) -> object:
        entry = self._modules.get(fullname)
        if entry is not None:
            source, origin, is_package = entry
            loader = _SnapshotLoader(
                source=source,
                origin=origin,
                is_package=is_package,
            )
            return importlib.util.spec_from_loader(
                fullname,
                loader,
                origin=origin,
                is_package=is_package,
            )
        if fullname.split(".", 1)[0] in {"app", "scripts"}:
            raise ImportError("project import is outside the approved source snapshot")
        return None


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _canonical_path(value: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.path.abspath(os.fspath(value)))


def _is_relative_to(path: str, root: str) -> bool:
    try:
        Path(path).relative_to(root)
    except ValueError:
        return False
    return True


def _startup_flags() -> dict[str, int | bool]:
    return {name: getattr(sys.flags, name) for name in STARTUP_FLAG_NAMES}


def _require_isolated_no_site_startup() -> None:
    flags = _startup_flags()
    if (
        flags["isolated"] != 1
        or flags["dont_write_bytecode"] != 1
        or flags["no_site"] != 1
        or flags["ignore_environment"] != 1
        or flags["no_user_site"] != 1
        or flags["safe_path"] is not True
    ):
        raise BootstrapError("qualification startup requires python -B -I -S")
    if any(name in sys.modules for name in AUTOMATIC_STARTUP_MODULES):
        raise BootstrapError("automatic startup modules are already loaded")


def _reject_preloaded_executable_modules(repo_root: str) -> None:
    executable_paths = {
        _canonical_path(Path(repo_root, relative))
        for relative in EXECUTION_DEPENDENCY_PATHS
        if relative.endswith(".py")
    }
    launcher_path = _canonical_path(__file__)
    for name, module in tuple(sys.modules.items()):
        if name == __name__:
            continue
        root = name.split(".", 1)[0]
        module_file = getattr(module, "__file__", None)
        if root in EXECUTABLE_MODULE_ROOTS:
            raise BootstrapError("preloaded executable module is forbidden")
        if isinstance(module_file, str):
            canonical = _canonical_path(module_file)
            if canonical in executable_paths and canonical != launcher_path:
                raise BootstrapError("preloaded executable module is forbidden")


def _git_command(*args: str) -> list[str]:
    return [
        GIT_BINARY,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        *args,
    ]


def _git_text(root: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            _git_command(*args),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )  # nosec B603
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BootstrapError("Git source preflight failed") from exc
    return completed.stdout.strip()


def _git_bytes(root: str, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            _git_command(*args),
            cwd=root,
            check=True,
            capture_output=True,
        )  # nosec B603
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BootstrapError("Git source preflight failed") from exc
    return completed.stdout


def _capture_source_preflight(
    repo_root: str,
    *,
    expected_source_sha: str | None = None,
) -> dict[str, object]:
    if _canonical_path(_git_text(repo_root, "rev-parse", "--show-toplevel")) != repo_root:
        raise BootstrapError("qualification repository root mismatch")
    source_sha = _git_text(repo_root, "rev-parse", "HEAD")
    if not source_sha or any(character not in "0123456789abcdef" for character in source_sha):
        raise BootstrapError("qualification source SHA is invalid")
    if expected_source_sha is not None and source_sha != expected_source_sha:
        raise BootstrapError("qualification source SHA does not match approval")
    status = _git_text(
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if status:
        raise BootstrapError("qualification worktree is not clean")

    dependencies: dict[str, dict[str, str]] = {}
    for relative in EXECUTION_DEPENDENCY_PATHS:
        candidate = Path(repo_root, relative)
        if candidate.is_symlink() or not candidate.is_file():
            raise BootstrapError("qualification dependency is unavailable")
        resolved = _canonical_path(candidate)
        if not _is_relative_to(resolved, repo_root):
            raise BootstrapError("qualification dependency escaped repository")
        worktree_bytes = candidate.read_bytes()
        committed_bytes = _git_bytes(repo_root, "cat-file", "blob", f"HEAD:{relative}")
        if worktree_bytes != committed_bytes:
            raise BootstrapError("qualification dependency differs from committed blob")
        dependencies[relative] = {
            "worktree_sha256": sha256(worktree_bytes).hexdigest(),
            "git_blob_id": _git_text(repo_root, "rev-parse", f"HEAD:{relative}"),
        }
    return {
        "source_sha": source_sha,
        "clean": True,
        "dependencies": dependencies,
    }


def _capture_execution_source_snapshot(
    repo_root: str,
    source_preflight: Mapping[str, object],
) -> dict[str, bytes]:
    dependencies = source_preflight.get("dependencies")
    if not isinstance(dependencies, Mapping):
        raise BootstrapError("qualification source snapshot identity is invalid")
    snapshot: dict[str, bytes] = {}
    for relative in EXECUTION_DEPENDENCY_PATHS:
        if not relative.endswith(".py"):
            continue
        identity = dependencies.get(relative)
        if not isinstance(identity, Mapping):
            raise BootstrapError("qualification source snapshot identity is invalid")
        expected_sha256 = identity.get("worktree_sha256")
        if not isinstance(expected_sha256, str):
            raise BootstrapError("qualification source snapshot identity is invalid")
        source = _read_executable_source(Path(repo_root, relative))
        if sha256(source).hexdigest() != expected_sha256:
            raise BootstrapError("qualification source changed before snapshot")
        snapshot[relative] = source
    expected_sources = {
        relative for relative in EXECUTION_DEPENDENCY_PATHS if relative.endswith(".py")
    }
    if set(snapshot) != expected_sources:
        raise BootstrapError("qualification source snapshot is incomplete")
    return snapshot


def _read_executable_source(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
        try:
            metadata_before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata_before.st_mode)
                or metadata_before.st_nlink != 1
                or metadata_before.st_size > MAX_EXECUTABLE_SOURCE_BYTES
            ):
                raise BootstrapError("qualification source snapshot is unavailable")
            chunks: list[bytes] = []
            remaining = MAX_EXECUTABLE_SOURCE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            source = b"".join(chunks)
            metadata_after = os.fstat(descriptor)
            if (
                len(source) > MAX_EXECUTABLE_SOURCE_BYTES
                or _stable_file_metadata(metadata_before)
                != _stable_file_metadata(metadata_after)
            ):
                raise BootstrapError("qualification source snapshot is unavailable")
            return source
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BootstrapError("qualification source snapshot is unavailable") from exc


def _stable_file_metadata(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _module_name_for_source(relative: str) -> tuple[str, bool]:
    path = Path(relative)
    if path.suffix != ".py" or path.is_absolute():
        raise BootstrapError("qualification source module path is invalid")
    if path.name == "__init__.py":
        parts = path.parent.parts
        is_package = True
    else:
        parts = (*path.parent.parts, path.stem)
        is_package = False
    if not parts or parts[0] not in {"app", "scripts"}:
        raise BootstrapError("qualification source module path is invalid")
    return ".".join(parts), is_package


def _execute_snapshot_target(
    repo_root: str,
    *,
    target: str,
    target_args: Sequence[str],
    snapshot: Mapping[str, bytes],
) -> None:
    relative = TARGETS.get(target)
    if relative is None or relative not in snapshot:
        raise BootstrapError("qualification target snapshot is unavailable")
    target_path = str(Path(repo_root, relative))
    finder = _SnapshotFinder(repo_root, snapshot)
    sys.meta_path.insert(0, finder)
    previous_argv = sys.argv
    try:
        sys.argv = [target_path, *target_args]
        globals_dict: dict[str, Any] = {
            "__builtins__": __builtins__,
            "__cached__": None,
            "__file__": target_path,
            "__loader__": None,
            "__name__": "__main__",
            "__package__": None,
            "__spec__": None,
        }
        code = compile(snapshot[relative], target_path, "exec", dont_inherit=True)
        try:
            exec(code, globals_dict)
        except ImportError as exc:
            raise BootstrapError(
                "qualification target import is outside the approved runtime"
            ) from exc
    finally:
        sys.argv = previous_argv
        if sys.meta_path[:1] != [finder]:
            raise BootstrapError("qualification source importer order changed")
        del sys.meta_path[0]


def _bytecode_source(path: Path) -> Path:
    if path.parent.name == "__pycache__":
        source_name = path.name.split(".", 1)[0] + ".py"
        return path.parent.parent / source_name
    return path.with_suffix(".py")


def _require_source_for_bytecode(path: Path, *, label: str) -> None:
    source = _bytecode_source(path)
    if source.is_symlink() or not source.is_file():
        raise BootstrapError(f"sourceless {label} bytecode is forbidden")


def _runtime_manifest_record(path: Path, *, name: str) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise BootstrapError("interpreter installation file is invalid")
    return {"name": name, "sha256": sha256(path.read_bytes()).hexdigest()}


def _manifest_sha256(records: Sequence[Mapping[str, str]]) -> str:
    return sha256(_canonical_json_bytes(list(records))).hexdigest()


def _runtime_site_file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "source"
    if suffix in STDLIB_BYTECODE_SUFFIXES:
        return "bytecode"
    if suffix in NATIVE_EXTENSION_SUFFIXES:
        return "native_extension"
    return "metadata_data"


def _capture_runtime_site_packages_identity(runtime_site: str) -> dict[str, object]:
    root = Path(runtime_site)
    if root.is_symlink() or not root.is_dir():
        raise BootstrapError("runtime site-packages identity is unavailable")
    records: list[dict[str, str]] = []
    counts = {
        "source": 0,
        "bytecode": 0,
        "native_extension": 0,
        "metadata_data": 0,
    }
    try:
        for candidate in sorted(root.rglob("*")):
            if candidate.is_symlink():
                raise BootstrapError("runtime site-packages symlink is forbidden")
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(root).as_posix()
            kind = _runtime_site_file_kind(candidate)
            counts[kind] += 1
            records.append(
                {
                    "kind": kind,
                    "path_sha256": sha256(relative.encode("utf-8")).hexdigest(),
                    "sha256": sha256(candidate.read_bytes()).hexdigest(),
                }
            )
    except OSError as exc:
        raise BootstrapError("runtime site-packages identity failed") from exc
    if not records:
        raise BootstrapError("runtime site-packages identity is empty")
    identity: dict[str, object] = {
        "schema_id": RUNTIME_SITE_PACKAGES_SCHEMA_ID,
        "source_count": counts["source"],
        "bytecode_count": counts["bytecode"],
        "native_extension_count": counts["native_extension"],
        "metadata_data_count": counts["metadata_data"],
        "file_count": len(records),
        "files_sha256": _manifest_sha256(records),
    }
    identity["manifest_sha256"] = sha256(
        _canonical_json_bytes(identity)
    ).hexdigest()
    return identity


def _capture_interpreter_installation_identity(
    *,
    stdlib_paths: Sequence[str] | None = None,
    python_executable: str | None = None,
) -> dict[str, object]:
    paths = tuple(_stdlib_paths() if stdlib_paths is None else stdlib_paths)
    executable = Path(
        _canonical_path(sys.executable if python_executable is None else python_executable)
    )
    if not executable.is_file():
        raise BootstrapError("Python executable identity is unavailable")

    source_bytecode: list[dict[str, str]] = []
    archives: list[dict[str, str]] = []
    native_extensions: list[dict[str, str]] = []
    seen_files: set[str] = set()
    for root_index, raw_root in enumerate(paths):
        root = Path(_canonical_path(raw_root))
        if root.is_file():
            if root.suffix.lower() == ".zip":
                archives.append(
                    _runtime_manifest_record(
                        root,
                        name=f"archive/{root_index}/{root.name}",
                    )
                )
            continue
        if not root.is_dir():
            continue
        if root.name == "lib-dynload":
            for candidate in sorted(root.rglob("*")):
                if candidate.suffix.lower() not in NATIVE_EXTENSION_SUFFIXES:
                    continue
                canonical = _canonical_path(candidate)
                if canonical in seen_files:
                    continue
                seen_files.add(canonical)
                native_extensions.append(
                    _runtime_manifest_record(
                        candidate,
                        name=f"native/{candidate.relative_to(root).as_posix()}",
                    )
                )
            continue
        for candidate in sorted(root.rglob("*")):
            relative = candidate.relative_to(root)
            if "site-packages" in relative.parts or relative.parts[:1] == (
                "lib-dynload",
            ):
                continue
            suffix = candidate.suffix.lower()
            if suffix not in STDLIB_SOURCE_BYTECODE_SUFFIXES:
                continue
            canonical = _canonical_path(candidate)
            if canonical in seen_files:
                continue
            if suffix in STDLIB_BYTECODE_SUFFIXES:
                _require_source_for_bytecode(candidate, label="standard-library")
            seen_files.add(canonical)
            source_bytecode.append(
                _runtime_manifest_record(
                    candidate,
                    name=f"stdlib/{relative.as_posix()}",
                )
            )

    if not source_bytecode or not native_extensions:
        raise BootstrapError("interpreter installation identity is incomplete")
    runtime_site = Path(_runtime_site_packages())
    site_native_extensions = tuple(
        candidate
        for candidate in sorted(runtime_site.rglob("*"))
        if candidate.is_file()
        and not candidate.is_symlink()
        and candidate.suffix.lower() in NATIVE_EXTENSION_SUFFIXES
    )
    native_runtime_closure, allowed_native_images = _capture_native_runtime_closure(
        executable,
        roots=(
            *(
                Path(_canonical_path(path))
                for path in seen_files
                if Path(path).suffix.lower() in NATIVE_EXTENSION_SUFFIXES
            ),
            *site_native_extensions,
            *_python_process_image_roots(),
        ),
    )
    _require_loaded_native_images_within_closure(allowed_native_images)
    identity: dict[str, object] = {
        "schema_id": INTERPRETER_INSTALLATION_SCHEMA_ID,
        "python_executable_sha256": sha256(executable.read_bytes()).hexdigest(),
        "stdlib_source_bytecode_sha256": _manifest_sha256(source_bytecode),
        "stdlib_source_bytecode_count": len(source_bytecode),
        "stdlib_archive_sha256": _manifest_sha256(archives),
        "stdlib_archive_count": len(archives),
        "native_extension_sha256": _manifest_sha256(native_extensions),
        "native_extension_count": len(native_extensions),
        "native_runtime_closure": native_runtime_closure,
    }
    identity["installation_sha256"] = sha256(
        _canonical_json_bytes(identity)
    ).hexdigest()
    return identity


def _capture_native_runtime_closure(
    executable: Path,
    *,
    roots: Sequence[Path],
) -> tuple[dict[str, object], dict[str, str]]:
    queue = [
        Path(_canonical_path(executable)),
        *(Path(_canonical_path(path)) for path in roots),
    ]
    allowed: dict[str, str] = {}
    virtual_dependencies: set[str] = set()
    while queue:
        candidate = queue.pop()
        canonical = _canonical_path(candidate)
        if canonical in allowed:
            continue
        path = Path(canonical)
        if path.is_symlink() or not path.is_file():
            raise BootstrapError("native runtime dependency is unavailable")
        digest = sha256(path.read_bytes()).hexdigest()
        allowed[canonical] = digest
        linked_paths, virtual = _linked_native_dependencies(
            path,
            executable=Path(_canonical_path(executable)),
        )
        queue.extend(linked_paths)
        virtual_dependencies.update(virtual)

    records = [
        {
            "location_sha256": sha256(path.encode("utf-8")).hexdigest(),
            "sha256": digest,
        }
        for path, digest in sorted(allowed.items())
    ]
    virtual_records = sorted(
        sha256(value.encode("utf-8")).hexdigest()
        for value in virtual_dependencies
    )
    identity: dict[str, object] = {
        "schema_id": NATIVE_RUNTIME_CLOSURE_SCHEMA_ID,
        "regular_file_count": len(records),
        "regular_files_sha256": sha256(_canonical_json_bytes(records)).hexdigest(),
        "virtual_dependency_count": len(virtual_records),
        "virtual_dependencies_sha256": sha256(
            _canonical_json_bytes(virtual_records)
        ).hexdigest(),
        "system_loader_identity_sha256": _system_loader_identity_sha256(
            virtual_dependencies
        ),
    }
    identity["closure_sha256"] = sha256(_canonical_json_bytes(identity)).hexdigest()
    return identity, allowed


def _linked_native_dependencies(
    path: Path,
    *,
    executable: Path,
) -> tuple[list[Path], set[str]]:
    system = platform.system()
    if system == "Darwin":
        command = [DARWIN_LINK_TOOL, "-L", str(path)]
    elif system == "Linux":
        command = [LINUX_LINK_TOOL, str(path)]
    else:
        raise BootstrapError("native runtime platform is unsupported")
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )  # nosec B603
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError("native runtime dependency inspection failed") from exc
    if system == "Darwin":
        return _parse_darwin_dependencies(
            completed.stdout,
            binary=path,
            executable=executable,
        )
    return _parse_linux_dependencies(completed.stdout)


def _parse_darwin_dependencies(
    output: str,
    *,
    binary: Path,
    executable: Path,
) -> tuple[list[Path], set[str]]:
    resolved: list[Path] = []
    virtual: set[str] = set()
    first_load_for_architecture = True
    for raw_line in output.splitlines()[1:]:
        if not raw_line.startswith("\t"):
            first_load_for_architecture = True
            continue
        line = raw_line.strip()
        if not line:
            continue
        load_name = line.split(" (", 1)[0]
        if first_load_for_architecture and Path(load_name).name == binary.name:
            first_load_for_architecture = False
            continue
        first_load_for_architecture = False
        if load_name.startswith("@loader_path/"):
            candidate = binary.parent / load_name.removeprefix("@loader_path/")
        elif load_name.startswith("@executable_path/"):
            candidate = executable.parent / load_name.removeprefix("@executable_path/")
        elif load_name.startswith("@rpath/"):
            local = binary.parent / load_name.removeprefix("@rpath/")
            if local.is_file():
                candidate = local
            else:
                virtual.add(load_name)
                continue
        elif load_name.startswith("/"):
            candidate = Path(load_name)
        else:
            virtual.add(load_name)
            continue
        if candidate.is_file():
            if _canonical_path(candidate) == _canonical_path(binary):
                continue
            resolved.append(candidate)
        elif load_name.startswith(("/usr/lib/", "/System/Library/")):
            virtual.add(load_name)
        else:
            raise BootstrapError("native runtime dependency is unavailable")
    return resolved, virtual


def _parse_linux_dependencies(output: str) -> tuple[list[Path], set[str]]:
    resolved: list[Path] = []
    virtual: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=>" in line:
            load_name, remainder = line.split("=>", 1)
            fields = remainder.split()
            if fields[:2] == ["not", "found"] or not fields:
                raise BootstrapError("native runtime dependency is unavailable")
            target = fields[0]
        else:
            load_name = line.split(maxsplit=1)[0]
            target = load_name
        if target.startswith("/"):
            candidate = Path(target)
            if not candidate.is_file():
                raise BootstrapError("native runtime dependency is unavailable")
            resolved.append(candidate)
        else:
            virtual.add(load_name.strip())
    return resolved, virtual


def _system_loader_identity_sha256(virtual_dependencies: set[str]) -> str:
    if platform.system() == "Darwin":
        buffer = (ctypes.c_ubyte * 16)()
        library = ctypes.CDLL(None)
        function = library._dyld_get_shared_cache_uuid
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_bool
        if not function(buffer):
            raise BootstrapError("dyld shared cache identity is unavailable")
        system_identity: dict[str, object] = {
            "platform": "darwin",
            "shared_cache_uuid": bytes(buffer).hex(),
        }
    else:
        system_identity = {
            "platform": platform.system().lower(),
            "release": platform.release(),
            "architecture": platform.machine().lower(),
        }
    system_identity["virtual_dependencies"] = sorted(virtual_dependencies)
    return sha256(_canonical_json_bytes(system_identity)).hexdigest()


def _loaded_native_image_paths() -> tuple[str, ...]:
    if platform.system() == "Darwin":
        library = ctypes.CDLL(None)
        count = library._dyld_image_count
        count.argtypes = []
        count.restype = ctypes.c_uint32
        name = library._dyld_get_image_name
        name.argtypes = [ctypes.c_uint32]
        name.restype = ctypes.c_char_p
        return tuple(
            decoded
            for index in range(count())
            if (raw := name(index)) is not None
            if (decoded := raw.decode("utf-8", errors="strict"))
        )
    if platform.system() == "Linux":
        try:
            lines = Path("/proc/self/maps").read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise BootstrapError("loaded native image map is unavailable") from exc
        paths: set[str] = set()
        for line in lines:
            fields = line.split(maxsplit=5)
            if len(fields) == 6 and "x" in fields[1] and fields[5].startswith("/"):
                paths.add(fields[5])
        return tuple(sorted(paths))
    raise BootstrapError("loaded native image platform is unsupported")


def _python_process_image_roots() -> tuple[Path, ...]:
    if platform.system() != "Darwin":
        return ()
    candidate = (
        Path(sys.base_prefix)
        / "Resources"
        / "Python.app"
        / "Contents"
        / "MacOS"
        / "Python"
    )
    return (candidate,) if candidate.is_file() else ()


def _require_loaded_native_images_within_closure(
    allowed_native_images: Mapping[str, str],
) -> None:
    for raw_path in _loaded_native_image_paths():
        if platform.system() == "Darwin" and raw_path.startswith(
            ("/usr/lib/", "/System/Library/")
        ):
            continue
        path = Path(raw_path.removesuffix(" (deleted)"))
        if path.is_file():
            canonical = _canonical_path(path)
            expected = allowed_native_images.get(canonical)
            if expected is None or sha256(Path(canonical).read_bytes()).hexdigest() != expected:
                raise BootstrapError("loaded native image is outside the approved closure")
        else:
            raise BootstrapError("loaded native image is unavailable")


def _stdlib_paths() -> list[str]:
    base_prefix = _canonical_path(sys.base_prefix)
    paths: list[str] = []
    for entry in sys.path:
        if not isinstance(entry, str) or not entry:
            raise BootstrapError("initial sys.path is not isolated")
        canonical = _canonical_path(entry)
        if not _is_relative_to(canonical, base_prefix):
            raise BootstrapError("initial sys.path escaped the base runtime")
        if canonical in paths:
            raise BootstrapError("initial sys.path contains duplicate entries")
        paths.append(canonical)
    if not paths:
        raise BootstrapError("base runtime paths are unavailable")
    return paths


def _runtime_site_packages() -> str:
    executable = Path(os.path.abspath(sys.executable))
    runtime_root = executable.parent.parent
    runtime_config = runtime_root / "pyvenv.cfg"
    if runtime_config.is_symlink() or not runtime_config.is_file():
        raise BootstrapError("qualification runtime is not a virtual environment")
    site_packages = (
        runtime_root
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    if site_packages.is_symlink() or not site_packages.is_dir():
        raise BootstrapError("qualification runtime site-packages is unavailable")
    return _canonical_path(site_packages)


def _hash_startup_artifact(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise BootstrapError("startup artifact is not a regular file")
    if path.stat().st_size > MAX_STARTUP_ARTIFACT_BYTES:
        raise BootstrapError("startup artifact exceeds its size bound")
    return sha256(path.read_bytes()).hexdigest()


def _runtime_pth_identities(runtime_site: str) -> dict[str, str]:
    root = Path(runtime_site)
    return {
        _canonical_path(path): _hash_startup_artifact(path)
        for path in sorted(root.glob("*.pth"))
    }


def _ignored_startup_hook_identities(runtime_site: str) -> dict[str, str]:
    root = Path(runtime_site)
    candidates = [
        root / f"{module_name}{suffix}"
        for module_name in ("sitecustomize", "usercustomize")
        for suffix in (".py", ".pyc")
    ]
    cache = root / "__pycache__"
    if cache.is_dir() and not cache.is_symlink():
        for module_name in ("sitecustomize", "usercustomize"):
            candidates.extend(sorted(cache.glob(f"{module_name}.*.pyc")))
    return {
        _canonical_path(path): _hash_startup_artifact(path)
        for path in candidates
        if path.exists() or path.is_symlink()
    }


def _reject_sourceless_repository_bytecode(repo_root: str) -> None:
    root = Path(repo_root)
    candidates = [*root.glob("*.pyc"), *root.glob("*.pyo")]
    for import_root in (root / "app", root / "scripts"):
        candidates.extend(import_root.rglob("*.pyc"))
        candidates.extend(import_root.rglob("*.pyo"))
    for path in candidates:
        _require_source_for_bytecode(path, label="repository")


def _reject_sourceless_runtime_bytecode(runtime_site: str) -> None:
    root = Path(runtime_site)
    for suffix in STDLIB_BYTECODE_SUFFIXES:
        for path in root.rglob(f"*{suffix}"):
            _require_source_for_bytecode(path, label="runtime")


def _build_marker(
    repo_root: str,
    *,
    target: str,
    expected_source_sha: str | None = None,
    expected_runtime_site_packages_sha256: str | None = None,
) -> dict[str, object]:
    if STARTUP_MARKER_ENV in os.environ:
        raise BootstrapError("trusted startup marker already exists")
    if SELF_ASSERTED_RUNTIME_ENV in os.environ:
        raise BootstrapError("self-asserted runtime image identity is forbidden")
    _reject_native_loader_environment()
    neutralized_environment = list(AMBIENT_PYTHON_PATH_ENV)
    for name in AMBIENT_PYTHON_PATH_ENV:
        os.environ.pop(name, None)

    pycache_prefix = _canonical_path(Path(repo_root) / ".gate0b-disabled-pycache")
    if Path(pycache_prefix).exists() or Path(pycache_prefix).is_symlink():
        raise BootstrapError("qualification bytecode cache prefix is not empty")
    sys.pycache_prefix = pycache_prefix
    runtime_site = _runtime_site_packages()
    stdlib_paths = _stdlib_paths()
    executable_target = target != "probe"
    if executable_target and (
        expected_source_sha is None
        or expected_runtime_site_packages_sha256 is None
    ):
        raise BootstrapError("qualification startup approvals are required")
    source_preflight = _capture_source_preflight(
        repo_root,
        expected_source_sha=expected_source_sha,
    )
    _reject_sourceless_repository_bytecode(repo_root)
    _reject_sourceless_runtime_bytecode(runtime_site)
    runtime_site_packages_manifest = _capture_runtime_site_packages_identity(
        runtime_site
    )
    if (
        expected_runtime_site_packages_sha256 is not None
        and runtime_site_packages_manifest["manifest_sha256"]
        != expected_runtime_site_packages_sha256
    ):
        raise BootstrapError("runtime site-packages identity does not match approval")
    interpreter_installation = _capture_interpreter_installation_identity(
        stdlib_paths=stdlib_paths,
        python_executable=sys.executable,
    )
    effective_sys_path = [*stdlib_paths, repo_root, runtime_site]
    if len(effective_sys_path) != len(set(effective_sys_path)):
        raise BootstrapError("effective sys.path contains duplicate entries")
    sys.path[:] = effective_sys_path
    sys.dont_write_bytecode = True

    return {
        "schema_id": TRUSTED_STARTUP_SCHEMA_ID,
        "target": target,
        "startup_flags": _startup_flags(),
        "bytecode_write_disabled": sys.dont_write_bytecode,
        "pycache_prefix": pycache_prefix,
        "repo_root": repo_root,
        "python_executable": _canonical_path(sys.executable),
        "runtime_site_packages": runtime_site,
        "effective_sys_path": effective_sys_path,
        "neutralized_environment": neutralized_environment,
        "runtime_pth_files_sha256": _runtime_pth_identities(runtime_site),
        "ignored_startup_hook_files_sha256": _ignored_startup_hook_identities(
            runtime_site
        ),
        "source_preflight": source_preflight,
        "interpreter_installation": interpreter_installation,
        "runtime_site_packages_manifest": runtime_site_packages_manifest,
    }


def _reject_native_loader_environment() -> None:
    if any(
        name.startswith(NATIVE_LOADER_ENV_PREFIXES)
        for name in os.environ
    ):
        raise BootstrapError("native loader environment is forbidden")


def _redact_path_map(values: Mapping[str, str]) -> dict[str, str]:
    return {
        sha256(path.encode("utf-8")).hexdigest(): digest
        for path, digest in sorted(values.items())
    }


def _redacted_source_preflight(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise BootstrapError("source preflight marker is invalid")
    dependencies = raw.get("dependencies")
    if not isinstance(dependencies, Mapping):
        raise BootstrapError("source dependency marker is invalid")
    redacted: dict[str, object] = {}
    for path, identity in sorted(dependencies.items()):
        if not isinstance(path, str) or not isinstance(identity, Mapping):
            raise BootstrapError("source dependency marker is invalid")
        redacted[sha256(path.encode("utf-8")).hexdigest()] = dict(identity)
    return {
        "source_sha": raw.get("source_sha"),
        "clean": raw.get("clean"),
        "dependencies": redacted,
    }


def _redacted_marker(marker: Mapping[str, object]) -> dict[str, object]:
    effective_sys_path = marker["effective_sys_path"]
    if not isinstance(effective_sys_path, list):
        raise BootstrapError("effective sys.path marker is invalid")
    pth_files = marker["runtime_pth_files_sha256"]
    startup_hooks = marker["ignored_startup_hook_files_sha256"]
    if not isinstance(pth_files, Mapping) or not isinstance(startup_hooks, Mapping):
        raise BootstrapError("startup artifact marker is invalid")
    return {
        "schema_id": marker["schema_id"],
        "target": marker["target"],
        "startup_flags": marker["startup_flags"],
        "bytecode_write_disabled": marker["bytecode_write_disabled"],
        "pycache_prefix_location_sha256": sha256(
            str(marker["pycache_prefix"]).encode("utf-8")
        ).hexdigest(),
        "repo_root_location_sha256": sha256(
            str(marker["repo_root"]).encode("utf-8")
        ).hexdigest(),
        "python_executable_location_sha256": sha256(
            str(marker["python_executable"]).encode("utf-8")
        ).hexdigest(),
        "runtime_site_packages_location_sha256": sha256(
            str(marker["runtime_site_packages"]).encode("utf-8")
        ).hexdigest(),
        "effective_sys_path_sha256": sha256(
            _canonical_json_bytes(effective_sys_path)
        ).hexdigest(),
        "effective_sys_path_entry_sha256": [
            sha256(str(path).encode("utf-8")).hexdigest()
            for path in effective_sys_path
        ],
        "neutralized_environment": marker["neutralized_environment"],
        "runtime_pth_files_sha256": _redact_path_map(pth_files),
        "ignored_startup_hook_files_sha256": _redact_path_map(startup_hooks),
        "source_preflight": _redacted_source_preflight(
            marker["source_preflight"]
        ),
        "interpreter_installation": marker["interpreter_installation"],
        "runtime_site_packages_manifest": marker[
            "runtime_site_packages_manifest"
        ],
        "marker_sha256": sha256(_canonical_json_bytes(marker)).hexdigest(),
    }


def _target_path(repo_root: str, target: str) -> Path:
    relative = TARGETS.get(target)
    if relative is None:
        raise BootstrapError("qualification target is not allowlisted")
    candidate = Path(repo_root, relative)
    expected = _canonical_path(candidate)
    if candidate.is_symlink() or not candidate.is_file() or expected != str(candidate):
        raise BootstrapError("qualification target is unavailable")
    return candidate


def _blocked(error_code: str) -> None:
    print(
        json.dumps(
            {"error_code": error_code, "status": "blocked"},
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _is_lower_hex(value: str, *, lengths: tuple[int, ...]) -> bool:
    return len(value) in lengths and all(
        character in "0123456789abcdef" for character in value
    )


def _parse_invocation(
    args: Sequence[str],
) -> tuple[str, str | None, str | None, list[str]]:
    if not args:
        raise BootstrapError("qualification target is required")
    target, *remaining = args
    if target == "probe":
        if remaining:
            raise BootstrapError("qualification probe takes no arguments")
        return target, None, None, []
    if target not in TARGETS:
        raise BootstrapError("qualification target is not allowlisted")
    if (
        len(remaining) < 4
        or remaining[0] != "--expected-source-sha"
        or remaining[2] != "--expected-runtime-site-packages-sha256"
    ):
        raise BootstrapError("qualification startup approvals are required")
    expected_source_sha = remaining[1]
    expected_site_sha256 = remaining[3]
    if not _is_lower_hex(expected_source_sha, lengths=(40, 64)):
        raise BootstrapError("approved qualification source SHA is invalid")
    if not _is_lower_hex(expected_site_sha256, lengths=(64,)):
        raise BootstrapError("approved runtime site-packages digest is invalid")
    return target, expected_source_sha, expected_site_sha256, remaining[4:]


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        _require_isolated_no_site_startup()
    except BootstrapError:
        _blocked("qualification_startup_not_isolated")
        return 2
    try:
        target, expected_source_sha, expected_site_sha256, target_args = (
            _parse_invocation(args)
        )
        repo_root = _canonical_path(Path(__file__).resolve().parents[1])
        _reject_preloaded_executable_modules(repo_root)
        marker = _build_marker(
            repo_root,
            target=target,
            expected_source_sha=expected_source_sha,
            expected_runtime_site_packages_sha256=expected_site_sha256,
        )
        source_snapshot = _capture_execution_source_snapshot(
            repo_root,
            marker["source_preflight"],
        )
        os.environ[STARTUP_MARKER_ENV] = _canonical_json_bytes(marker).decode("ascii")
        if target == "probe":
            print(_canonical_json_bytes(_redacted_marker(marker)).decode("ascii"))
            return 0
        _target_path(repo_root, target)
        _execute_snapshot_target(
            repo_root,
            target=target,
            target_args=target_args,
            snapshot=source_snapshot,
        )
    except BootstrapError:
        _blocked("qualification_startup_invalid")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
