"""Source, environment, and signed approval identity for Gate 0B."""

from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1, sha256
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import ssl
import stat
# Subprocess use is limited to fixed identity-tool argv with shell execution disabled.
import subprocess  # nosec B404
import sys
import time
from typing import Any, Mapping, Sequence
import unicodedata

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

CAMPAIGN_APPROVAL_SCHEMA_ID = "gate_0b_campaign_approval_v1"
ATTEMPT_AUTHORIZATION_SCHEMA_ID = "gate_0b_attempt_authorization_v1"
QUALIFICATION_SCOPE = "gate_0b_purpose_recorded_turn_assembly"
MAX_APPROVAL_LIFETIME_SECONDS = 24 * 60 * 60
MAX_ATTEMPTS = 3
MAX_REQUESTS_PER_ATTEMPT = 128
MAX_REQUESTS_PER_CAMPAIGN = 384
MAX_COST_PER_ATTEMPT_MICROUSD = 10_000_000
MAX_COST_PER_CAMPAIGN_MICROUSD = 30_000_000
SAFE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SOURCE_SHA_PATTERN = re.compile(r"[0-9a-f]{40,64}")
OUTAGE_ENUMS = {
    "provider_dns_outage",
    "provider_control_plane_outage",
    "qualification_host_failure",
}
GIT_BINARY = "/usr/bin/git"
TRUSTED_STARTUP_SCHEMA_ID = "gate_0b_trusted_startup_v5"
TRUSTED_STARTUP_POLICY_SCHEMA_ID = "gate_0b_trusted_startup_policy_v5"
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
STDLIB_BYTECODE_SUFFIXES = (".pyc", ".pyo")
STDLIB_SOURCE_BYTECODE_SUFFIXES = (".py", *STDLIB_BYTECODE_SUFFIXES)
NATIVE_EXTENSION_SUFFIXES = (".dll", ".dylib", ".pyd", ".so")
NATIVE_LOADER_ENV_PREFIXES = ("DYLD_", "LD_")
DARWIN_LINK_TOOL = "/usr/bin/otool"
LINUX_LINK_TOOL = "/usr/bin/ldd"


class IdentityError(ValueError):
    """Raised when execution identity or authorization cannot be trusted."""


def read_identity_bound_file(
    repo_root: str | Path,
    *,
    relative_path: str | Path,
    source_identity: Mapping[str, Any],
    maximum_bytes: int,
) -> bytes:
    """Read one repository file through a descriptor bound to source identity."""
    root = Path(repo_root)
    relative = Path(relative_path)
    if (
        not root.is_absolute()
        or relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or maximum_bytes < 1
    ):
        raise IdentityError("identity-bound file is unavailable")

    validated_source = _validated_redacted_source_preflight(source_identity)
    dependency_key = sha256(relative.as_posix().encode("utf-8")).hexdigest()
    try:
        dependency = validated_source["dependencies"][dependency_key]
        expected_worktree_sha256 = dependency["worktree_sha256"]
        expected_git_blob_id = dependency["git_blob_id"]
    except (KeyError, TypeError) as exc:
        raise IdentityError("identity-bound file source identity mismatch") from exc

    descriptors: list[int] = []
    opened_entries: list[tuple[int, str, os.stat_result]] = []
    try:
        directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(root, directory_flags)
        descriptors.append(root_fd)
        root_metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise IdentityError("identity-bound file is unavailable")

        parent_fd = root_fd
        for component in relative.parts[:-1]:
            child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            descriptors.append(child_fd)
            child_metadata = os.fstat(child_fd)
            if not stat.S_ISDIR(child_metadata.st_mode):
                raise IdentityError("identity-bound file is unavailable")
            opened_entries.append((parent_fd, component, child_metadata))
            parent_fd = child_fd

        file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        file_fd = os.open(relative.name, file_flags, dir_fd=parent_fd)
        descriptors.append(file_fd)
        file_metadata_before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(file_metadata_before.st_mode)
            or file_metadata_before.st_nlink != 1
        ):
            raise IdentityError("identity-bound file is unavailable")
        opened_entries.append((parent_fd, relative.name, file_metadata_before))
        data = _read_bounded_descriptor(file_fd, maximum=maximum_bytes)
        file_metadata_after = os.fstat(file_fd)
        if _stable_metadata(file_metadata_before) != _stable_metadata(
            file_metadata_after
        ):
            raise IdentityError("identity-bound file is unavailable")

        current_root = os.stat(root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current_root.st_mode)
            or _entry_identity(current_root) != _entry_identity(root_metadata)
        ):
            raise IdentityError("identity-bound file is unavailable")
        for entry_parent_fd, component, opened_metadata in opened_entries:
            current = os.stat(
                component,
                dir_fd=entry_parent_fd,
                follow_symlinks=False,
            )
            if _entry_identity(current) != _entry_identity(opened_metadata):
                raise IdentityError("identity-bound file is unavailable")
    except OSError as exc:
        raise IdentityError("identity-bound file is unavailable") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    if len(data) > maximum_bytes:
        raise IdentityError("identity-bound file is unavailable")
    if (
        sha256(data).hexdigest() != expected_worktree_sha256
        or _git_blob_id(data, expected=expected_git_blob_id) != expected_git_blob_id
    ):
        raise IdentityError("identity-bound file source identity mismatch")
    return data


def _read_bounded_descriptor(file_fd: int, *, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(file_fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _stable_metadata(value: os.stat_result) -> tuple[int, ...]:
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


def _entry_identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)


def _git_blob_id(data: bytes, *, expected: str) -> str:
    payload = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
    if len(expected) == 40:
        return sha1(payload, usedforsecurity=False).hexdigest()
    if len(expected) == 64:
        return sha256(payload).hexdigest()
    raise IdentityError("identity-bound file source identity mismatch")


@dataclass(frozen=True, slots=True)
class DependencyIdentity:
    worktree_sha256: str
    git_blob_id: str


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    source_sha: str
    clean: bool
    dependencies: dict[str, DependencyIdentity]

    def redacted_report_dict(self) -> dict[str, Any]:
        return {
            "source_sha": self.source_sha,
            "clean": self.clean,
            "dependencies": {
                sha256(name.encode("utf-8")).hexdigest(): {
                    "worktree_sha256": identity.worktree_sha256,
                    "git_blob_id": identity.git_blob_id,
                }
                for name, identity in sorted(self.dependencies.items())
            },
        }


@dataclass(frozen=True, slots=True)
class EnvironmentIdentity:
    python_version: str
    uv_version: str
    python_executable_sha256: str
    uv_executable_sha256: str
    python_executable_location_sha256: str
    uv_executable_location_sha256: str
    interpreter_installation: dict[str, Any]
    runtime_site_packages_manifest: dict[str, Any]
    platform_id: str
    architecture: str
    unicode_version: str
    monotonic_clock_implementation: str
    monotonic_clock_resolution_ns: int
    bytecode_write_disabled: bool
    openssl_version: str
    ca_bundle_sha256: str
    lock_sha256: str
    codec_golden_sha256: str
    import_sha256: dict[str, str]
    distributions: dict[str, str]
    distribution_files_sha256: dict[str, str]

    def redacted_report_dict(self) -> dict[str, Any]:
        return {
            "python_version": self.python_version,
            "uv_version": self.uv_version,
            "python_executable_sha256": self.python_executable_sha256,
            "uv_executable_sha256": self.uv_executable_sha256,
            "python_executable_location_sha256": self.python_executable_location_sha256,
            "uv_executable_location_sha256": self.uv_executable_location_sha256,
            "interpreter_installation": dict(self.interpreter_installation),
            "runtime_site_packages_manifest": dict(
                self.runtime_site_packages_manifest
            ),
            "platform_id": self.platform_id,
            "architecture": self.architecture,
            "unicode_version": self.unicode_version,
            "monotonic_clock_implementation": self.monotonic_clock_implementation,
            "monotonic_clock_resolution_ns": self.monotonic_clock_resolution_ns,
            "bytecode_write_disabled": self.bytecode_write_disabled,
            "openssl_version": self.openssl_version,
            "ca_bundle_sha256": self.ca_bundle_sha256,
            "lock_sha256": self.lock_sha256,
            "codec_golden_sha256": self.codec_golden_sha256,
            "import_sha256": dict(sorted(self.import_sha256.items())),
            "distributions": dict(sorted(self.distributions.items())),
            "distribution_files_sha256": dict(
                sorted(self.distribution_files_sha256.items())
            ),
        }


@dataclass(frozen=True, slots=True)
class TrustedStartupIdentity:
    target: str
    startup_flags: dict[str, int | bool]
    bytecode_write_disabled: bool
    pycache_prefix_location_sha256: str
    repo_root_location_sha256: str
    python_executable_location_sha256: str
    runtime_site_packages_location_sha256: str
    effective_sys_path_sha256: str
    effective_sys_path_entry_sha256: tuple[str, ...]
    neutralized_environment: tuple[str, ...]
    runtime_pth_files_sha256: dict[str, str]
    ignored_startup_hook_files_sha256: dict[str, str]
    source_preflight: dict[str, Any]
    interpreter_installation: dict[str, Any]
    runtime_site_packages_manifest: dict[str, Any]
    marker_sha256: str

    def redacted_report_dict(self) -> dict[str, Any]:
        return {
            "schema_id": TRUSTED_STARTUP_SCHEMA_ID,
            "target": self.target,
            "startup_flags": dict(self.startup_flags),
            "bytecode_write_disabled": self.bytecode_write_disabled,
            "pycache_prefix_location_sha256": self.pycache_prefix_location_sha256,
            "repo_root_location_sha256": self.repo_root_location_sha256,
            "python_executable_location_sha256": self.python_executable_location_sha256,
            "runtime_site_packages_location_sha256": (
                self.runtime_site_packages_location_sha256
            ),
            "effective_sys_path_sha256": self.effective_sys_path_sha256,
            "effective_sys_path_entry_sha256": list(
                self.effective_sys_path_entry_sha256
            ),
            "neutralized_environment": list(self.neutralized_environment),
            "runtime_pth_files_sha256": dict(
                sorted(self.runtime_pth_files_sha256.items())
            ),
            "ignored_startup_hook_files_sha256": dict(
                sorted(self.ignored_startup_hook_files_sha256.items())
            ),
            "source_preflight": self.source_preflight,
            "interpreter_installation": self.interpreter_installation,
            "runtime_site_packages_manifest": self.runtime_site_packages_manifest,
            "marker_sha256": self.marker_sha256,
        }

    def policy_report_dict(self) -> dict[str, Any]:
        """Return the target-independent startup policy bound to execution identity."""
        return {
            "schema_id": TRUSTED_STARTUP_POLICY_SCHEMA_ID,
            "startup_flags": dict(self.startup_flags),
            "bytecode_write_disabled": self.bytecode_write_disabled,
            "pycache_prefix_location_sha256": self.pycache_prefix_location_sha256,
            "repo_root_location_sha256": self.repo_root_location_sha256,
            "python_executable_location_sha256": self.python_executable_location_sha256,
            "runtime_site_packages_location_sha256": (
                self.runtime_site_packages_location_sha256
            ),
            "effective_sys_path_sha256": self.effective_sys_path_sha256,
            "effective_sys_path_entry_sha256": list(
                self.effective_sys_path_entry_sha256
            ),
            "neutralized_environment": list(self.neutralized_environment),
            "runtime_pth_files_sha256": dict(
                sorted(self.runtime_pth_files_sha256.items())
            ),
            "ignored_startup_hook_files_sha256": dict(
                sorted(self.ignored_startup_hook_files_sha256.items())
            ),
            "source_preflight": self.source_preflight,
            "interpreter_installation": self.interpreter_installation,
            "runtime_site_packages_manifest": self.runtime_site_packages_manifest,
        }


@dataclass(frozen=True, slots=True)
class CampaignApproval:
    campaign_id: str
    authorization_id: str
    nonce: str
    preregistration_sha256: str
    source_sha: str
    issued_at: datetime
    expires_at: datetime
    max_attempts: int
    max_provider_requests: int
    max_cost_microusd: int
    ledger_instance_id: str
    ledger_custodian_key_id: str
    ledger_custodian_public_key_sha256: str
    ledger_location_sha256: str
    real_caller_data_authorized: bool
    runtime_wiring_authorized: bool
    deployment_authorized: bool
    production_authorized: bool
    release_authorized: bool
    signed_payload_sha256: str


@dataclass(frozen=True, slots=True)
class AttemptAuthorization:
    campaign_id: str
    authorization_id: str
    attempt_id: str
    attempt_index: int
    prior_attempt_id: str | None
    outage_enum: str | None
    preregistration_sha256: str
    source_sha: str
    issued_at: datetime
    expires_at: datetime
    provider_request_reservation: int
    cost_reservation_microusd: int
    signed_payload_sha256: str


@dataclass(frozen=True, slots=True)
class AttemptClaim:
    campaign_id: str
    attempt_id: str
    attempt_index: int
    lease_id: str
    provider_requests_reserved: int
    cost_reserved_microusd: int


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise IdentityError("value is not canonical JSON") from exc


def capture_trusted_startup_identity(
    repo_root: str | Path,
    *,
    expected_target: str,
) -> TrustedStartupIdentity:
    """Revalidate the stdlib bootstrap marker against the live interpreter state."""
    encoded = os.environ.get(STARTUP_MARKER_ENV)
    if encoded is None:
        raise IdentityError("trusted qualification startup is unavailable")
    if SELF_ASSERTED_RUNTIME_ENV in os.environ:
        raise IdentityError("self-asserted runtime image identity is forbidden")
    _reject_native_loader_environment()
    try:
        raw = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise IdentityError("trusted qualification startup marker is invalid") from exc
    fields = {
        "schema_id",
        "target",
        "startup_flags",
        "bytecode_write_disabled",
        "pycache_prefix",
        "repo_root",
        "python_executable",
        "runtime_site_packages",
        "effective_sys_path",
        "neutralized_environment",
        "runtime_pth_files_sha256",
        "ignored_startup_hook_files_sha256",
        "source_preflight",
        "interpreter_installation",
        "runtime_site_packages_manifest",
    }
    marker = _strict_object(raw, allowed=fields, label="trusted startup marker")
    if marker["schema_id"] != TRUSTED_STARTUP_SCHEMA_ID:
        raise IdentityError("trusted qualification startup schema is invalid")
    if marker["target"] != expected_target:
        raise IdentityError("trusted qualification startup target is invalid")

    startup_flags = _validated_startup_flags(marker["startup_flags"])
    if (
        startup_flags["isolated"] != 1
        or startup_flags["dont_write_bytecode"] != 1
        or startup_flags["no_site"] != 1
        or startup_flags["ignore_environment"] != 1
        or startup_flags["no_user_site"] != 1
        or startup_flags["safe_path"] is not True
    ):
        raise IdentityError("trusted qualification startup flags are unsafe")
    if any(name in sys.modules for name in AUTOMATIC_STARTUP_MODULES):
        raise IdentityError("automatic startup module loaded after trusted startup")
    if marker["bytecode_write_disabled"] is not True or sys.dont_write_bytecode is not True:
        raise IdentityError("trusted qualification startup permits bytecode writes")

    expected_root = str(Path(repo_root).resolve())
    marker_root = _canonical_marker_path(marker["repo_root"], "repository root")
    if marker_root != expected_root:
        raise IdentityError("trusted qualification repository root mismatch")
    pycache_prefix = _canonical_marker_path(
        marker["pycache_prefix"],
        "bytecode cache prefix",
    )
    if (
        sys.pycache_prefix != pycache_prefix
        or Path(pycache_prefix).exists()
        or Path(pycache_prefix).is_symlink()
    ):
        raise IdentityError("trusted qualification bytecode cache policy changed")
    python_executable = _canonical_marker_path(
        marker["python_executable"],
        "Python executable",
    )
    if python_executable != _canonical_path(sys.executable):
        raise IdentityError("trusted qualification Python executable mismatch")
    runtime_site = _canonical_marker_path(
        marker["runtime_site_packages"],
        "runtime site-packages",
    )

    effective_sys_path = _validated_effective_sys_path(marker["effective_sys_path"])
    live_sys_path = tuple(_canonical_path(entry) for entry in sys.path)
    if effective_sys_path != live_sys_path:
        raise IdentityError("trusted qualification effective sys.path mismatch")
    if marker_root not in effective_sys_path or runtime_site not in effective_sys_path:
        raise IdentityError("trusted qualification allowlisted paths are incomplete")

    neutralized_environment = marker["neutralized_environment"]
    if (
        not isinstance(neutralized_environment, list)
        or neutralized_environment != list(AMBIENT_PYTHON_PATH_ENV)
        or any(name in os.environ for name in AMBIENT_PYTHON_PATH_ENV)
    ):
        raise IdentityError("ambient Python path environment was not neutralized")

    runtime_pth = _validated_startup_artifact_map(
        marker["runtime_pth_files_sha256"],
        label="runtime .pth",
    )
    ignored_hooks = _validated_startup_artifact_map(
        marker["ignored_startup_hook_files_sha256"],
        label="ignored startup hook",
    )
    if runtime_pth != _runtime_pth_identities(runtime_site):
        raise IdentityError("runtime .pth identity changed after trusted startup")
    if ignored_hooks != _ignored_startup_hook_identities(runtime_site):
        raise IdentityError("startup hook identity changed after trusted startup")
    runtime_site_packages_manifest = validate_runtime_site_packages_identity(
        marker["runtime_site_packages_manifest"]
    )
    current_runtime_site_packages = capture_runtime_site_packages_identity(
        runtime_site
    )
    if runtime_site_packages_manifest != current_runtime_site_packages:
        raise IdentityError("runtime site-packages changed after trusted startup")

    source_preflight = _validated_source_preflight_marker(
        marker["source_preflight"]
    )
    current_source = capture_source_identity(
        expected_root,
        expected_source_sha=source_preflight["source_sha"],
        dependency_paths=EXECUTION_DEPENDENCY_PATHS,
    )
    if source_preflight != _source_identity_marker_dict(current_source):
        raise IdentityError("source identity changed after trusted startup")
    interpreter_installation = validate_interpreter_installation_identity(
        marker["interpreter_installation"]
    )
    current_interpreter = capture_interpreter_installation_identity()
    if interpreter_installation != current_interpreter:
        raise IdentityError("interpreter installation changed after trusted startup")

    return TrustedStartupIdentity(
        target=expected_target,
        startup_flags=startup_flags,
        bytecode_write_disabled=True,
        pycache_prefix_location_sha256=sha256(
            pycache_prefix.encode("utf-8")
        ).hexdigest(),
        repo_root_location_sha256=sha256(marker_root.encode("utf-8")).hexdigest(),
        python_executable_location_sha256=sha256(
            python_executable.encode("utf-8")
        ).hexdigest(),
        runtime_site_packages_location_sha256=sha256(
            runtime_site.encode("utf-8")
        ).hexdigest(),
        effective_sys_path_sha256=sha256(
            canonical_json_bytes(list(effective_sys_path))
        ).hexdigest(),
        effective_sys_path_entry_sha256=tuple(
            sha256(path.encode("utf-8")).hexdigest() for path in effective_sys_path
        ),
        neutralized_environment=tuple(neutralized_environment),
        runtime_pth_files_sha256=_redacted_path_map(runtime_pth),
        ignored_startup_hook_files_sha256=_redacted_path_map(ignored_hooks),
        source_preflight=current_source.redacted_report_dict(),
        interpreter_installation=interpreter_installation,
        runtime_site_packages_manifest=runtime_site_packages_manifest,
        marker_sha256=sha256(canonical_json_bytes(marker)).hexdigest(),
    )


def validate_trusted_startup_policy_report(raw: object) -> dict[str, Any]:
    """Validate a path-redacted, target-independent startup policy report."""
    fields = {
        "schema_id",
        "startup_flags",
        "bytecode_write_disabled",
        "pycache_prefix_location_sha256",
        "repo_root_location_sha256",
        "python_executable_location_sha256",
        "runtime_site_packages_location_sha256",
        "effective_sys_path_sha256",
        "effective_sys_path_entry_sha256",
        "neutralized_environment",
        "runtime_pth_files_sha256",
        "ignored_startup_hook_files_sha256",
        "source_preflight",
        "interpreter_installation",
        "runtime_site_packages_manifest",
    }
    report = _strict_object(raw, allowed=fields, label="trusted startup policy")
    if report["schema_id"] != TRUSTED_STARTUP_POLICY_SCHEMA_ID:
        raise IdentityError("trusted qualification startup policy schema is invalid")
    flags = report["startup_flags"]
    if (
        not isinstance(flags, Mapping)
        or set(flags) != set(STARTUP_FLAG_NAMES)
        or flags["isolated"] != 1
        or flags["no_site"] != 1
        or flags["ignore_environment"] != 1
        or flags["no_user_site"] != 1
        or flags["safe_path"] is not True
    ):
        raise IdentityError("trusted qualification startup policy flags are invalid")
    if report["bytecode_write_disabled"] is not True:
        raise IdentityError("trusted qualification startup policy permits bytecode writes")
    for field in (
        "pycache_prefix_location_sha256",
        "repo_root_location_sha256",
        "python_executable_location_sha256",
        "runtime_site_packages_location_sha256",
        "effective_sys_path_sha256",
    ):
        if not isinstance(report[field], str) or not SHA256_PATTERN.fullmatch(report[field]):
            raise IdentityError("trusted qualification startup policy digest is invalid")
    path_entries = report["effective_sys_path_entry_sha256"]
    if (
        not isinstance(path_entries, list)
        or not path_entries
        or len(path_entries) != len(set(path_entries))
        or any(not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value) for value in path_entries)
    ):
        raise IdentityError("trusted qualification startup path policy is invalid")
    if report["neutralized_environment"] != list(AMBIENT_PYTHON_PATH_ENV):
        raise IdentityError("trusted qualification ambient environment policy is invalid")
    for field in (
        "runtime_pth_files_sha256",
        "ignored_startup_hook_files_sha256",
    ):
        values = report[field]
        if not isinstance(values, Mapping) or any(
            not isinstance(name, str)
            or not SHA256_PATTERN.fullmatch(name)
            or not isinstance(digest, str)
            or not SHA256_PATTERN.fullmatch(digest)
            for name, digest in values.items()
        ):
            raise IdentityError("trusted qualification startup artifact policy is invalid")
    _validated_redacted_source_preflight(report["source_preflight"])
    validate_interpreter_installation_identity(report["interpreter_installation"])
    validate_runtime_site_packages_identity(
        report["runtime_site_packages_manifest"]
    )
    return json.loads(canonical_json_bytes(report))


def ledger_location_sha256(path: str | Path) -> str:
    """Bind an approval to one canonical local ledger location without exposing it."""
    candidate = Path(path).expanduser()
    canonical = candidate.parent.resolve() / candidate.name
    return sha256(str(canonical).encode("utf-8")).hexdigest()


def capture_source_identity(
    repo_root: str | Path,
    *,
    expected_source_sha: str,
    dependency_paths: Sequence[str],
) -> SourceIdentity:
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise IdentityError("repository root is unavailable")
    resolved_root = _git(root, "rev-parse", "--show-toplevel")
    if Path(resolved_root).resolve() != root:
        raise IdentityError("repository root mismatch")
    source_sha = _git(root, "rev-parse", "HEAD")
    if source_sha != expected_source_sha:
        raise IdentityError("source SHA mismatch")
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if status:
        raise IdentityError("worktree is not clean")
    dependencies: dict[str, DependencyIdentity] = {}
    for relative in dependency_paths:
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise IdentityError("dependency path must be repository relative")
        candidate = root / relative
        if candidate.is_symlink():
            raise IdentityError("dependency path must not be a symlink")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise IdentityError("dependency path escapes repository") from exc
        if not resolved.is_file():
            raise IdentityError("dependency path is unavailable")
        worktree_bytes = resolved.read_bytes()
        blob_id = _git(root, "rev-parse", f"HEAD:{relative}")
        committed_bytes = _git_bytes(root, "cat-file", "blob", f"HEAD:{relative}")
        if worktree_bytes != committed_bytes:
            raise IdentityError("Git blob mismatch")
        dependencies[relative] = DependencyIdentity(
            worktree_sha256=sha256(worktree_bytes).hexdigest(),
            git_blob_id=blob_id,
        )
    return SourceIdentity(source_sha=source_sha, clean=True, dependencies=dependencies)


def _base_stdlib_paths() -> tuple[str, ...]:
    base_prefix = _canonical_path(sys.base_prefix)
    paths: list[str] = []
    for entry in sys.path:
        if not isinstance(entry, str) or not entry:
            continue
        canonical = _canonical_path(entry)
        if not _is_relative_to(Path(canonical), Path(base_prefix)):
            continue
        if "site-packages" in Path(canonical).parts:
            continue
        if canonical not in paths:
            paths.append(canonical)
    if not paths:
        raise IdentityError("base standard-library paths are unavailable")
    return tuple(paths)


def _interpreter_manifest_record(path: Path, *, name: str) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise IdentityError("interpreter installation file is invalid")
    return {"name": name, "sha256": sha256(path.read_bytes()).hexdigest()}


def _interpreter_manifest_sha256(records: Sequence[Mapping[str, str]]) -> str:
    return sha256(canonical_json_bytes(list(records))).hexdigest()


def _runtime_bytecode_source(path: Path) -> Path:
    if path.parent.name == "__pycache__":
        source_name = path.name.split(".", 1)[0] + ".py"
        return path.parent.parent / source_name
    return path.with_suffix(".py")


def capture_interpreter_installation_identity(
    *,
    stdlib_paths: Sequence[str] | None = None,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """Hash all executable, stdlib, and native-extension bytes without paths."""
    paths = tuple(_base_stdlib_paths() if stdlib_paths is None else stdlib_paths)
    executable = Path(
        _canonical_path(sys.executable if python_executable is None else python_executable)
    )
    if not executable.is_file():
        raise IdentityError("Python executable identity is unavailable")

    source_bytecode: list[dict[str, str]] = []
    archives: list[dict[str, str]] = []
    native_extensions: list[dict[str, str]] = []
    seen_files: set[str] = set()
    for root_index, raw_root in enumerate(paths):
        root = Path(_canonical_path(raw_root))
        if root.is_file():
            if root.suffix.lower() == ".zip":
                archives.append(
                    _interpreter_manifest_record(
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
                    _interpreter_manifest_record(
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
                source = _runtime_bytecode_source(candidate)
                if source.is_symlink() or not source.is_file():
                    raise IdentityError(
                        "sourceless standard-library bytecode is forbidden"
                    )
            seen_files.add(canonical)
            source_bytecode.append(
                _interpreter_manifest_record(
                    candidate,
                    name=f"stdlib/{relative.as_posix()}",
                )
            )

    if not source_bytecode or not native_extensions:
        raise IdentityError("interpreter installation identity is incomplete")
    runtime_site = _current_runtime_site_packages()
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
            *(Path(_canonical_path(path)) for path in seen_files if Path(path).suffix.lower() in NATIVE_EXTENSION_SUFFIXES),
            *site_native_extensions,
            *_python_process_image_roots(),
        ),
    )
    _require_loaded_native_images_within_closure(allowed_native_images)
    identity: dict[str, Any] = {
        "schema_id": INTERPRETER_INSTALLATION_SCHEMA_ID,
        "python_executable_sha256": sha256(executable.read_bytes()).hexdigest(),
        "stdlib_source_bytecode_sha256": _interpreter_manifest_sha256(
            source_bytecode
        ),
        "stdlib_source_bytecode_count": len(source_bytecode),
        "stdlib_archive_sha256": _interpreter_manifest_sha256(archives),
        "stdlib_archive_count": len(archives),
        "native_extension_sha256": _interpreter_manifest_sha256(native_extensions),
        "native_extension_count": len(native_extensions),
        "native_runtime_closure": native_runtime_closure,
    }
    identity["installation_sha256"] = sha256(
        canonical_json_bytes(identity)
    ).hexdigest()
    return identity


def validate_interpreter_installation_identity(raw: object) -> dict[str, Any]:
    fields = {
        "schema_id",
        "python_executable_sha256",
        "stdlib_source_bytecode_sha256",
        "stdlib_source_bytecode_count",
        "stdlib_archive_sha256",
        "stdlib_archive_count",
        "native_extension_sha256",
        "native_extension_count",
        "native_runtime_closure",
        "installation_sha256",
    }
    identity = _strict_object(
        raw,
        allowed=fields,
        label="interpreter installation identity",
    )
    if identity["schema_id"] != INTERPRETER_INSTALLATION_SCHEMA_ID:
        raise IdentityError("interpreter installation schema is invalid")
    for field in (
        "python_executable_sha256",
        "stdlib_source_bytecode_sha256",
        "stdlib_archive_sha256",
        "native_extension_sha256",
        "installation_sha256",
    ):
        if not isinstance(identity[field], str) or not SHA256_PATTERN.fullmatch(
            identity[field]
        ):
            raise IdentityError("interpreter installation digest is invalid")
    for field, minimum in (
        ("stdlib_source_bytecode_count", 1),
        ("stdlib_archive_count", 0),
        ("native_extension_count", 1),
    ):
        value = identity[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise IdentityError("interpreter installation count is invalid")
    _validate_native_runtime_closure(identity["native_runtime_closure"])
    unsigned = dict(identity)
    claimed = unsigned.pop("installation_sha256")
    if sha256(canonical_json_bytes(unsigned)).hexdigest() != claimed:
        raise IdentityError("interpreter installation aggregate is invalid")
    return json.loads(canonical_json_bytes(identity))


def _capture_native_runtime_closure(
    executable: Path,
    *,
    roots: Sequence[Path],
) -> tuple[dict[str, Any], dict[str, str]]:
    queue = [Path(_canonical_path(executable)), *map(lambda path: Path(_canonical_path(path)), roots)]
    allowed: dict[str, str] = {}
    virtual_dependencies: set[str] = set()
    while queue:
        candidate = queue.pop()
        canonical = _canonical_path(candidate)
        if canonical in allowed:
            continue
        path = Path(canonical)
        if path.is_symlink() or not path.is_file():
            raise IdentityError("native runtime dependency is unavailable")
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
    identity: dict[str, Any] = {
        "schema_id": NATIVE_RUNTIME_CLOSURE_SCHEMA_ID,
        "regular_file_count": len(records),
        "regular_files_sha256": sha256(canonical_json_bytes(records)).hexdigest(),
        "virtual_dependency_count": len(virtual_records),
        "virtual_dependencies_sha256": sha256(
            canonical_json_bytes(virtual_records)
        ).hexdigest(),
        "system_loader_identity_sha256": _system_loader_identity_sha256(
            virtual_dependencies
        ),
    }
    identity["closure_sha256"] = sha256(canonical_json_bytes(identity)).hexdigest()
    return identity, allowed


def _linked_native_dependencies(
    path: Path,
    *,
    executable: Path,
) -> tuple[list[Path], set[str]]:
    system = platform.system()
    if system == "Darwin":
        tool = DARWIN_LINK_TOOL
        commands = (
            [tool, "-L", str(path)],
            [tool, "-D", str(path)],
            [tool, "-l", str(path)],
        )
    elif system == "Linux":
        tool = LINUX_LINK_TOOL
        commands = ([tool, str(path)],)
    else:
        raise IdentityError("native runtime platform is unsupported")
    _require_externally_immutable_tool(tool)
    outputs: list[str] = []
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )  # nosec B603
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            raise IdentityError("native runtime dependency inspection failed") from exc
        if completed.stderr.strip():
            raise IdentityError("native runtime dependency inspection failed")
        outputs.append(completed.stdout)
    if system == "Darwin":
        return _parse_darwin_dependencies(
            outputs[0],
            binary=path,
            executable=executable,
            install_names=_parse_darwin_install_names(outputs[1]),
            runpaths=_parse_darwin_runpaths(
                outputs[2],
                binary=path,
                executable=executable,
            ),
        )
    return _parse_linux_dependencies(outputs[0])


def _require_externally_immutable_tool(path: str) -> None:
    candidate = Path(os.path.realpath(os.path.abspath(path)))
    if os.geteuid() == 0 or not candidate.exists() or candidate.is_symlink():
        raise IdentityError("native dependency inspection tool is mutable")
    for current in (candidate, *candidate.parents):
        try:
            metadata = current.stat()
        except OSError as exc:
            raise IdentityError("native dependency inspection tool is mutable") from exc
        if metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise IdentityError("native dependency inspection tool is mutable")
    if os.access(candidate, os.W_OK):
        raise IdentityError("native dependency inspection tool is mutable")


def _parse_darwin_install_names(output: str) -> set[str]:
    install_names: set[str] = set()
    seen_header = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if raw_line.rstrip().endswith(":"):
            seen_header = True
            continue
        if not seen_header:
            raise IdentityError("native runtime dependency inspection failed")
        install_names.add(line)
    if not seen_header:
        raise IdentityError("native runtime dependency inspection failed")
    return install_names


def _parse_darwin_runpaths(
    output: str,
    *,
    binary: Path,
    executable: Path,
) -> tuple[Path, ...]:
    runpaths: list[Path] = []
    expect_path = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line == "cmd LC_RPATH":
            expect_path = True
            continue
        if not expect_path:
            continue
        if not line.startswith("path ") or " (offset " not in line:
            continue
        raw_path = line.removeprefix("path ").split(" (offset ", 1)[0]
        if raw_path.startswith("@loader_path/"):
            candidate = binary.parent / raw_path.removeprefix("@loader_path/")
        elif raw_path.startswith("@executable_path/"):
            candidate = executable.parent / raw_path.removeprefix("@executable_path/")
        elif raw_path.startswith("/"):
            candidate = Path(raw_path)
        else:
            raise IdentityError("native runtime runpath is unsupported")
        runpaths.append(Path(os.path.realpath(os.path.abspath(candidate))))
        expect_path = False
    if expect_path:
        raise IdentityError("native runtime dependency inspection failed")
    return tuple(runpaths)


def _parse_darwin_dependencies(
    output: str,
    *,
    binary: Path,
    executable: Path,
    install_names: set[str] | None = None,
    runpaths: Sequence[Path] = (),
) -> tuple[list[Path], set[str]]:
    resolved: list[Path] = []
    virtual: set[str] = set()
    seen_header = False
    ids = set() if install_names is None else install_names
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        if not raw_line.startswith("\t"):
            if not raw_line.rstrip().endswith(":"):
                raise IdentityError("native runtime dependency inspection failed")
            seen_header = True
            continue
        if not seen_header:
            raise IdentityError("native runtime dependency inspection failed")
        line = raw_line.strip()
        load_name = line.split(" (", 1)[0]
        if load_name in ids:
            continue
        if load_name.startswith("@loader_path/"):
            candidate = binary.parent / load_name.removeprefix("@loader_path/")
        elif load_name.startswith("@executable_path/"):
            candidate = executable.parent / load_name.removeprefix("@executable_path/")
        elif load_name.startswith("@rpath/"):
            suffix = load_name.removeprefix("@rpath/")
            candidates = [runpath / suffix for runpath in runpaths]
            candidate = next((value for value in candidates if value.is_file()), None)
            if candidate is None:
                raise IdentityError("native runtime dependency is unavailable")
        elif load_name.startswith("/"):
            candidate = Path(load_name)
        else:
            raise IdentityError("native runtime dependency is unavailable")
        if candidate.is_file():
            if _canonical_path(candidate) == _canonical_path(binary):
                continue
            resolved.append(candidate)
        elif load_name.startswith(("/usr/lib/", "/System/Library/")):
            virtual.add(load_name)
        else:
            raise IdentityError("native runtime dependency is unavailable")
    if not seen_header:
        raise IdentityError("native runtime dependency inspection failed")
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
                raise IdentityError("native runtime dependency is unavailable")
            target = fields[0]
        else:
            load_name = line.split(maxsplit=1)[0]
            target = load_name
        if target.startswith("/"):
            candidate = Path(target)
            if not candidate.is_file():
                raise IdentityError("native runtime dependency is unavailable")
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
            raise IdentityError("dyld shared cache identity is unavailable")
        system_identity = {
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
    return sha256(canonical_json_bytes(system_identity)).hexdigest()


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
            raise IdentityError("loaded native image map is unavailable") from exc
        paths: set[str] = set()
        for line in lines:
            fields = line.split(maxsplit=5)
            if len(fields) == 6 and "x" in fields[1] and fields[5].startswith("/"):
                paths.add(fields[5])
        return tuple(sorted(paths))
    raise IdentityError("loaded native image platform is unsupported")


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
                raise IdentityError("loaded native image is outside the approved closure")
        else:
            raise IdentityError("loaded native image is unavailable")


def _validate_native_runtime_closure(raw: object) -> dict[str, Any]:
    fields = {
        "schema_id",
        "regular_file_count",
        "regular_files_sha256",
        "virtual_dependency_count",
        "virtual_dependencies_sha256",
        "system_loader_identity_sha256",
        "closure_sha256",
    }
    identity = _strict_object(
        raw,
        allowed=fields,
        label="native runtime closure",
    )
    if identity["schema_id"] != NATIVE_RUNTIME_CLOSURE_SCHEMA_ID:
        raise IdentityError("native runtime closure schema is invalid")
    for field in (
        "regular_files_sha256",
        "virtual_dependencies_sha256",
        "system_loader_identity_sha256",
        "closure_sha256",
    ):
        if not isinstance(identity[field], str) or not SHA256_PATTERN.fullmatch(
            identity[field]
        ):
            raise IdentityError("native runtime closure digest is invalid")
    for field, minimum in (
        ("regular_file_count", 1),
        ("virtual_dependency_count", 0),
    ):
        value = identity[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise IdentityError("native runtime closure count is invalid")
    unsigned = dict(identity)
    claimed = unsigned.pop("closure_sha256")
    if sha256(canonical_json_bytes(unsigned)).hexdigest() != claimed:
        raise IdentityError("native runtime closure aggregate is invalid")
    return json.loads(canonical_json_bytes(identity))


def _runtime_site_file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "source"
    if suffix in STDLIB_BYTECODE_SUFFIXES:
        return "bytecode"
    if suffix in NATIVE_EXTENSION_SUFFIXES:
        return "native_extension"
    return "metadata_data"


def _current_runtime_site_packages() -> Path:
    executable = Path(os.path.abspath(sys.executable))
    runtime_root = executable.parent.parent
    runtime_config = runtime_root / "pyvenv.cfg"
    if runtime_config.is_symlink() or not runtime_config.is_file():
        raise IdentityError("qualification runtime is not a virtual environment")
    site_packages = (
        runtime_root
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    if site_packages.is_symlink() or not site_packages.is_dir():
        raise IdentityError("runtime site-packages is unavailable")
    return Path(_canonical_path(site_packages))


def capture_runtime_site_packages_identity(
    runtime_site: str | Path | None = None,
) -> dict[str, Any]:
    """Hash every runtime site-packages file without exposing file paths."""
    root = (
        _current_runtime_site_packages()
        if runtime_site is None
        else Path(runtime_site)
    )
    if root.is_symlink() or not root.is_dir():
        raise IdentityError("runtime site-packages identity is unavailable")
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
                raise IdentityError("runtime site-packages symlink is forbidden")
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
        raise IdentityError("runtime site-packages identity failed") from exc
    if not records:
        raise IdentityError("runtime site-packages identity is empty")
    identity: dict[str, Any] = {
        "schema_id": RUNTIME_SITE_PACKAGES_SCHEMA_ID,
        "source_count": counts["source"],
        "bytecode_count": counts["bytecode"],
        "native_extension_count": counts["native_extension"],
        "metadata_data_count": counts["metadata_data"],
        "file_count": len(records),
        "files_sha256": sha256(canonical_json_bytes(records)).hexdigest(),
    }
    identity["manifest_sha256"] = sha256(
        canonical_json_bytes(identity)
    ).hexdigest()
    return identity


def validate_runtime_site_packages_identity(raw: object) -> dict[str, Any]:
    fields = {
        "schema_id",
        "source_count",
        "bytecode_count",
        "native_extension_count",
        "metadata_data_count",
        "file_count",
        "files_sha256",
        "manifest_sha256",
    }
    identity = _strict_object(
        raw,
        allowed=fields,
        label="runtime site-packages identity",
    )
    if identity["schema_id"] != RUNTIME_SITE_PACKAGES_SCHEMA_ID:
        raise IdentityError("runtime site-packages schema is invalid")
    for field in ("files_sha256", "manifest_sha256"):
        if not isinstance(identity[field], str) or not SHA256_PATTERN.fullmatch(
            identity[field]
        ):
            raise IdentityError("runtime site-packages digest is invalid")
    count_fields = (
        "source_count",
        "bytecode_count",
        "native_extension_count",
        "metadata_data_count",
    )
    for field in (*count_fields, "file_count"):
        value = identity[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise IdentityError("runtime site-packages count is invalid")
    if identity["file_count"] < 1 or sum(
        identity[field] for field in count_fields
    ) != identity["file_count"]:
        raise IdentityError("runtime site-packages counts are inconsistent")
    unsigned = dict(identity)
    claimed = unsigned.pop("manifest_sha256")
    if sha256(canonical_json_bytes(unsigned)).hexdigest() != claimed:
        raise IdentityError("runtime site-packages aggregate is invalid")
    return json.loads(canonical_json_bytes(identity))


def capture_environment_identity(
    *,
    repo_root: str | Path,
    expected_python: str,
    expected_uv: str,
    import_names: Sequence[str],
    expected_interpreter_installation: Mapping[str, Any] | None = None,
    expected_runtime_site_packages_manifest: Mapping[str, Any] | None = None,
) -> EnvironmentIdentity:
    if SELF_ASSERTED_RUNTIME_ENV in os.environ:
        raise IdentityError("self-asserted runtime image identity is forbidden")
    _reject_native_loader_environment()
    root = Path(repo_root).resolve()
    python_version = platform.python_version()
    if python_version != expected_python:
        raise IdentityError("Python version mismatch")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected_uv) is None:
        raise IdentityError("uv version mismatch")
    python_executable = Path(sys.executable).resolve()
    uv_location = shutil.which("uv")
    if not python_executable.is_file() or uv_location is None:
        raise IdentityError("runtime executable identity is unavailable")
    uv_executable = Path(uv_location).resolve()
    if not uv_executable.is_file():
        raise IdentityError("runtime executable identity is unavailable")
    interpreter_installation = capture_interpreter_installation_identity(
        python_executable=str(python_executable)
    )
    if expected_interpreter_installation is not None:
        expected_installation = validate_interpreter_installation_identity(
            expected_interpreter_installation
        )
        if interpreter_installation != expected_installation:
            raise IdentityError("interpreter installation identity mismatch")
    python_executable_sha256 = interpreter_installation["python_executable_sha256"]
    uv_executable_sha256 = sha256(uv_executable.read_bytes()).hexdigest()
    lock_path = root / "uv.lock"
    if not lock_path.is_file() or lock_path.is_symlink():
        raise IdentityError("uv.lock is unavailable")

    ca_file = ssl.get_default_verify_paths().cafile
    if ca_file is None or not Path(ca_file).is_file():
        raise IdentityError("CA bundle identity is unavailable")
    import_sha256: dict[str, str] = {}
    distributions: dict[str, str] = {}
    distribution_files_sha256: dict[str, str] = {}
    for name in import_names:
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            raise IdentityError("approved import is unavailable")
        origin = Path(spec.origin).resolve()
        if not origin.is_file():
            raise IdentityError("approved import origin is invalid")
        inside_repo = _is_relative_to(origin, root)
        if name.startswith("app.") and not inside_repo:
            raise IdentityError("project import escaped repository")
        if not name.startswith("app.") and inside_repo and not _is_venv_distribution(origin, root):
            raise IdentityError("third-party import is shadowed by repository")
        import_sha256[name] = sha256(origin.read_bytes()).hexdigest()
        distribution_name = name.split(".", 1)[0]
        try:
            distribution = importlib.metadata.distribution(distribution_name)
            distributions[distribution_name] = distribution.version
            distribution_files_sha256[distribution_name] = _distribution_files_sha256(
                distribution
            )
        except importlib.metadata.PackageNotFoundError:
            if not name.startswith("app."):
                raise IdentityError("approved distribution metadata is unavailable") from None

    clock = time.get_clock_info("monotonic")
    if not clock.monotonic or clock.adjustable or clock.resolution <= 0:
        raise IdentityError("monotonic clock identity is invalid")
    from app.services.voice_turn_replay import compute_gate0b_roundtrip_sha256

    golden_pcm = b"".join(
        sample.to_bytes(2, "little", signed=True)
        for sample in range(-16_000, 16_000, 1_000)
    )
    runtime_site_packages_manifest = capture_runtime_site_packages_identity()
    if expected_runtime_site_packages_manifest is not None:
        expected_site_manifest = validate_runtime_site_packages_identity(
            expected_runtime_site_packages_manifest
        )
        if runtime_site_packages_manifest != expected_site_manifest:
            raise IdentityError("runtime site-packages identity mismatch")

    return EnvironmentIdentity(
        python_version=python_version,
        uv_version=expected_uv,
        python_executable_sha256=python_executable_sha256,
        uv_executable_sha256=uv_executable_sha256,
        python_executable_location_sha256=sha256(
            str(python_executable).encode("utf-8")
        ).hexdigest(),
        uv_executable_location_sha256=sha256(str(uv_executable).encode("utf-8")).hexdigest(),
        interpreter_installation=interpreter_installation,
        runtime_site_packages_manifest=runtime_site_packages_manifest,
        platform_id=platform.system().lower() + "-" + platform.release(),
        architecture=platform.machine().lower(),
        unicode_version=unicodedata.unidata_version,
        monotonic_clock_implementation=clock.implementation,
        monotonic_clock_resolution_ns=max(1, round(clock.resolution * 1_000_000_000)),
        bytecode_write_disabled=sys.dont_write_bytecode,
        openssl_version=ssl.OPENSSL_VERSION,
        ca_bundle_sha256=sha256(Path(ca_file).read_bytes()).hexdigest(),
        lock_sha256=sha256(lock_path.read_bytes()).hexdigest(),
        codec_golden_sha256=compute_gate0b_roundtrip_sha256(golden_pcm),
        import_sha256=import_sha256,
        distributions=distributions,
        distribution_files_sha256=distribution_files_sha256,
    )


def _reject_native_loader_environment() -> None:
    if any(
        name.startswith(NATIVE_LOADER_ENV_PREFIXES)
        for name in os.environ
    ):
        raise IdentityError("native loader environment is forbidden")


def verify_campaign_approval(
    envelope: Mapping[str, Any],
    *,
    public_key: bytes,
    expected_key_id: str,
    expected_preregistration_sha256: str,
    expected_source_sha: str,
    now: datetime,
) -> CampaignApproval:
    payload, payload_digest = _verify_envelope(
        envelope,
        public_key=public_key,
        expected_key_id=expected_key_id,
    )
    allowed = {
        "schema_id",
        "scope",
        "campaign_id",
        "authorization_id",
        "nonce",
        "preregistration_sha256",
        "source_sha",
        "issued_at",
        "expires_at",
        "max_attempts",
        "max_provider_requests",
        "max_cost_microusd",
        "ledger_instance_id",
        "ledger_custodian_key_id",
        "ledger_custodian_public_key_sha256",
        "ledger_location_sha256",
        "real_caller_data_authorized",
        "runtime_wiring_authorized",
        "deployment_authorized",
        "production_authorized",
        "release_authorized",
    }
    data = _strict_object(payload, allowed=allowed, label="campaign approval")
    if data.get("schema_id") != CAMPAIGN_APPROVAL_SCHEMA_ID or data.get("scope") != QUALIFICATION_SCOPE:
        raise IdentityError("campaign approval scope is invalid")
    preregistration_sha = _sha(data.get("preregistration_sha256"), "preregistration")
    source_sha = _source_sha(data.get("source_sha"))
    if preregistration_sha != expected_preregistration_sha256 or source_sha != expected_source_sha:
        raise IdentityError("campaign approval identity mismatch")
    issued_at, expires_at = _approval_window(data, now=now)
    max_attempts = _bounded_int(data.get("max_attempts"), "max_attempts", 1, MAX_ATTEMPTS)
    max_requests = _bounded_int(
        data.get("max_provider_requests"),
        "max_provider_requests",
        1,
        MAX_REQUESTS_PER_CAMPAIGN,
    )
    max_cost = _bounded_int(
        data.get("max_cost_microusd"),
        "max_cost_microusd",
        1,
        MAX_COST_PER_CAMPAIGN_MICROUSD,
    )
    ledger_instance_id = _safe_id(data.get("ledger_instance_id"), "ledger_instance_id")
    ledger_custodian_key_id = _safe_id(
        data.get("ledger_custodian_key_id"),
        "ledger_custodian_key_id",
    )
    ledger_custodian_public_key_sha256 = _sha(
        data.get("ledger_custodian_public_key_sha256"),
        "ledger custodian public key",
    )
    ledger_location = _sha(data.get("ledger_location_sha256"), "ledger location")
    nonauthorization_fields = (
        "real_caller_data_authorized",
        "runtime_wiring_authorized",
        "deployment_authorized",
        "production_authorized",
        "release_authorized",
    )
    for field in nonauthorization_fields:
        if data.get(field) is not False:
            raise IdentityError(f"{field} must remain false")
    return CampaignApproval(
        campaign_id=_safe_id(data.get("campaign_id"), "campaign_id"),
        authorization_id=_safe_id(data.get("authorization_id"), "authorization_id"),
        nonce=_safe_id(data.get("nonce"), "nonce"),
        preregistration_sha256=preregistration_sha,
        source_sha=source_sha,
        issued_at=issued_at,
        expires_at=expires_at,
        max_attempts=max_attempts,
        max_provider_requests=max_requests,
        max_cost_microusd=max_cost,
        ledger_instance_id=ledger_instance_id,
        ledger_custodian_key_id=ledger_custodian_key_id,
        ledger_custodian_public_key_sha256=ledger_custodian_public_key_sha256,
        ledger_location_sha256=ledger_location,
        real_caller_data_authorized=False,
        runtime_wiring_authorized=False,
        deployment_authorized=False,
        production_authorized=False,
        release_authorized=False,
        signed_payload_sha256=payload_digest,
    )


def verify_attempt_authorization(
    envelope: Mapping[str, Any],
    *,
    public_key: bytes,
    expected_key_id: str,
    campaign: CampaignApproval,
    now: datetime,
) -> AttemptAuthorization:
    payload, payload_digest = _verify_envelope(
        envelope,
        public_key=public_key,
        expected_key_id=expected_key_id,
    )
    allowed = {
        "schema_id",
        "campaign_id",
        "authorization_id",
        "attempt_id",
        "attempt_index",
        "prior_attempt_id",
        "outage_enum",
        "preregistration_sha256",
        "source_sha",
        "issued_at",
        "expires_at",
        "provider_request_reservation",
        "cost_reservation_microusd",
    }
    data = _strict_object(payload, allowed=allowed, label="attempt authorization")
    if data.get("schema_id") != ATTEMPT_AUTHORIZATION_SCHEMA_ID:
        raise IdentityError("attempt authorization schema is invalid")
    campaign_id = _safe_id(data.get("campaign_id"), "campaign_id")
    authorization_id = _safe_id(data.get("authorization_id"), "authorization_id")
    if campaign_id != campaign.campaign_id or authorization_id != campaign.authorization_id:
        raise IdentityError("attempt campaign identity mismatch")
    preregistration_sha = _sha(data.get("preregistration_sha256"), "preregistration")
    source_sha = _source_sha(data.get("source_sha"))
    if preregistration_sha != campaign.preregistration_sha256 or source_sha != campaign.source_sha:
        raise IdentityError("attempt source identity mismatch")
    issued_at, expires_at = _approval_window(data, now=now)
    if expires_at > campaign.expires_at:
        raise IdentityError("attempt expiry exceeds campaign approval")
    attempt_index = _bounded_int(data.get("attempt_index"), "attempt_index", 1, campaign.max_attempts)
    prior_attempt_id = data.get("prior_attempt_id")
    outage_enum = data.get("outage_enum")
    if attempt_index == 1:
        if prior_attempt_id is not None or outage_enum is not None:
            raise IdentityError("first attempt cannot be a replacement")
    else:
        prior_attempt_id = _safe_id(prior_attempt_id, "prior_attempt_id")
        if outage_enum not in OUTAGE_ENUMS:
            raise IdentityError("replacement outage enum is invalid")
    request_reservation = _bounded_int(
        data.get("provider_request_reservation"),
        "provider_request_reservation",
        1,
        MAX_REQUESTS_PER_ATTEMPT,
    )
    cost_reservation = _bounded_int(
        data.get("cost_reservation_microusd"),
        "cost_reservation_microusd",
        1,
        MAX_COST_PER_ATTEMPT_MICROUSD,
    )
    return AttemptAuthorization(
        campaign_id=campaign_id,
        authorization_id=authorization_id,
        attempt_id=_safe_id(data.get("attempt_id"), "attempt_id"),
        attempt_index=attempt_index,
        prior_attempt_id=prior_attempt_id,
        outage_enum=outage_enum,
        preregistration_sha256=preregistration_sha,
        source_sha=source_sha,
        issued_at=issued_at,
        expires_at=expires_at,
        provider_request_reservation=request_reservation,
        cost_reservation_microusd=cost_reservation,
        signed_payload_sha256=payload_digest,
    )
def _verify_envelope(
    envelope: Mapping[str, Any],
    *,
    public_key: bytes,
    expected_key_id: str,
) -> tuple[Mapping[str, Any], str]:
    data = _strict_object(
        envelope,
        allowed={"key_id", "payload", "signature"},
        label="signed envelope",
    )
    if data.get("key_id") != expected_key_id:
        raise IdentityError("approval signing key identity mismatch")
    payload = data.get("payload")
    if not isinstance(payload, Mapping):
        raise IdentityError("signed payload must be an object")
    signature_value = data.get("signature")
    if not isinstance(signature_value, str):
        raise IdentityError("approval signature is invalid")
    try:
        signature = base64.b64decode(signature_value, validate=True)
        key = Ed25519PublicKey.from_public_bytes(public_key)
        serialized = canonical_json_bytes(payload)
        key.verify(signature, serialized)
    except (ValueError, InvalidSignature) as exc:
        raise IdentityError("approval signature is invalid") from exc
    return payload, sha256(serialized).hexdigest()


def _approval_window(data: Mapping[str, Any], *, now: datetime) -> tuple[datetime, datetime]:
    issued_at = _parse_time(data.get("issued_at"), "issued_at")
    expires_at = _parse_time(data.get("expires_at"), "expires_at")
    current = _utc(now)
    lifetime = (expires_at - issued_at).total_seconds()
    if lifetime <= 0 or lifetime > MAX_APPROVAL_LIFETIME_SECONDS:
        raise IdentityError("approval lifetime is invalid")
    if current < issued_at:
        raise IdentityError("approval is not active")
    if current >= expires_at:
        raise IdentityError("approval is expired")
    return issued_at, expires_at


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IdentityError(f"{label} must be UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise IdentityError(f"{label} is invalid") from exc
    return _utc(parsed)


def _format_time(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise IdentityError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_path(value: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.path.abspath(os.fspath(value)))


def _canonical_marker_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise IdentityError(f"trusted qualification {label} is invalid")
    canonical = _canonical_path(value)
    if value != canonical:
        raise IdentityError(f"trusted qualification {label} is not canonical")
    return canonical


def _validated_startup_flags(raw: object) -> dict[str, int | bool]:
    if not isinstance(raw, Mapping) or set(raw) != set(STARTUP_FLAG_NAMES):
        raise IdentityError("trusted qualification startup flags are invalid")
    current = {name: getattr(sys.flags, name) for name in STARTUP_FLAG_NAMES}
    for name, value in raw.items():
        if type(value) is not type(current[name]) or value != current[name]:
            raise IdentityError("trusted qualification startup flags changed")
    return dict(raw)


def _validated_effective_sys_path(raw: object) -> tuple[str, ...]:
    if (
        not isinstance(raw, list)
        or not raw
        or any(not isinstance(entry, str) or not entry for entry in raw)
    ):
        raise IdentityError("trusted qualification effective sys.path is invalid")
    canonical = tuple(_canonical_path(entry) for entry in raw)
    if tuple(raw) != canonical or len(canonical) != len(set(canonical)):
        raise IdentityError("trusted qualification effective sys.path is not canonical")
    return canonical


def _validated_startup_artifact_map(raw: object, *, label: str) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise IdentityError(f"trusted qualification {label} identities are invalid")
    identities: dict[str, str] = {}
    for path, digest in raw.items():
        if (
            not isinstance(path, str)
            or not path
            or path != _canonical_path(path)
            or not isinstance(digest, str)
            or not SHA256_PATTERN.fullmatch(digest)
        ):
            raise IdentityError(f"trusted qualification {label} identities are invalid")
        identities[path] = digest
    return identities


def _hash_startup_artifact(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise IdentityError("startup artifact is not a regular file")
    if path.stat().st_size > MAX_STARTUP_ARTIFACT_BYTES:
        raise IdentityError("startup artifact exceeds its size bound")
    return sha256(path.read_bytes()).hexdigest()


def _runtime_pth_identities(runtime_site: str) -> dict[str, str]:
    root = Path(runtime_site)
    if root.is_symlink() or not root.is_dir():
        raise IdentityError("runtime site-packages is unavailable")
    return {
        _canonical_path(path): _hash_startup_artifact(path)
        for path in sorted(root.glob("*.pth"))
    }


def _ignored_startup_hook_identities(runtime_site: str) -> dict[str, str]:
    root = Path(runtime_site)
    if root.is_symlink() or not root.is_dir():
        raise IdentityError("runtime site-packages is unavailable")
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


def _redacted_path_map(values: Mapping[str, str]) -> dict[str, str]:
    return {
        sha256(path.encode("utf-8")).hexdigest(): digest
        for path, digest in sorted(values.items())
    }


def _source_identity_marker_dict(source: SourceIdentity) -> dict[str, Any]:
    return {
        "source_sha": source.source_sha,
        "clean": source.clean,
        "dependencies": {
            name: {
                "worktree_sha256": identity.worktree_sha256,
                "git_blob_id": identity.git_blob_id,
            }
            for name, identity in sorted(source.dependencies.items())
        },
    }


def _validate_dependency_identity(raw: object, *, label: str) -> dict[str, str]:
    identity = _strict_object(
        raw,
        allowed={"worktree_sha256", "git_blob_id"},
        label=label,
    )
    if not isinstance(identity["worktree_sha256"], str) or not SHA256_PATTERN.fullmatch(
        identity["worktree_sha256"]
    ):
        raise IdentityError("source dependency worktree digest is invalid")
    if not isinstance(identity["git_blob_id"], str) or not re.fullmatch(
        r"[0-9a-f]{40,64}",
        identity["git_blob_id"],
    ):
        raise IdentityError("source dependency Git blob is invalid")
    return identity


def _validated_source_preflight_marker(raw: object) -> dict[str, Any]:
    report = _strict_object(
        raw,
        allowed={"source_sha", "clean", "dependencies"},
        label="source preflight",
    )
    if (
        not isinstance(report["source_sha"], str)
        or not SOURCE_SHA_PATTERN.fullmatch(report["source_sha"])
        or report["clean"] is not True
    ):
        raise IdentityError("source preflight identity is invalid")
    dependencies = report["dependencies"]
    if not isinstance(dependencies, Mapping) or set(dependencies) != set(
        EXECUTION_DEPENDENCY_PATHS
    ):
        raise IdentityError("source preflight dependencies are incomplete")
    validated = {
        name: _validate_dependency_identity(
            dependencies[name],
            label="source preflight dependency",
        )
        for name in EXECUTION_DEPENDENCY_PATHS
    }
    return {
        "source_sha": report["source_sha"],
        "clean": True,
        "dependencies": validated,
    }


def _validated_redacted_source_preflight(raw: object) -> dict[str, Any]:
    report = _strict_object(
        raw,
        allowed={"source_sha", "clean", "dependencies"},
        label="redacted source preflight",
    )
    if (
        not isinstance(report["source_sha"], str)
        or not SOURCE_SHA_PATTERN.fullmatch(report["source_sha"])
        or report["clean"] is not True
    ):
        raise IdentityError("redacted source preflight identity is invalid")
    dependencies = report["dependencies"]
    expected_names = {
        sha256(path.encode("utf-8")).hexdigest()
        for path in EXECUTION_DEPENDENCY_PATHS
    }
    if not isinstance(dependencies, Mapping) or set(dependencies) != expected_names:
        raise IdentityError("redacted source preflight dependencies are incomplete")
    for identity in dependencies.values():
        _validate_dependency_identity(
            identity,
            label="redacted source preflight dependency",
        )
    return json.loads(canonical_json_bytes(report))


def _strict_object(
    raw: object,
    *,
    allowed: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise IdentityError(f"{label} must be an object")
    unknown = set(raw) - allowed
    if unknown:
        raise IdentityError(f"unknown {label} field")
    missing = allowed - set(raw)
    if missing:
        raise IdentityError(f"missing {label} field")
    return dict(raw)


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_PATTERN.fullmatch(value):
        raise IdentityError(f"{label} must be a safe identifier")
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise IdentityError(f"{label} must be SHA-256")
    return value


def _source_sha(value: object) -> str:
    if not isinstance(value, str) or not SOURCE_SHA_PATTERN.fullmatch(value):
        raise IdentityError("source SHA is invalid")
    return value


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise IdentityError(f"{label} is outside its bound")
    return value


def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            [
                GIT_BINARY,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                *args,
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )  # nosec B603
    except (OSError, subprocess.CalledProcessError) as exc:
        raise IdentityError("Git identity command failed") from exc
    return completed.stdout.strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            [
                GIT_BINARY,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                *args,
            ],
            cwd=root,
            check=True,
            capture_output=True,
        )  # nosec B603
    except (OSError, subprocess.CalledProcessError) as exc:
        raise IdentityError("Git identity command failed") from exc
    return completed.stdout


def _distribution_files_sha256(distribution: importlib.metadata.Distribution) -> str:
    files = distribution.files
    if not files:
        raise IdentityError("approved distribution files are unavailable")
    identities = []
    for relative in sorted(files, key=str):
        candidate = Path(distribution.locate_file(relative))
        if candidate.is_symlink() or not candidate.is_file():
            raise IdentityError("approved distribution file identity is unavailable")
        identities.append(
            {
                "name": str(relative),
                "sha256": sha256(candidate.read_bytes()).hexdigest(),
            }
        )
    return sha256(canonical_json_bytes(identities)).hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_venv_distribution(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return relative.parts[:1] == (".venv",) and "site-packages" in relative.parts
