"""Source, environment, and signed approval identity for Gate 0B."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import ssl
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


class IdentityError(ValueError):
    """Raised when execution identity or authorization cannot be trusted."""


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
    runtime_image_kind: str
    runtime_image_sha256: str
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
            "runtime_image_kind": self.runtime_image_kind,
            "runtime_image_sha256": self.runtime_image_sha256,
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
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
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
        blob_id = _git(root, "rev-parse", f"HEAD:{relative}")
        worktree_blob_id = _git(root, "hash-object", relative)
        if blob_id != worktree_blob_id:
            raise IdentityError("Git blob mismatch")
        dependencies[relative] = DependencyIdentity(
            worktree_sha256=sha256(resolved.read_bytes()).hexdigest(),
            git_blob_id=blob_id,
        )
    return SourceIdentity(source_sha=source_sha, clean=True, dependencies=dependencies)


def capture_environment_identity(
    *,
    repo_root: str | Path,
    expected_python: str,
    expected_uv: str,
    import_names: Sequence[str],
) -> EnvironmentIdentity:
    root = Path(repo_root).resolve()
    python_version = platform.python_version()
    if python_version != expected_python:
        raise IdentityError("Python version mismatch")
    uv_output = _command("uv", "--version")
    match = re.fullmatch(r"uv ([0-9]+\.[0-9]+\.[0-9]+)(?: .*)?", uv_output)
    if match is None or match.group(1) != expected_uv:
        raise IdentityError("uv version mismatch")
    python_executable = Path(sys.executable).resolve()
    uv_location = shutil.which("uv")
    if not python_executable.is_file() or uv_location is None:
        raise IdentityError("runtime executable identity is unavailable")
    uv_executable = Path(uv_location).resolve()
    if not uv_executable.is_file():
        raise IdentityError("runtime executable identity is unavailable")
    python_executable_sha256 = sha256(python_executable.read_bytes()).hexdigest()
    uv_executable_sha256 = sha256(uv_executable.read_bytes()).hexdigest()
    container_digest = os.environ.get("QUALIFICATION_CONTAINER_IMAGE_DIGEST")
    if container_digest is None:
        runtime_image_kind = "interpreter"
        runtime_image_sha256 = python_executable_sha256
    else:
        match = re.fullmatch(r"sha256:([0-9a-f]{64})", container_digest)
        if match is None:
            raise IdentityError("container image identity is invalid")
        runtime_image_kind = "container"
        runtime_image_sha256 = match.group(1)
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

    return EnvironmentIdentity(
        python_version=python_version,
        uv_version=expected_uv,
        python_executable_sha256=python_executable_sha256,
        uv_executable_sha256=uv_executable_sha256,
        python_executable_location_sha256=sha256(
            str(python_executable).encode("utf-8")
        ).hexdigest(),
        uv_executable_location_sha256=sha256(str(uv_executable).encode("utf-8")).hexdigest(),
        runtime_image_kind=runtime_image_kind,
        runtime_image_sha256=runtime_image_sha256,
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
            [GIT_BINARY, *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )  # nosec B603
    except (OSError, subprocess.CalledProcessError) as exc:
        raise IdentityError("Git identity command failed") from exc
    return completed.stdout.strip()


def _command(*args: str) -> str:
    try:
        completed = subprocess.run(
            list(args),
            check=True,
            capture_output=True,
            text=True,
        )  # nosec B603
    except (OSError, subprocess.CalledProcessError) as exc:
        raise IdentityError("environment identity command failed") from exc
    return completed.stdout.strip()


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
