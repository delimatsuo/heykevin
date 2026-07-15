"""Externally signed Gate 0B ledger receipts and replay validation."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Any, Mapping, Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.services.qualification_identity import canonical_json_bytes


LEDGER_EXPORT_SCHEMA_ID = "gate_0b_custodian_ledger_export_v1"
LEDGER_RECORD_SCHEMA_ID = "gate_0b_custodian_ledger_record_v1"
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SOURCE_SHA = re.compile(r"[0-9a-f]{40,64}")
PHASES = {
    "preregistered",
    "development_collection",
    "policy_selection_locked",
    "holdout_collection",
    "completed",
    "aborted",
    "invalidated",
}
TERMINAL_PHASES = {"completed", "aborted", "invalidated"}
OUTAGE_ENUMS = {
    "provider_dns_outage",
    "provider_control_plane_outage",
    "qualification_host_failure",
}
POLICIES_MS = {100, 250, 500, 750}


class CustodyLedgerError(ValueError):
    """Raised when an exported custodian ledger cannot prove its history."""


@dataclass(frozen=True, slots=True)
class LedgerReceipt:
    event: str
    sequence: int
    record_sha256: str
    phase: str


@dataclass(frozen=True, slots=True)
class LedgerCustodyIdentity:
    ledger_instance_id: str
    key_id: str
    public_key_sha256: str
    ledger_location_sha256: str


@dataclass(frozen=True, slots=True)
class CustodyLedgerState:
    phase: str
    phase_history: tuple[str, ...]
    attempt_ids: tuple[str, ...]
    active_attempt_id: str | None
    completed_attempt_id: str | None
    campaign_approval_sha256: str
    attempt_authorization_sha256: str | None
    attempt_claimed_at: datetime | None
    selected_policy_ms: int | None
    policy_lock_sha256: str | None
    development_capsule_sha256: str | None
    development_ledger_head_sha256: str | None
    holdout_manifest_sha256: str | None
    holdout_capsule_sha256: str | None
    development_usage_evidence_sha256: str | None
    final_usage_evidence_sha256: str | None
    actual_provider_requests: int
    actual_cost_microusd: int
    final_ledger_head_sha256: str


@runtime_checkable
class LedgerCustodyClient(Protocol):
    """Local IPC boundary owned by a separate durable ledger custodian."""

    def identity(self) -> LedgerCustodyIdentity: ...

    def claim_attempt(self, **values: Any) -> Any: ...

    def record_development_checkpoint(self, **values: Any) -> LedgerReceipt: ...

    def record_policy_lock(self, **values: Any) -> LedgerReceipt: ...

    def release_holdout(self, **values: Any) -> LedgerReceipt: ...

    def record_terminal_outcome(self, **values: Any) -> LedgerReceipt: ...

    def export_snapshot(self) -> Mapping[str, Any]: ...


@dataclass(slots=True)
class _ReplayState:
    phase: str = "preregistered"
    history: list[str] | None = None
    attempts: list[str] | None = None
    active_attempt: str | None = None
    active_attempt_index: int | None = None
    active_request_reservation: int = 0
    active_cost_reservation: int = 0
    active_authorization: str | None = None
    active_claimed_at: datetime | None = None
    reserved_requests: int = 0
    reserved_cost: int = 0
    last_outage_attempt: str | None = None
    last_outage_enum: str | None = None
    development_checkpoint_attempt: str | None = None
    development_capsule: str | None = None
    development_usage: str | None = None
    development_requests: int = 0
    development_cost: int = 0
    development_head: str | None = None
    selected_policy: int | None = None
    policy_lock: str | None = None
    policy_lock_receipt: str | None = None
    holdout_manifest: str | None = None
    holdout_capsule: str | None = None
    campaign_approval: str | None = None
    completed_attempt: str | None = None
    completed_authorization: str | None = None
    completed_claimed_at: datetime | None = None
    final_usage: str | None = None
    actual_requests: int = 0
    actual_cost: int = 0
    max_attempts: int = 0
    max_requests: int = 0
    max_cost: int = 0
    genesis_seen: bool = False

    def __post_init__(self) -> None:
        self.history = ["preregistered"]
        self.attempts = []

    def transition(self, target: str) -> None:
        if target != self.phase:
            self.phase = target
            assert self.history is not None
            self.history.append(target)


def validate_custody_ledger_snapshot(
    raw: Mapping[str, Any],
    *,
    public_key: bytes,
    expected_key_id: str,
    expected_ledger_instance_id: str,
) -> CustodyLedgerState:
    """Verify every custodian receipt and derive campaign state by strict replay."""
    fields = {
        "schema_id",
        "ledger_instance_id",
        "ledger_key_id",
        "campaign_id",
        "authorization_id",
        "preregistration_sha256",
        "source_sha",
        "ledger_location_sha256",
        "records",
        "head_hash",
    }
    data = _strict_mapping(raw, fields, label="custodian ledger export")
    if data["schema_id"] != LEDGER_EXPORT_SCHEMA_ID:
        raise CustodyLedgerError("custodian ledger schema is invalid")
    ledger_instance = _safe_id(data["ledger_instance_id"], label="ledger instance")
    if ledger_instance != _safe_id(
        expected_ledger_instance_id,
        label="expected ledger instance",
    ):
        raise CustodyLedgerError("ledger instance identity mismatch")
    key_id = _safe_id(data["ledger_key_id"], label="ledger key")
    if key_id != _safe_id(expected_key_id, label="expected ledger key"):
        raise CustodyLedgerError("ledger custodian key identity mismatch")
    identities = {
        "ledger_instance_id": ledger_instance,
        "campaign_id": _safe_id(data["campaign_id"], label="campaign"),
        "authorization_id": _safe_id(data["authorization_id"], label="authorization"),
        "preregistration_sha256": _digest(
            data["preregistration_sha256"],
            label="preregistration",
        ),
        "source_sha": _source_digest(data["source_sha"]),
        "ledger_location_sha256": _digest(
            data["ledger_location_sha256"],
            label="ledger location",
        ),
    }
    records = data["records"]
    if not isinstance(records, list) or not 1 <= len(records) <= 32:
        raise CustodyLedgerError("custodian ledger records are invalid")
    try:
        verifier = Ed25519PublicKey.from_public_bytes(public_key)
    except (TypeError, ValueError) as exc:
        raise CustodyLedgerError("ledger custodian public key is invalid") from exc

    replay = _ReplayState()
    previous = "0" * 64
    previous_time: datetime | None = None
    for sequence, envelope in enumerate(records, start=1):
        payload, record_sha = _verify_record(
            envelope,
            verifier=verifier,
            expected_key_id=key_id,
        )
        if any(payload[field] != value for field, value in identities.items()):
            raise CustodyLedgerError("ledger record identity mismatch")
        if payload["sequence"] != sequence or payload["previous_hash"] != previous:
            raise CustodyLedgerError("ledger receipt chain is invalid")
        record_time = _utc_time(payload["at"])
        if previous_time is not None and record_time < previous_time:
            raise CustodyLedgerError("ledger receipt time moved backward")
        previous_time = record_time
        _replay_record(
            replay,
            payload,
            previous_hash=previous,
            record_time=record_time,
        )
        previous = record_sha
    if data["head_hash"] != previous:
        raise CustodyLedgerError("ledger export head is invalid")
    if not replay.genesis_seen:
        raise CustodyLedgerError("ledger genesis receipt is missing")

    assert replay.history is not None
    assert replay.attempts is not None
    assert replay.campaign_approval is not None
    return CustodyLedgerState(
        phase=replay.phase,
        phase_history=tuple(replay.history),
        attempt_ids=tuple(replay.attempts),
        active_attempt_id=replay.active_attempt,
        completed_attempt_id=replay.completed_attempt,
        campaign_approval_sha256=replay.campaign_approval,
        attempt_authorization_sha256=(
            replay.completed_authorization or replay.active_authorization
        ),
        attempt_claimed_at=replay.completed_claimed_at or replay.active_claimed_at,
        selected_policy_ms=replay.selected_policy,
        policy_lock_sha256=replay.policy_lock,
        development_capsule_sha256=replay.development_capsule,
        development_ledger_head_sha256=replay.development_head,
        holdout_manifest_sha256=replay.holdout_manifest,
        holdout_capsule_sha256=replay.holdout_capsule,
        development_usage_evidence_sha256=replay.development_usage,
        final_usage_evidence_sha256=replay.final_usage,
        actual_provider_requests=replay.actual_requests,
        actual_cost_microusd=replay.actual_cost,
        final_ledger_head_sha256=previous,
    )


def _verify_record(
    raw: object,
    *,
    verifier: Ed25519PublicKey,
    expected_key_id: str,
) -> tuple[dict[str, Any], str]:
    envelope = _strict_mapping(
        raw,
        {"key_id", "payload", "signature"},
        label="ledger receipt",
    )
    if envelope["key_id"] != expected_key_id:
        raise CustodyLedgerError("ledger receipt key identity mismatch")
    payload = _strict_mapping(
        envelope["payload"],
        {
            "schema_id",
            "ledger_instance_id",
            "campaign_id",
            "authorization_id",
            "preregistration_sha256",
            "source_sha",
            "ledger_location_sha256",
            "sequence",
            "previous_hash",
            "event",
            "phase_before",
            "phase_after",
            "attempt_id",
            "at",
            "body",
        },
        label="ledger receipt payload",
    )
    if payload["schema_id"] != LEDGER_RECORD_SCHEMA_ID:
        raise CustodyLedgerError("ledger receipt schema is invalid")
    signature = envelope["signature"]
    if not isinstance(signature, str) or len(signature) > 256:
        raise CustodyLedgerError("ledger receipt signature is invalid")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
        verifier.verify(signature_bytes, canonical_json_bytes(payload))
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise CustodyLedgerError("ledger receipt signature is invalid") from exc
    return payload, sha256(canonical_json_bytes(payload)).hexdigest()


def _replay_record(
    state: _ReplayState,
    payload: Mapping[str, Any],
    *,
    previous_hash: str,
    record_time: datetime,
) -> None:
    event = _safe_id(payload["event"], label="ledger event")
    before = _phase(payload["phase_before"])
    after = _phase(payload["phase_after"])
    attempt_id = payload["attempt_id"]
    if attempt_id is not None:
        attempt_id = _safe_id(attempt_id, label="attempt")
    if state.phase in TERMINAL_PHASES:
        raise CustodyLedgerError("terminal ledger cannot accept another receipt")
    if before != state.phase:
        raise CustodyLedgerError("ledger phase history is invalid")
    body = payload["body"]
    if event == "genesis":
        _replay_genesis(state, body, before=before, after=after, attempt_id=attempt_id)
    elif not state.genesis_seen:
        raise CustodyLedgerError("ledger genesis receipt is missing")
    elif event == "claim":
        _replay_claim(
            state,
            body,
            before=before,
            after=after,
            attempt_id=attempt_id,
            record_time=record_time,
        )
    elif event == "development_checkpoint":
        _replay_development_checkpoint(
            state,
            body,
            before=before,
            after=after,
            attempt_id=attempt_id,
        )
    elif event == "policy_lock":
        _replay_policy_lock(
            state,
            body,
            before=before,
            after=after,
            attempt_id=attempt_id,
            previous_hash=previous_hash,
        )
    elif event == "holdout_release":
        _replay_holdout_release(
            state,
            body,
            before=before,
            after=after,
            attempt_id=attempt_id,
            previous_hash=previous_hash,
        )
    elif event == "terminal_outcome":
        _replay_terminal_outcome(
            state,
            body,
            before=before,
            after=after,
            attempt_id=attempt_id,
        )
    else:
        raise CustodyLedgerError("ledger event is invalid")


def _replay_genesis(
    state: _ReplayState,
    raw: object,
    *,
    before: str,
    after: str,
    attempt_id: str | None,
) -> None:
    body = _strict_mapping(
        raw,
        {
            "campaign_approval_sha256",
            "max_attempts",
            "max_provider_requests",
            "max_cost_microusd",
        },
        label="ledger genesis body",
    )
    if state.genesis_seen or before != "preregistered" or after != before or attempt_id is not None:
        raise CustodyLedgerError("ledger genesis receipt is invalid")
    state.campaign_approval = _digest(
        body["campaign_approval_sha256"],
        label="campaign approval",
    )
    state.max_attempts = _bounded_int(body["max_attempts"], label="max attempts", maximum=3)
    state.max_requests = _bounded_int(
        body["max_provider_requests"],
        label="max provider requests",
        maximum=384,
    )
    state.max_cost = _bounded_int(
        body["max_cost_microusd"],
        label="max cost",
        maximum=30_000_000,
    )
    state.genesis_seen = True


def _replay_claim(
    state: _ReplayState,
    raw: object,
    *,
    before: str,
    after: str,
    attempt_id: str | None,
    record_time: datetime,
) -> None:
    body = _strict_mapping(
        raw,
        {
            "attempt_index",
            "authorization_sha256",
            "prior_attempt_id",
            "outage_enum",
            "provider_requests_reserved",
            "cost_reserved_microusd",
        },
        label="ledger claim body",
    )
    if attempt_id is None or state.active_attempt is not None or after != "development_collection":
        raise CustodyLedgerError("ledger claim state is invalid")
    if before not in {"preregistered", "development_collection"}:
        raise CustodyLedgerError("ledger claim phase is invalid")
    assert state.attempts is not None
    index = _bounded_int(body["attempt_index"], label="attempt index", maximum=3)
    if index != len(state.attempts) + 1 or index > state.max_attempts:
        raise CustodyLedgerError("ledger attempt index is invalid")
    authorization = _digest(body["authorization_sha256"], label="attempt authorization")
    prior = body["prior_attempt_id"]
    outage = body["outage_enum"]
    if index == 1:
        if before != "preregistered" or prior is not None or outage is not None:
            raise CustodyLedgerError("first ledger claim is invalid")
    else:
        if (
            before != "development_collection"
            or _safe_id(prior, label="prior attempt") != state.last_outage_attempt
            or outage not in OUTAGE_ENUMS
            or outage != state.last_outage_enum
        ):
            raise CustodyLedgerError("replacement ledger claim lacks a matching outage")
    request_reservation = _bounded_int(
        body["provider_requests_reserved"],
        label="request reservation",
        maximum=128,
    )
    cost_reservation = _bounded_int(
        body["cost_reserved_microusd"],
        label="cost reservation",
        maximum=10_000_000,
    )
    if (
        state.reserved_requests + request_reservation > state.max_requests
        or state.reserved_cost + cost_reservation > state.max_cost
    ):
        raise CustodyLedgerError("ledger campaign reservation is exhausted")
    state.reserved_requests += request_reservation
    state.reserved_cost += cost_reservation
    state.active_attempt = attempt_id
    state.active_attempt_index = index
    state.active_request_reservation = request_reservation
    state.active_cost_reservation = cost_reservation
    state.active_authorization = authorization
    state.active_claimed_at = record_time
    state.attempts.append(attempt_id)
    state.last_outage_attempt = None
    state.last_outage_enum = None
    state.development_checkpoint_attempt = None
    state.development_capsule = None
    state.development_usage = None
    state.development_requests = 0
    state.development_cost = 0
    state.final_usage = None
    state.actual_requests = 0
    state.actual_cost = 0
    state.transition(after)


def _replay_development_checkpoint(
    state: _ReplayState,
    raw: object,
    *,
    before: str,
    after: str,
    attempt_id: str | None,
) -> None:
    body = _strict_mapping(
        raw,
        {
            "development_capsule_sha256",
            "usage_evidence_sha256",
            "actual_provider_requests",
            "actual_cost_microusd",
        },
        label="development checkpoint body",
    )
    if (
        before != "development_collection"
        or after != before
        or attempt_id != state.active_attempt
        or state.development_checkpoint_attempt is not None
    ):
        raise CustodyLedgerError("development checkpoint state is invalid")
    requests = _bounded_int(
        body["actual_provider_requests"],
        label="development requests",
        maximum=state.active_request_reservation,
        minimum=0,
    )
    cost = _bounded_int(
        body["actual_cost_microusd"],
        label="development cost",
        maximum=state.active_cost_reservation,
        minimum=0,
    )
    state.development_checkpoint_attempt = attempt_id
    state.development_capsule = _digest(
        body["development_capsule_sha256"],
        label="development capsule",
    )
    state.development_usage = _digest(body["usage_evidence_sha256"], label="usage evidence")
    state.development_requests = requests
    state.development_cost = cost


def _replay_policy_lock(
    state: _ReplayState,
    raw: object,
    *,
    before: str,
    after: str,
    attempt_id: str | None,
    previous_hash: str,
) -> None:
    body = _strict_mapping(
        raw,
        {
            "development_ledger_head_sha256",
            "development_capsule_sha256",
            "selected_policy_ms",
            "policy_lock_sha256",
        },
        label="policy lock body",
    )
    if (
        before != "development_collection"
        or after != "policy_selection_locked"
        or attempt_id != state.active_attempt
        or state.development_checkpoint_attempt != state.active_attempt
    ):
        raise CustodyLedgerError("policy lock state is invalid")
    development_head = _digest(
        body["development_ledger_head_sha256"],
        label="development ledger head",
    )
    if development_head != previous_hash:
        raise CustodyLedgerError("policy lock development head is invalid")
    capsule = _digest(body["development_capsule_sha256"], label="development capsule")
    if capsule != state.development_capsule:
        raise CustodyLedgerError("policy lock capsule is invalid")
    policy = body["selected_policy_ms"]
    if isinstance(policy, bool) or policy not in POLICIES_MS:
        raise CustodyLedgerError("policy lock selection is invalid")
    state.development_head = development_head
    state.selected_policy = policy
    state.policy_lock = _digest(body["policy_lock_sha256"], label="policy lock")
    state.transition(after)


def _replay_holdout_release(
    state: _ReplayState,
    raw: object,
    *,
    before: str,
    after: str,
    attempt_id: str | None,
    previous_hash: str,
) -> None:
    body = _strict_mapping(
        raw,
        {
            "policy_lock_receipt_sha256",
            "selected_policy_ms",
            "policy_lock_sha256",
            "holdout_manifest_sha256",
            "release_nonce",
        },
        label="holdout release body",
    )
    if (
        before != "policy_selection_locked"
        or after != "holdout_collection"
        or attempt_id != state.active_attempt
        or state.holdout_manifest is not None
    ):
        raise CustodyLedgerError("holdout release phase is invalid")
    if (
        _digest(body["policy_lock_receipt_sha256"], label="policy lock receipt") != previous_hash
        or body["selected_policy_ms"] != state.selected_policy
        or _digest(body["policy_lock_sha256"], label="policy lock") != state.policy_lock
    ):
        raise CustodyLedgerError("holdout release policy binding is invalid")
    _safe_id(body["release_nonce"], label="holdout release nonce")
    state.policy_lock_receipt = previous_hash
    state.holdout_manifest = _digest(
        body["holdout_manifest_sha256"],
        label="holdout manifest",
    )
    state.transition(after)


def _replay_terminal_outcome(
    state: _ReplayState,
    raw: object,
    *,
    before: str,
    after: str,
    attempt_id: str | None,
) -> None:
    body = _strict_mapping(
        raw,
        {
            "outcome",
            "outage_enum",
            "holdout_capsule_sha256",
            "usage_evidence_sha256",
            "actual_provider_requests",
            "actual_cost_microusd",
        },
        label="terminal outcome body",
    )
    if attempt_id != state.active_attempt:
        raise CustodyLedgerError("terminal outcome attempt is invalid")
    requests = _bounded_int(
        body["actual_provider_requests"],
        label="actual provider requests",
        maximum=state.active_request_reservation,
        minimum=0,
    )
    cost = _bounded_int(
        body["actual_cost_microusd"],
        label="actual cost",
        maximum=state.active_cost_reservation,
        minimum=0,
    )
    if requests < state.development_requests or cost < state.development_cost:
        raise CustodyLedgerError("terminal outcome understates development usage")
    _digest(body["usage_evidence_sha256"], label="usage evidence")
    final_usage = _digest(body["usage_evidence_sha256"], label="usage evidence")
    outcome = body["outcome"]
    outage = body["outage_enum"]
    capsule = body["holdout_capsule_sha256"]
    if outcome == "completed":
        if before != "holdout_collection" or after != "completed" or outage is not None:
            raise CustodyLedgerError("completed outcome phase is invalid")
        state.holdout_capsule = _digest(capsule, label="holdout capsule")
        state.completed_attempt = attempt_id
        state.completed_authorization = state.active_authorization
        state.completed_claimed_at = state.active_claimed_at
        state.transition(after)
    elif outcome == "infrastructure_outage":
        if (
            before != "development_collection"
            or after != before
            or state.development_checkpoint_attempt is not None
            or outage not in OUTAGE_ENUMS
            or capsule is not None
        ):
            raise CustodyLedgerError("infrastructure outage outcome is invalid")
        state.last_outage_attempt = attempt_id
        state.last_outage_enum = outage
    elif outcome in {"failed", "invalidated"}:
        expected_after = "aborted" if outcome == "failed" else "invalidated"
        if after != expected_after or outage is not None or capsule is not None:
            raise CustodyLedgerError("terminal failure outcome is invalid")
        state.transition(after)
    else:
        raise CustodyLedgerError("terminal outcome is invalid")
    state.active_attempt = None
    state.active_attempt_index = None
    state.active_request_reservation = 0
    state.active_cost_reservation = 0
    state.active_authorization = None
    state.active_claimed_at = None
    state.final_usage = final_usage
    state.actual_requests = requests
    state.actual_cost = cost


def _strict_mapping(raw: object, fields: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise CustodyLedgerError(f"{label} fields are invalid")
    return dict(raw)


def _safe_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise CustodyLedgerError(f"{label} is invalid")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise CustodyLedgerError(f"{label} digest is invalid")
    return value


def _source_digest(value: object) -> str:
    if not isinstance(value, str) or not SOURCE_SHA.fullmatch(value):
        raise CustodyLedgerError("source digest is invalid")
    return value


def _phase(value: object) -> str:
    if value not in PHASES:
        raise CustodyLedgerError("ledger phase is invalid")
    assert isinstance(value, str)
    return value


def _bounded_int(
    value: object,
    *,
    label: str,
    maximum: int,
    minimum: int = 1,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CustodyLedgerError(f"{label} is invalid")
    return value


def _utc_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CustodyLedgerError("ledger receipt time is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CustodyLedgerError("ledger receipt time is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise CustodyLedgerError("ledger receipt time is invalid")
    return parsed
