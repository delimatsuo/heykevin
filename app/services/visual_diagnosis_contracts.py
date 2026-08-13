"""Provider-neutral, synthetic-only contracts for visual diagnosis A0-A2.

This module is deliberately pure.  It contains structural validation and
canonical hashing only; it does not know about storage, providers, actors,
delivery policy, safety policy, or customer data.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


SCHEMA_VERSION = "visual_diagnosis/v1"
EVIDENCE_SCOPE = "synthetic_offline_only"
ORDINARY_RECEIPT_CAP = 64
CONTROL_RECEIPT_CAP = 6
OPAQUE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
PAYLOAD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_FORBIDDEN_PAYLOAD_TERMS = (
    "address",
    "api_key",
    "credential",
    "diagnosis",
    "model",
    "part",
    "phone",
    "price",
    "prompt",
    "serial",
    "token",
    "transcript",
    "url",
)
_FORBIDDEN_VALUE_PATTERNS = tuple(
    re.compile(rf"(?:^|[^a-z0-9]){re.escape(term)}(?:$|[^a-z0-9])")
    for term in _FORBIDDEN_PAYLOAD_TERMS
)
_PROJECTION_REASON_CODES = {
    "ok",
    "case_terminal",
    "customer_action_pending",
    "deletion_pending",
    "media_required",
    "analysis_ready",
    "packet_ready",
    "verified_deleted",
}
_PROJECTION_NEXT_ACTIONS = {
    "resolve_customer_action",
    "upload_initial_symptom_media",
    "start_analysis",
    "record_packet_delivery",
}
_EVENT_PAYLOAD_KEYS = {
    "case_created": {"scenario", "source_ref"},
    "consent_requested": set(),
    "consent_granted": set(),
    "consent_declined": set(),
    "consent_withdrawn": set(),
    "media_action_issued": {
        "request_id", "action_kind", "budget_bucket", "copy_ref", "receipt_ref",
        "locale", "optionality", "safe_refusal_code", "decision_ref", "evidence_ref",
        "response_option_codes",
        "expires_at",
    },
    "diagnostic_question_issued": {
        "request_id", "action_kind", "budget_bucket", "copy_ref", "receipt_ref",
        "locale", "optionality", "safe_refusal_code", "decision_ref", "evidence_ref",
        "response_option_codes",
        "expires_at",
    },
    "customer_action_resolved": {
        "request_id", "status", "action_kind", "locale", "response_option_code",
    },
    "upload_started": {
        "asset_id", "media_type", "byte_size", "duration_ms", "width", "height", "digest",
    },
    "upload_finalized": {"asset_id"},
    "media_validated": {"asset_id", "validation"},
    "media_action_resolved": {
        "request_id", "action_kind", "media_role", "validation", "status", "asset_id",
    },
    "analysis_started": set(),
    "analysis_retry_recorded": {"attempt"},
    "analysis_completed": {"outcome"},
    "analysis_failed": {"failure_state"},
    "contractor_packet_ready_recorded": set(),
    "contractor_packet_delivery_started": set(),
    "contractor_packet_delivery_receipt_recorded": {"status"},
    "delivery_policy_receipt_recorded": {"receipt_ref"},
    "customer_delivery_started": {"receipt_ref"},
    "customer_delivery_receipt_recorded": {"status"},
    "case_closed": set(),
    "case_cancelled": set(),
    "case_expired": set(),
    "deletion_requested": set(),
    "deletion_retry_recorded": {"attempt"},
    "deletion_verified": set(),
}


def _canonical_json(value: Any) -> bytes:
    """Return the one canonical representation used by both digests."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _validate_structural_value(value: Any, *, depth: int = 0) -> None:
    if depth > 4:
        raise ValueError("payload nesting exceeds structural bound")
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return
    if isinstance(value, str):
        if len(value) > 256:
            raise ValueError("payload string exceeds structural bound")
        lowered = value.casefold()
        if any(term in lowered for term in _FORBIDDEN_PAYLOAD_TERMS):
            raise ValueError("payload value is outside the structural contract")
        return
    if isinstance(value, list):
        if len(value) > 16:
            raise ValueError("payload list exceeds structural bound")
        for item in value:
            _validate_structural_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 32:
            raise ValueError("payload map exceeds structural bound")
        for key, item in value.items():
            if not isinstance(key, str) or not PAYLOAD_KEY_PATTERN.fullmatch(key):
                raise ValueError("payload keys must be closed structural codes")
            lowered = key.casefold()
            if any(term in lowered for term in _FORBIDDEN_PAYLOAD_TERMS):
                raise ValueError("payload key is outside the structural contract")
            _validate_structural_value(item, depth=depth + 1)
        return
    raise ValueError("payload contains a non-structural value")


def _validate_code(value: str, *, field_name: str) -> str:
    if not CODE_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a closed lowercase code")
    return value


class _ClosedStrEnum(str, Enum):
    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(member.value for member in cls)


class CaseStatus(_ClosedStrEnum):
    CREATED = "created"
    ACTIVE = "active"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ConsentStatus(_ClosedStrEnum):
    NOT_REQUESTED = "not_requested"
    AWAITING_DECISION = "awaiting_decision"
    GRANTED = "granted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


class MediaStatus(_ClosedStrEnum):
    NONE = "none"
    AWAITING_REQUIRED = "awaiting_required"
    UPLOAD_PENDING = "upload_pending"
    UPLOADED_QUARANTINED = "uploaded_quarantined"
    VALIDATED = "validated"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class AnalysisStatus(_ClosedStrEnum):
    NOT_STARTED = "not_started"
    READY = "ready"
    PROCESSING = "processing"
    AWAITING_CUSTOMER_ACTION = "awaiting_customer_action"
    COMPLETE = "complete"
    ABSTAINED = "abstained"
    FAILED_RETRIABLE = "failed_retriable"
    FAILED_TERMINAL = "failed_terminal"


class PacketStatus(_ClosedStrEnum):
    NOT_READY = "not_ready"
    READY = "ready"
    DELIVERY_PENDING = "delivery_pending"
    DELIVERED = "delivered"
    DELIVERY_FAILED = "delivery_failed"


class DeliveryStatus(_ClosedStrEnum):
    NOT_EVALUATED = "not_evaluated"
    POLICY_RECEIPT_RECORDED = "policy_receipt_recorded"
    DELIVERY_PENDING = "delivery_pending"
    DELIVERED = "delivered"
    DELIVERY_FAILED = "delivery_failed"


class DeletionStatus(_ClosedStrEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "deletion_pending"
    VERIFIED = "verified_deleted"


class EventKind(_ClosedStrEnum):
    CASE_CREATED = "case_created"
    CONSENT_REQUESTED = "consent_requested"
    CONSENT_GRANTED = "consent_granted"
    CONSENT_DECLINED = "consent_declined"
    CONSENT_WITHDRAWN = "consent_withdrawn"
    MEDIA_ACTION_ISSUED = "media_action_issued"
    DIAGNOSTIC_QUESTION_ISSUED = "diagnostic_question_issued"
    CUSTOMER_ACTION_RESOLVED = "customer_action_resolved"
    UPLOAD_STARTED = "upload_started"
    UPLOAD_FINALIZED = "upload_finalized"
    MEDIA_VALIDATED = "media_validated"
    MEDIA_ACTION_RESOLVED = "media_action_resolved"
    ANALYSIS_STARTED = "analysis_started"
    ANALYSIS_RETRY_RECORDED = "analysis_retry_recorded"
    ANALYSIS_COMPLETED = "analysis_completed"
    ANALYSIS_FAILED = "analysis_failed"
    CONTRACTOR_PACKET_READY_RECORDED = "contractor_packet_ready_recorded"
    CONTRACTOR_PACKET_DELIVERY_STARTED = "contractor_packet_delivery_started"
    CONTRACTOR_PACKET_DELIVERY_RECEIPT_RECORDED = "contractor_packet_delivery_receipt_recorded"
    DELIVERY_POLICY_RECEIPT_RECORDED = "delivery_policy_receipt_recorded"
    CUSTOMER_DELIVERY_STARTED = "customer_delivery_started"
    CUSTOMER_DELIVERY_RECEIPT_RECORDED = "customer_delivery_receipt_recorded"
    CASE_CLOSED = "case_closed"
    CASE_CANCELLED = "case_cancelled"
    CASE_EXPIRED = "case_expired"
    DELETION_REQUESTED = "deletion_requested"
    DELETION_RETRY_RECORDED = "deletion_retry_recorded"
    DELETION_VERIFIED = "deletion_verified"


class EventSource(_ClosedStrEnum):
    SYNTHETIC = "synthetic_structural"
    CUSTOMER_ACTION = "customer_action"
    SYSTEM = "system"
    POLICY_RECEIPT = "policy_receipt"


class ActionKind(_ClosedStrEnum):
    DIAGNOSTIC_QUESTION = "diagnostic_question"
    RATING_PLATE = "rating_plate"
    TARGETED_RECAPTURE = "targeted_recapture"


class ActionStatus(_ClosedStrEnum):
    ISSUED = "issued"
    UPLOADING = "uploading"
    SUBMITTED = "submitted"
    ANSWERED = "answered"
    NOT_SURE = "not_sure"
    FULFILLED = "fulfilled"
    UNAVAILABLE = "unavailable"
    DECLINED = "declined"
    CANNOT_SAFELY_COMPLETE = "cannot_safely_complete"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class MediaRole(_ClosedStrEnum):
    SYMPTOM_VIDEO = "symptom_video"
    RATING_PLATE = "rating_plate"
    TARGETED_RECAPTURE = "targeted_recapture"


class MediaValidation(_ClosedStrEnum):
    PENDING = "pending"
    VALIDATED = "validated"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


class ArtifactReadiness(_ClosedStrEnum):
    NOT_READY = "not_ready"
    READY = "ready"
    COMPLETE = "complete"
    ABSTAINED = "abstained"


class ProjectionRole(_ClosedStrEnum):
    INTERNAL = "internal"
    CUSTOMER = "customer_safe_structural"
    CONTRACTOR = "contractor_safe_structural"
    AUDIT = "audit"


class StructuralValidationError(ValueError):
    """Stable, payload-free construction failure for structural contracts."""


class StructuralModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError:
            raise StructuralValidationError("invalid structural contract") from None

    @classmethod
    def model_validate(cls, *args: Any, **kwargs: Any) -> "StructuralModel":
        # Pydantic's model_validate()/model_validate_json()/
        # model_validate_strings() are separate entry points that do not
        # route through __init__ above -- each needs its own sanitization,
        # or a caller using any of them gets a raw pydantic ValidationError
        # whose message embeds the offending input value verbatim.
        try:
            return super().model_validate(*args, **kwargs)
        except ValidationError:
            raise StructuralValidationError("invalid structural contract") from None

    @classmethod
    def model_validate_json(cls, *args: Any, **kwargs: Any) -> "StructuralModel":
        try:
            return super().model_validate_json(*args, **kwargs)
        except ValidationError:
            raise StructuralValidationError("invalid structural contract") from None

    @classmethod
    def model_validate_strings(cls, *args: Any, **kwargs: Any) -> "StructuralModel":
        try:
            return super().model_validate_strings(*args, **kwargs)
        except ValidationError:
            raise StructuralValidationError("invalid structural contract") from None

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(structural)"

    def __str__(self) -> str:
        return self.__repr__()


class StateVector(StructuralModel):
    case: CaseStatus = CaseStatus.CREATED
    consent: ConsentStatus = ConsentStatus.NOT_REQUESTED
    media: MediaStatus = MediaStatus.NONE
    analysis: AnalysisStatus = AnalysisStatus.NOT_STARTED
    contractor_packet: PacketStatus = PacketStatus.NOT_READY
    customer_delivery: DeliveryStatus = DeliveryStatus.NOT_EVALUATED
    deletion: DeletionStatus = DeletionStatus.NOT_REQUESTED


class ArtifactReference(StructuralModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    readiness: ArtifactReadiness

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if not OPAQUE_REF_PATTERN.fullmatch(value):
            raise ValueError("artifact_id must be opaque")
        return value


class CallFacts(StructuralModel):
    """Bounded, normalized call facts without raw transcript content."""

    scenario: str = Field(min_length=1, max_length=128)
    symptom_code: str = Field(min_length=1, max_length=64)
    source_ref: str = Field(min_length=1, max_length=128)
    provenance_code: str = Field(min_length=1, max_length=64)
    unknown_fields: tuple[str, ...] = Field(default_factory=tuple, max_length=16)

    @field_validator("scenario", "symptom_code", "provenance_code")
    @classmethod
    def validate_fact_codes(cls, value: str) -> str:
        return _validate_code(value, field_name="call fact")

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, value: str) -> str:
        if not OPAQUE_REF_PATTERN.fullmatch(value):
            raise ValueError("source_ref must be opaque")
        return value

    @field_validator("unknown_fields")
    @classmethod
    def validate_unknown_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validate_code(value, field_name="unknown field")
        return values


class StructuralReceiptReference(StructuralModel):
    """Synthetic A0-A2 receipt reference; never an external receipt payload."""

    event_id: str = Field(min_length=1, max_length=128)
    semantic_envelope_fingerprint: str = Field(min_length=64, max_length=64)
    status_code: str = Field(min_length=1, max_length=64)
    resulting_revision: int = Field(ge=1)

    @field_validator("event_id")
    @classmethod
    def validate_reference_event_id(cls, value: str) -> str:
        if not OPAQUE_REF_PATTERN.fullmatch(value):
            raise ValueError("event_id must be opaque")
        return value

    @field_validator("semantic_envelope_fingerprint")
    @classmethod
    def validate_reference_fingerprint(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("fingerprint must be lowercase SHA-256 hex")
        return value

    @field_validator("status_code")
    @classmethod
    def validate_reference_status(cls, value: str) -> str:
        return _validate_code(value, field_name="status code")


class MediaAsset(StructuralModel):
    asset_id: str = Field(min_length=1, max_length=128)
    role: MediaRole
    media_type: str = Field(min_length=1, max_length=32)
    byte_size: int = Field(ge=0, le=50 * 1024 * 1024)
    duration_ms: int | None = Field(default=None, ge=0, le=120_000)
    width: int | None = Field(default=None, ge=1, le=20_000)
    height: int | None = Field(default=None, ge=1, le=20_000)
    digest: str = Field(min_length=64, max_length=64)
    validation: MediaValidation

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, value: str) -> str:
        if not OPAQUE_REF_PATTERN.fullmatch(value):
            raise ValueError("asset_id must be opaque")
        return value

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("digest must be lowercase SHA-256 hex")
        return value


class PendingCustomerAction(StructuralModel):
    request_id: str = Field(min_length=1, max_length=128)
    kind: ActionKind
    budget_bucket: str = Field(min_length=1, max_length=64)
    status: ActionStatus
    copy_ref: str | None = Field(default=None, max_length=128)
    response_option_codes: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    response_option_code: str | None = Field(default=None, max_length=64)
    locale: str = Field(default="und", min_length=2, max_length=35)
    optionality: str = Field(default="optional", min_length=1, max_length=32)
    safe_refusal_code: str | None = Field(default=None, max_length=64)
    decision_ref: str | None = Field(default=None, max_length=128)
    evidence_ref: str | None = Field(default=None, max_length=128)
    issued_at: datetime
    expires_at: datetime | None = None
    resolved_at: datetime | None = None

    @field_validator("issued_at", "expires_at", "resolved_at")
    @classmethod
    def validate_action_times(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("action timestamps must be timezone-aware")
        return value

    @field_validator("request_id", "copy_ref", "decision_ref", "evidence_ref")
    @classmethod
    def validate_opaque_refs(cls, value: str | None) -> str | None:
        if value is not None and not OPAQUE_REF_PATTERN.fullmatch(value):
            raise ValueError("reference must be opaque")
        return value

    @field_validator("budget_bucket", "optionality", "safe_refusal_code")
    @classmethod
    def validate_codes(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_code(value, field_name="code")
        return value

    @field_validator("response_option_codes")
    @classmethod
    def validate_response_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validate_code(value, field_name="response option")
        return values

    @field_validator("response_option_code")
    @classmethod
    def validate_response_option_code(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_code(value, field_name="response option")
        return value

    @field_validator("locale")
    @classmethod
    def validate_locale(cls, value: str) -> str:
        if value == "und" or re.fullmatch(
            r"[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?", value
        ):
            return value
        raise ValueError("locale must be a bounded BCP-47 structural tag")


class Receipt(StructuralModel):
    event_id: str = Field(min_length=1, max_length=128)
    kind: EventKind
    semantic_envelope_fingerprint: str = Field(min_length=64, max_length=64)
    historical_decision_code: str = Field(min_length=1, max_length=64)
    resulting_revision: int = Field(ge=1)

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        if not OPAQUE_REF_PATTERN.fullmatch(value):
            raise ValueError("event_id must be opaque")
        return value

    @field_validator("semantic_envelope_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("fingerprint must be lowercase SHA-256 hex")
        return value

    @field_validator("historical_decision_code")
    @classmethod
    def validate_decision_code(cls, value: str) -> str:
        return _validate_code(value, field_name="decision code")


class VisualTriageEvent(StructuralModel):
    schema_version: str = SCHEMA_VERSION
    case_id: str = Field(min_length=1, max_length=128)
    contractor_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    kind: EventKind
    payload: dict[str, Any]
    canonical_payload_digest: str = Field(min_length=64, max_length=64)
    semantic_envelope_fingerprint: str = Field(min_length=64, max_length=64)
    expected_revision: int = Field(ge=0, le=10_000)
    source_kind: EventSource
    event_time: datetime
    retry_stage: str | None = Field(default=None, max_length=64)
    retry_attempt: int | None = Field(default=None, ge=1, le=3)
    evidence_scope: str = EVIDENCE_SCOPE

    _envelope_fields: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "case_id",
        "contractor_id",
        "event_kind",
        "canonical_payload_digest",
        "expected_revision",
        "source_kind",
        "event_time",
        "retry_stage",
        "retry_attempt",
        "evidence_scope",
    )

    @field_validator("case_id", "contractor_id", "event_id")
    @classmethod
    def validate_identity_refs(cls, value: str) -> str:
        if not OPAQUE_REF_PATTERN.fullmatch(value):
            raise ValueError("identity must be opaque")
        return value

    @field_validator("retry_stage")
    @classmethod
    def validate_retry_stage(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_code(value, field_name="retry stage")
        return value

    @field_validator("event_time")
    @classmethod
    def validate_event_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("event_time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_hashes_and_payload(self) -> "VisualTriageEvent":
        self.assert_integrity()
        return self

    def assert_integrity(self) -> None:
        """Recheck the canonical envelope after callers may mutate nested input."""

        _validate_structural_value(self.payload)
        allowed_keys = _EVENT_PAYLOAD_KEYS[self.kind.value]
        if set(self.payload) - allowed_keys:
            raise ValueError("event payload keys are outside the closed contract")
        payload_digest = _sha256(self.payload)
        if payload_digest != self.canonical_payload_digest:
            raise ValueError("canonical payload digest does not match payload")
        envelope = {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "contractor_id": self.contractor_id,
            "event_kind": self.kind.value,
            "canonical_payload_digest": self.canonical_payload_digest,
            "expected_revision": self.expected_revision,
            "source_kind": self.source_kind.value,
            "event_time": self.event_time.isoformat(),
            "retry_stage": self.retry_stage,
            "retry_attempt": self.retry_attempt,
            "evidence_scope": self.evidence_scope,
        }
        if _sha256(envelope) != self.semantic_envelope_fingerprint:
            raise ValueError("semantic envelope fingerprint does not match envelope")
        if self.schema_version != SCHEMA_VERSION or self.evidence_scope != EVIDENCE_SCOPE:
            raise ValueError("event schema or evidence scope is not A0-A2 compatible")

    @classmethod
    def build(
        cls,
        *,
        case_id: str,
        contractor_id: str,
        event_id: str,
        kind: EventKind,
        payload: dict[str, Any],
        expected_revision: int,
        source_kind: EventSource,
        event_time: datetime,
        retry_stage: str | None = None,
        retry_attempt: int | None = None,
    ) -> "VisualTriageEvent":
        payload_digest = _sha256(payload)
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "case_id": case_id,
            "contractor_id": contractor_id,
            "event_kind": kind.value,
            "canonical_payload_digest": payload_digest,
            "expected_revision": expected_revision,
            "source_kind": source_kind.value,
            "event_time": event_time.isoformat(),
            "retry_stage": retry_stage,
            "retry_attempt": retry_attempt,
            "evidence_scope": EVIDENCE_SCOPE,
        }
        return cls(
            case_id=case_id,
            contractor_id=contractor_id,
            event_id=event_id,
            kind=kind,
            payload=payload,
            canonical_payload_digest=payload_digest,
            semantic_envelope_fingerprint=_sha256(envelope),
            expected_revision=expected_revision,
            source_kind=source_kind,
            event_time=event_time,
            retry_stage=retry_stage,
            retry_attempt=retry_attempt,
        )


class VisualTriageCase(StructuralModel):
    schema_version: str = SCHEMA_VERSION
    case_id: str = Field(min_length=1, max_length=128)
    contractor_id: str = Field(min_length=1, max_length=128)
    call_sid_ref: str | None = Field(default=None, max_length=128)
    supported_scenario: str = Field(default="hvac.not_cooling_with_outdoor_unit_noise", max_length=128)
    aggregate_revision: int = Field(ge=0)
    state_vector: StateVector
    pending_customer_action: PendingCustomerAction | None = None
    resolved_customer_actions: tuple[PendingCustomerAction, ...] = Field(default_factory=tuple, max_length=4)
    question_count: int = Field(ge=0, le=2)
    rating_plate_request_count: int = Field(ge=0, le=1)
    recapture_count: int = Field(ge=0, le=1)
    media_assets: tuple[MediaAsset, ...] = Field(default_factory=tuple, max_length=4)
    analysis_artifact: ArtifactReference | None = None
    candidate_set: ArtifactReference | None = None
    policy_versions: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    external_receipt_slots: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    ordinary_receipts: tuple[Receipt, ...] = Field(default_factory=tuple, max_length=ORDINARY_RECEIPT_CAP)
    control_receipts: tuple[Receipt, ...] = Field(default_factory=tuple, max_length=CONTROL_RECEIPT_CAP)
    deletion_retry_count: int = Field(ge=0, le=3)
    created_at: datetime | None = None
    expires_at: datetime | None = None
    completed_at: datetime | None = None
    deleted_at: datetime | None = None

    @field_validator("created_at", "expires_at", "completed_at", "deleted_at")
    @classmethod
    def validate_case_times(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("case timestamps must be timezone-aware")
        return value

    @field_validator("case_id", "contractor_id", "call_sid_ref")
    @classmethod
    def validate_case_refs(cls, value: str | None) -> str | None:
        if value is not None and not OPAQUE_REF_PATTERN.fullmatch(value):
            raise ValueError("case reference must be opaque")
        return value

    @field_validator("supported_scenario")
    @classmethod
    def validate_scenario(cls, value: str) -> str:
        if not CODE_PATTERN.fullmatch(value.replace(".", ":")):
            raise ValueError("scenario must be a closed code")
        return value

    @field_validator("external_receipt_slots")
    @classmethod
    def validate_external_receipt_slots(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values:
            raise ValueError("external receipt slots are unavailable in A0-A2")
        return values


class Projection(StructuralModel):
    role: ProjectionRole
    case_status: CaseStatus
    consent_status: ConsentStatus
    media_status: MediaStatus
    analysis_status: AnalysisStatus
    contractor_packet_status: PacketStatus
    customer_delivery_status: DeliveryStatus
    deletion_status: DeletionStatus
    aggregate_revision: int = Field(ge=0)
    historical_replay: bool = False
    historical_revision: int | None = Field(default=None, ge=0)
    historical_decision_code: str | None = Field(default=None, max_length=64)
    next_action: str | None = Field(default=None, max_length=64)
    reason_code: str = Field(default="ok", max_length=64)
    pending_action_kind: ActionKind | None = None
    pending_action_status: ActionStatus | None = None
    pending_action_request_ref: str | None = Field(default=None, max_length=128)
    pending_action_locale: str | None = Field(default=None, max_length=35)
    pending_action_copy_ref: str | None = Field(default=None, max_length=128)
    pending_action_budget_bucket: str | None = Field(default=None, max_length=64)
    pending_action_optionality: str | None = Field(default=None, max_length=32)
    pending_action_safe_refusal_code: str | None = Field(default=None, max_length=64)
    pending_action_response_option_codes: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    pending_action_expires_at: datetime | None = None

    @field_validator("historical_decision_code", "next_action", "reason_code")
    @classmethod
    def validate_projection_codes(cls, value: str | None) -> str | None:
        if value is not None:
            normalized = value.casefold()
            if any(pattern.search(normalized) for pattern in _FORBIDDEN_VALUE_PATTERNS):
                raise ValueError("projection code is outside the structural contract")
            return _validate_code(value, field_name="projection code")
        return value

    @model_validator(mode="after")
    def validate_deletion_projection(self) -> "Projection":
        if self.deletion_status in (DeletionStatus.PENDING, DeletionStatus.VERIFIED):
            if self.next_action is not None:
                raise ValueError("deletion projection cannot expose next action")
            if self.reason_code not in {"deletion_pending", "verified_deleted"}:
                raise ValueError("deletion projection must take precedence")
        if self.reason_code not in _PROJECTION_REASON_CODES:
            raise ValueError("projection reason is outside the closed contract")
        if self.next_action is not None and self.next_action not in _PROJECTION_NEXT_ACTIONS:
            raise ValueError("projection next action is outside the closed contract")
        return self


class Decision(StructuralModel):
    accepted: bool
    replayed: bool = False
    decision_code: str
    historical_decision_code: str | None = None
    historical_revision: int | None = Field(default=None, ge=0)
    current_revision: int = Field(ge=0)
    projection: Projection | None = None

    @field_validator("decision_code", "historical_decision_code")
    @classmethod
    def validate_decision_codes(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_code(value, field_name="decision code")
        return value
