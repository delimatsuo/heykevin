#!/usr/bin/env python3
"""Start Gate 0B tools behind an isolated, no-site Python import boundary."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import sys
from typing import Mapping, Sequence


TRUSTED_STARTUP_SCHEMA_ID = "gate_0b_trusted_startup_v1"
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
MAX_STARTUP_ARTIFACT_BYTES = 1024 * 1024
TARGETS = {
    "evaluate-qualification": "scripts/evaluate_gemini_caller_turn_qualification.py",
    "run-qualification": "scripts/run_gemini_caller_turn_qualification.py",
    "verify-environment": "scripts/verify_qualification_environment.py",
}


class BootstrapError(RuntimeError):
    """Raised before any project or third-party import can be trusted."""


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
        or flags["no_site"] != 1
        or flags["ignore_environment"] != 1
        or flags["no_user_site"] != 1
        or flags["safe_path"] is not True
    ):
        raise BootstrapError("qualification startup requires python -I -S")
    if any(name in sys.modules for name in AUTOMATIC_STARTUP_MODULES):
        raise BootstrapError("automatic startup modules are already loaded")


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
    if any("__pycache__" not in path.parts for path in candidates):
        raise BootstrapError("sourceless repository bytecode is forbidden")


def _build_marker(repo_root: str, *, target: str) -> dict[str, object]:
    if STARTUP_MARKER_ENV in os.environ:
        raise BootstrapError("trusted startup marker already exists")
    neutralized_environment = list(AMBIENT_PYTHON_PATH_ENV)
    for name in AMBIENT_PYTHON_PATH_ENV:
        os.environ.pop(name, None)

    runtime_site = _runtime_site_packages()
    _reject_sourceless_repository_bytecode(repo_root)
    effective_sys_path = [*_stdlib_paths(), repo_root, runtime_site]
    if len(effective_sys_path) != len(set(effective_sys_path)):
        raise BootstrapError("effective sys.path contains duplicate entries")
    sys.path[:] = effective_sys_path
    sys.dont_write_bytecode = True
    pycache_prefix = _canonical_path(Path(repo_root) / ".gate0b-disabled-pycache")
    if Path(pycache_prefix).exists() or Path(pycache_prefix).is_symlink():
        raise BootstrapError("qualification bytecode cache prefix is not empty")
    sys.pycache_prefix = pycache_prefix

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
    }


def _redact_path_map(values: Mapping[str, str]) -> dict[str, str]:
    return {
        sha256(path.encode("utf-8")).hexdigest(): digest
        for path, digest in sorted(values.items())
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


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        _require_isolated_no_site_startup()
    except BootstrapError:
        _blocked("qualification_startup_not_isolated")
        return 2
    try:
        if not args:
            raise BootstrapError("qualification target is required")
        target, *target_args = args
        if target != "probe" and target not in TARGETS:
            raise BootstrapError("qualification target is not allowlisted")
        repo_root = _canonical_path(Path(__file__).resolve().parents[1])
        marker = _build_marker(repo_root, target=target)
        os.environ[STARTUP_MARKER_ENV] = _canonical_json_bytes(marker).decode("ascii")
        if target == "probe":
            print(_canonical_json_bytes(_redacted_marker(marker)).decode("ascii"))
            return 0
        target_path = _target_path(repo_root, target)
        sys.argv = [str(target_path), *target_args]
        runpy.run_path(str(target_path), run_name="__main__")
    except BootstrapError:
        _blocked("qualification_startup_invalid")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
