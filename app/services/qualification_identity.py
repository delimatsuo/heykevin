"""Source, environment, approval, and attempt-ledger identity for Gate 0B."""

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
import ssl
# Subprocess use is limited to fixed identity-tool argv with shell execution disabled.
import subprocess  # nosec B404
from typing import Any, Mapping, Sequence
import unicodedata

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.services.caller_turn_qualification import CampaignPhase


CAMPAIGN_APPROVAL_SCHEMA_ID = "gate_0b_campaign_approval_v1"
ATTEMPT_AUTHORIZATION_SCHEMA_ID = "gate_0b_attempt_authorization_v1"
LEDGER_SCHEMA_ID = "gate_0b_attempt_ledger_v1"
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
                name: {
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
    platform_id: str
    architecture: str
    unicode_version: str
    openssl_version: str
    ca_bundle_sha256: str
    lock_sha256: str
    import_sha256: dict[str, str]
    distributions: dict[str, str]

    def redacted_report_dict(self) -> dict[str, Any]:
        return {
            "python_version": self.python_version,
            "uv_version": self.uv_version,
            "platform_id": self.platform_id,
            "architecture": self.architecture,
            "unicode_version": self.unicode_version,
            "openssl_version": self.openssl_version,
            "ca_bundle_sha256": self.ca_bundle_sha256,
            "lock_sha256": self.lock_sha256,
            "import_sha256": dict(sorted(self.import_sha256.items())),
            "distributions": dict(sorted(self.distributions.items())),
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
    lock_path = root / "uv.lock"
    if not lock_path.is_file() or lock_path.is_symlink():
        raise IdentityError("uv.lock is unavailable")

    ca_file = ssl.get_default_verify_paths().cafile
    if ca_file is None or not Path(ca_file).is_file():
        raise IdentityError("CA bundle identity is unavailable")
    import_sha256: dict[str, str] = {}
    distributions: dict[str, str] = {}
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
            distributions[distribution_name] = importlib.metadata.version(distribution_name)
        except importlib.metadata.PackageNotFoundError:
            if not name.startswith("app."):
                raise IdentityError("approved distribution metadata is unavailable") from None

    return EnvironmentIdentity(
        python_version=python_version,
        uv_version=expected_uv,
        platform_id=platform.system().lower() + "-" + platform.release(),
        architecture=platform.machine().lower(),
        unicode_version=unicodedata.unidata_version,
        openssl_version=ssl.OPENSSL_VERSION,
        ca_bundle_sha256=sha256(Path(ca_file).read_bytes()).hexdigest(),
        lock_sha256=sha256(lock_path.read_bytes()).hexdigest(),
        import_sha256=import_sha256,
        distributions=distributions,
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


class AttemptLedger:
    """Single-host, lock-protected, hash-chained attempt consumption ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def claim_attempt(
        self,
        *,
        campaign: CampaignApproval,
        authorization: AttemptAuthorization,
        phase: CampaignPhase,
        holdout_materialized: bool,
        now: datetime,
    ) -> AttemptClaim:
        if not isinstance(phase, CampaignPhase):
            raise IdentityError("claim phase is invalid")
        if authorization.campaign_id != campaign.campaign_id:
            raise IdentityError("attempt campaign mismatch")
        if ledger_location_sha256(self.path) != campaign.ledger_location_sha256:
            raise IdentityError("attempt ledger location mismatch")
        if not authorization.issued_at <= _utc(now) <= authorization.expires_at:
            raise IdentityError("attempt authorization is expired or not active")
        with self._exclusive_lock():
            ledger = self._read_or_initialize(campaign)
            self._validate_ledger(ledger)
            records = ledger["records"]
            claims = [record for record in records if record["event"] == "claim"]
            outcomes = {record["attempt_id"]: record for record in records if record["event"] == "outcome"}
            if any(record["attempt_id"] == authorization.attempt_id for record in claims):
                raise IdentityError("attempt authorization already consumed")
            active = [record for record in claims if record["attempt_id"] not in outcomes]
            if active:
                raise IdentityError("active attempt blocks concurrent claim")
            expected_index = len(claims) + 1
            if authorization.attempt_index != expected_index:
                raise IdentityError("attempt index is not sequential")
            total_requests = sum(record["provider_requests_reserved"] for record in claims)
            total_cost = sum(record["cost_reserved_microusd"] for record in claims)
            if total_requests + authorization.provider_request_reservation > campaign.max_provider_requests:
                raise IdentityError("campaign request reservation exceeded")
            if total_cost + authorization.cost_reservation_microusd > campaign.max_cost_microusd:
                raise IdentityError("campaign cost reservation exceeded")

            if authorization.attempt_index > 1:
                if phase is not CampaignPhase.DEVELOPMENT_COLLECTION or holdout_materialized:
                    raise IdentityError("replacement is allowed only before policy lock and holdout")
                prior = authorization.prior_attempt_id
                if prior is None or not claims or claims[-1]["attempt_id"] != prior:
                    raise IdentityError("replacement prior attempt mismatch")
                prior_outcome = outcomes.get(prior)
                if (
                    prior_outcome is None
                    or prior_outcome["outcome"] != "infrastructure_outage"
                    or prior_outcome["outage_enum"] != authorization.outage_enum
                ):
                    raise IdentityError("replacement lacks matching infrastructure outage")
            elif phase is not CampaignPhase.DEVELOPMENT_COLLECTION or holdout_materialized:
                raise IdentityError("first attempt must begin before policy lock and holdout")

            lease_id = sha256(
                canonical_json_bytes(
                    {
                        "campaign_id": campaign.campaign_id,
                        "attempt_id": authorization.attempt_id,
                        "authorization_sha256": authorization.signed_payload_sha256,
                    }
                )
            ).hexdigest()
            self._append_record(
                ledger,
                {
                    "event": "claim",
                    "attempt_id": authorization.attempt_id,
                    "attempt_index": authorization.attempt_index,
                    "lease_id": lease_id,
                    "phase": phase.value,
                    "holdout_materialized": holdout_materialized,
                    "provider_requests_reserved": authorization.provider_request_reservation,
                    "cost_reserved_microusd": authorization.cost_reservation_microusd,
                    "authorization_sha256": authorization.signed_payload_sha256,
                    "prior_attempt_id": authorization.prior_attempt_id,
                    "outage_enum": authorization.outage_enum,
                    "at": _format_time(now),
                },
            )
            self._write(ledger)
            return AttemptClaim(
                campaign_id=campaign.campaign_id,
                attempt_id=authorization.attempt_id,
                attempt_index=authorization.attempt_index,
                lease_id=lease_id,
                provider_requests_reserved=authorization.provider_request_reservation,
                cost_reserved_microusd=authorization.cost_reservation_microusd,
            )

    def record_outcome(
        self,
        claim: AttemptClaim,
        *,
        outcome: str,
        outage_enum: str | None,
        actual_provider_requests: int,
        actual_cost_microusd: int,
        now: datetime,
    ) -> None:
        if outcome not in {"completed", "failed", "infrastructure_outage", "invalidated"}:
            raise IdentityError("attempt outcome is invalid")
        if outcome == "infrastructure_outage":
            if outage_enum not in OUTAGE_ENUMS:
                raise IdentityError("infrastructure outage enum is invalid")
        elif outage_enum is not None:
            raise IdentityError("outage enum is allowed only for infrastructure outage")
        with self._exclusive_lock():
            ledger = self._read()
            self._validate_ledger(ledger)
            claims = [record for record in ledger["records"] if record["event"] == "claim"]
            outcomes = [record for record in ledger["records"] if record["event"] == "outcome"]
            matching = next(
                (
                    record
                    for record in reversed(claims)
                    if record["attempt_id"] == claim.attempt_id
                    and record["lease_id"] == claim.lease_id
                ),
                None,
            )
            if matching is None:
                raise IdentityError("attempt claim is not present in ledger")
            if any(record["attempt_id"] == claim.attempt_id for record in outcomes):
                raise IdentityError("attempt outcome is already recorded")
            requests = _bounded_int(
                actual_provider_requests,
                "actual_provider_requests",
                0,
                matching["provider_requests_reserved"],
            )
            cost = _bounded_int(
                actual_cost_microusd,
                "actual_cost_microusd",
                0,
                matching["cost_reserved_microusd"],
            )
            self._append_record(
                ledger,
                {
                    "event": "outcome",
                    "attempt_id": claim.attempt_id,
                    "attempt_index": claim.attempt_index,
                    "lease_id": claim.lease_id,
                    "outcome": outcome,
                    "outage_enum": outage_enum,
                    "actual_provider_requests": requests,
                    "actual_cost_microusd": cost,
                    "at": _format_time(now),
                },
            )
            self._write(ledger)

    def snapshot(self) -> dict[str, Any]:
        with self._exclusive_lock():
            ledger = self._read()
            self._validate_ledger(ledger)
            return json.loads(json.dumps(ledger))

    def _read_or_initialize(self, campaign: CampaignApproval) -> dict[str, Any]:
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise IdentityError("attempt ledger paths must not be symlinks")
        if self.path.exists():
            ledger = self._read()
            if (
                ledger.get("campaign_id") != campaign.campaign_id
                or ledger.get("authorization_id") != campaign.authorization_id
                or ledger.get("campaign_approval_sha256") != campaign.signed_payload_sha256
                or ledger.get("max_attempts") != campaign.max_attempts
                or ledger.get("max_provider_requests") != campaign.max_provider_requests
                or ledger.get("max_cost_microusd") != campaign.max_cost_microusd
                or ledger.get("ledger_location_sha256") != campaign.ledger_location_sha256
            ):
                raise IdentityError("ledger campaign identity mismatch")
            return ledger
        return {
            "schema_id": LEDGER_SCHEMA_ID,
            "campaign_id": campaign.campaign_id,
            "authorization_id": campaign.authorization_id,
            "campaign_approval_sha256": campaign.signed_payload_sha256,
            "ledger_location_sha256": campaign.ledger_location_sha256,
            "max_attempts": campaign.max_attempts,
            "max_provider_requests": campaign.max_provider_requests,
            "max_cost_microusd": campaign.max_cost_microusd,
            "records": [],
            "head_hash": "0" * 64,
        }

    def _read(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise IdentityError("attempt ledger path must not be a symlink")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IdentityError("attempt ledger is unavailable or invalid") from exc
        if not isinstance(value, dict):
            raise IdentityError("attempt ledger must be an object")
        return value

    def _validate_ledger(self, ledger: Mapping[str, Any]) -> None:
        allowed = {
            "schema_id",
            "campaign_id",
            "authorization_id",
            "campaign_approval_sha256",
            "ledger_location_sha256",
            "max_attempts",
            "max_provider_requests",
            "max_cost_microusd",
            "records",
            "head_hash",
        }
        data = _strict_object(ledger, allowed=allowed, label="attempt ledger")
        if data.get("schema_id") != LEDGER_SCHEMA_ID:
            raise IdentityError("attempt ledger schema is invalid")
        _safe_id(data.get("campaign_id"), "ledger campaign_id")
        _safe_id(data.get("authorization_id"), "ledger authorization_id")
        _sha(data.get("campaign_approval_sha256"), "campaign approval")
        _sha(data.get("ledger_location_sha256"), "ledger location")
        _bounded_int(data.get("max_attempts"), "ledger max_attempts", 1, MAX_ATTEMPTS)
        _bounded_int(
            data.get("max_provider_requests"),
            "ledger max_provider_requests",
            1,
            MAX_REQUESTS_PER_CAMPAIGN,
        )
        _bounded_int(
            data.get("max_cost_microusd"),
            "ledger max_cost_microusd",
            1,
            MAX_COST_PER_CAMPAIGN_MICROUSD,
        )
        records = data.get("records")
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            raise IdentityError("attempt ledger records are invalid")
        previous = "0" * 64
        for sequence, record in enumerate(records, start=1):
            if record.get("sequence") != sequence or record.get("previous_hash") != previous:
                raise IdentityError("ledger hash chain is invalid")
            record_hash = record.get("record_hash")
            if not isinstance(record_hash, str):
                raise IdentityError("ledger hash chain is invalid")
            unsigned = {key: value for key, value in record.items() if key != "record_hash"}
            expected = sha256(canonical_json_bytes(unsigned)).hexdigest()
            if record_hash != expected:
                raise IdentityError("ledger hash chain is invalid")
            previous = record_hash
        if data.get("head_hash") != previous:
            raise IdentityError("ledger hash chain is invalid")

    def _append_record(self, ledger: dict[str, Any], record: dict[str, Any]) -> None:
        previous = ledger["head_hash"]
        entry = {
            "sequence": len(ledger["records"]) + 1,
            "previous_hash": previous,
            **record,
        }
        entry["record_hash"] = sha256(canonical_json_bytes(entry)).hexdigest()
        ledger["records"].append(entry)
        ledger["head_hash"] = entry["record_hash"]

    def _write(self, ledger: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise IdentityError("attempt ledger path must not be a symlink")
        temporary = self.path.with_suffix(self.path.suffix + f".tmp.{os.getpid()}")
        payload = canonical_json_bytes(ledger) + b"\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except OSError as exc:
            raise IdentityError("temporary ledger path is unavailable") from exc
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise IdentityError("attempt ledger write did not make progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise IdentityError("attempt ledger replacement failed") from exc
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _exclusive_lock(self):
        return _FileLock(self.lock_path)


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def __enter__(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise IdentityError("attempt ledger lock must not be a symlink")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise IdentityError("attempt ledger lock is unavailable") from exc
        self._file = os.fdopen(descriptor, "a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, traceback):
        import fcntl

        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
        return False


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
