"""Strict, offline-only contracts for Gemini caller-turn Gate 0B qualification."""

from __future__ import annotations

import audioop
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import re
import struct
from typing import Any, Mapping

from app.utils.audio import mulaw_to_pcm16k


CORPUS_SCHEMA_ID = "gate_0b_corpus_v1"
PRICING_SCHEMA_ID = "gate_0b_pricing_v1"
PRIMITIVE_SCHEMA_ID = "gate_0b_primitive_v1"
AUDIT_EVENT_SCHEMA_ID = "gate_0b_audit_event_v1"

EXACT_ACTIVITY_COUNT = 256
EXACT_HOLDOUT_ACTIVITY_COUNT = 128
EXACT_NO_SPEECH_WINDOW_COUNT = 64
EXACT_LANGUAGE_COUNT = 8
EXACT_ACTIVITIES_PER_LANGUAGE = 32
EXACT_ACTIVITIES_PER_LANGUAGE_SPLIT = 16
MAX_SESSIONS = 64
MAX_ACTIVITIES_PER_SESSION = 10
FIXED_LANGUAGES = {"en", "es", "pt", "fr", "zh", "hi", "ar"}
VALID_SPLITS = {"development", "holdout"}
PRIMARY_CONDITIONS = {
    "clean",
    "twilio_codec_only",
    "acoustic_impairment",
    "interaction_stress",
}
REQUIRED_STRESS_TAGS = {
    "jitter_packet_loss",
    "clipping",
    "echo_crosstalk",
    "far_field_low_volume",
    "background_noise",
    "long_pause",
    "fast_speech",
    "correction",
    "number_dictation",
    "synchronous_tool_use",
    "tool_cancellation_interruption",
    "fresh_connection_restart",
}
CODE_SWITCH_TAGS = {
    "code_switch_english_to_language",
    "code_switch_language_to_english",
}

EVIDENCE_FIELDS = (
    "attempt_authorization_validated",
    "authorization_consumed",
    "provider_execution_started",
    "attempt_completed",
    "assembly_sample_passed",
    "transcription_fidelity_sample_passed",
    "provider_interaction_integrity_sample_passed",
    "gate_0b_sample_passed",
    "future_execution_authorized",
    "enterprise_readiness_validated",
    "accessibility_validated",
    "broad_multilingual_support_validated",
    "semantic_extraction_validated",
    "caller_experience_neutrality_validated",
    "model_migration_authorized",
    "runtime_wiring_authorized",
    "controller_work_authorized",
    "staging_authorized",
    "deployment_authorized",
    "production_authorized",
    "release_authorized",
)

SAFE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
LANGUAGE_PATTERN = re.compile(r"[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-[A-Z]{2}|-[0-9]{3})?")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
FORBIDDEN_REPORT_KEYS = {
    "audio",
    "audio_path",
    "caller_id",
    "contractor_id",
    "credential",
    "exception",
    "exception_text",
    "file_path",
    "path",
    "phone",
    "prompt",
    "provider_request_id",
    "provider_session_id",
    "reference_text",
    "subject_id",
    "text",
    "tool_arguments",
    "transcript",
}


class QualificationContractError(ValueError):
    """Raised when a Gate 0B artifact violates its fail-closed contract."""


class CampaignPhase(str, Enum):
    PREREGISTERED = "preregistered"
    DEVELOPMENT_COLLECTION = "development_collection"
    POLICY_SELECTION_LOCKED = "policy_selection_locked"
    HOLDOUT_COLLECTION = "holdout_collection"
    COMPLETED = "completed"
    ABORTED = "aborted"
    INVALIDATED = "invalidated"


_PHASE_TRANSITIONS = {
    CampaignPhase.PREREGISTERED: {CampaignPhase.DEVELOPMENT_COLLECTION},
    CampaignPhase.DEVELOPMENT_COLLECTION: {
        CampaignPhase.POLICY_SELECTION_LOCKED,
        CampaignPhase.ABORTED,
        CampaignPhase.INVALIDATED,
    },
    CampaignPhase.POLICY_SELECTION_LOCKED: {
        CampaignPhase.HOLDOUT_COLLECTION,
        CampaignPhase.ABORTED,
        CampaignPhase.INVALIDATED,
    },
    CampaignPhase.HOLDOUT_COLLECTION: {
        CampaignPhase.COMPLETED,
        CampaignPhase.ABORTED,
        CampaignPhase.INVALIDATED,
    },
    CampaignPhase.COMPLETED: set(),
    CampaignPhase.ABORTED: set(),
    CampaignPhase.INVALIDATED: set(),
}


@dataclass(frozen=True, slots=True)
class CorpusSummary:
    schema_id: str
    collection_status: str
    activity_count: int
    holdout_activity_count: int
    no_speech_window_count: int
    subject_count: int
    session_count: int
    language_counts: dict[str, int]

    @property
    def execution_ready(self) -> bool:
        return self.collection_status == "ready"

    def redacted_report_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "collection_status": self.collection_status,
            "activity_count": self.activity_count,
            "holdout_activity_count": self.holdout_activity_count,
            "no_speech_window_count": self.no_speech_window_count,
            "subject_count": self.subject_count,
            "session_count": self.session_count,
            "language_counts": dict(sorted(self.language_counts.items())),
            "execution_ready": self.execution_ready,
        }


@dataclass(frozen=True, slots=True)
class PricingSchedule:
    schema_id: str
    model: str
    currency: str
    unit: str
    input_audio_usd: Decimal
    output_audio_usd: Decimal
    input_text_usd: Decimal
    output_text_usd: Decimal
    source_url: str
    retrieved_at: str
    artifact_sha256: str

    def cost_usd(
        self,
        *,
        input_audio_tokens: int = 0,
        output_audio_tokens: int = 0,
        input_text_tokens: int = 0,
        output_text_tokens: int = 0,
    ) -> Decimal:
        counts = (
            input_audio_tokens,
            output_audio_tokens,
            input_text_tokens,
            output_text_tokens,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
            raise QualificationContractError("token counts must be nonnegative integers")
        total = (
            self.input_audio_usd * input_audio_tokens
            + self.output_audio_usd * output_audio_tokens
            + self.input_text_usd * input_text_tokens
            + self.output_text_usd * output_text_tokens
        )
        return total / Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    schema_id: str
    activity_ordinal: int
    at_ms: int
    sequence: int
    epoch: int
    kind: str
    text: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AuditEvent":
        data = _strict_object(
            raw,
            allowed={
                "schema_id",
                "activity_ordinal",
                "at_ms",
                "sequence",
                "epoch",
                "kind",
                "text",
            },
            label="audit event",
        )
        if data.get("schema_id") != AUDIT_EVENT_SCHEMA_ID:
            raise QualificationContractError("unsupported audit event schema")
        text = data.get("text")
        if not isinstance(text, str) or len(text) > 16_000:
            raise QualificationContractError("audit event text is invalid")
        return cls(
            schema_id=AUDIT_EVENT_SCHEMA_ID,
            activity_ordinal=_bounded_int(data, "activity_ordinal", maximum=EXACT_ACTIVITY_COUNT - 1),
            at_ms=_bounded_int(data, "at_ms", maximum=7_200_000),
            sequence=_bounded_int(data, "sequence", maximum=1_000_000),
            epoch=_bounded_int(data, "epoch", maximum=1_000),
            kind=_safe_id(data.get("kind"), label="audit event kind"),
            text=text,
        )


@dataclass(frozen=True, slots=True)
class PrimitiveRecord:
    schema_id: str
    activity_ordinal: int
    split: str
    language: str
    assignment_status: str
    lifecycle_status: str
    fragment_count: int
    event_count: int
    reference_codepoints: int
    hypothesis_codepoints: int
    substitutions: int
    insertions: int
    deletions: int
    cer_micros: int
    wer_micros: int | None
    ambiguity_margin_micros: int
    critical_spans_exact: bool
    contamination_count: int
    duplicate_count: int
    late_fragment_mutation_count: int
    first_audio_ms: int | None
    interruption_tail_ms: int | None
    error_code: str | None
    commitment: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PrimitiveRecord":
        allowed = {
            "schema_id",
            "activity_ordinal",
            "split",
            "language",
            "assignment_status",
            "lifecycle_status",
            "fragment_count",
            "event_count",
            "reference_codepoints",
            "hypothesis_codepoints",
            "substitutions",
            "insertions",
            "deletions",
            "cer_micros",
            "wer_micros",
            "ambiguity_margin_micros",
            "critical_spans_exact",
            "contamination_count",
            "duplicate_count",
            "late_fragment_mutation_count",
            "first_audio_ms",
            "interruption_tail_ms",
            "error_code",
            "commitment",
        }
        data = _strict_object(raw, allowed=allowed, label="primitive record")
        if data.get("schema_id") != PRIMITIVE_SCHEMA_ID:
            raise QualificationContractError("unsupported primitive record schema")
        split = data.get("split")
        if split not in VALID_SPLITS:
            raise QualificationContractError("primitive split is invalid")
        language = _language(data.get("language"))
        assignment_status = _safe_id(data.get("assignment_status"), label="assignment status")
        lifecycle_status = _safe_id(data.get("lifecycle_status"), label="lifecycle status")
        critical = data.get("critical_spans_exact")
        if not isinstance(critical, bool):
            raise QualificationContractError("critical_spans_exact must be boolean")
        error_code = data.get("error_code")
        if error_code is not None:
            error_code = _safe_id(error_code, label="error code")
        return cls(
            schema_id=PRIMITIVE_SCHEMA_ID,
            activity_ordinal=_bounded_int(data, "activity_ordinal", maximum=EXACT_ACTIVITY_COUNT - 1),
            split=split,
            language=language,
            assignment_status=assignment_status,
            lifecycle_status=lifecycle_status,
            fragment_count=_bounded_int(data, "fragment_count", maximum=1_000),
            event_count=_bounded_int(data, "event_count", maximum=10_000),
            reference_codepoints=_bounded_int(data, "reference_codepoints", maximum=16_000),
            hypothesis_codepoints=_bounded_int(data, "hypothesis_codepoints", maximum=16_000),
            substitutions=_bounded_int(data, "substitutions", maximum=16_000),
            insertions=_bounded_int(data, "insertions", maximum=16_000),
            deletions=_bounded_int(data, "deletions", maximum=16_000),
            cer_micros=_bounded_int(data, "cer_micros", maximum=10_000_000),
            wer_micros=_optional_bounded_int(data.get("wer_micros"), maximum=10_000_000),
            ambiguity_margin_micros=_bounded_int(
                data,
                "ambiguity_margin_micros",
                maximum=1_000_000,
            ),
            critical_spans_exact=critical,
            contamination_count=_bounded_int(data, "contamination_count", maximum=1_000),
            duplicate_count=_bounded_int(data, "duplicate_count", maximum=1_000),
            late_fragment_mutation_count=_bounded_int(
                data,
                "late_fragment_mutation_count",
                maximum=1_000,
            ),
            first_audio_ms=_optional_bounded_int(data.get("first_audio_ms"), maximum=120_000),
            interruption_tail_ms=_optional_bounded_int(
                data.get("interruption_tail_ms"),
                maximum=120_000,
            ),
            error_code=error_code,
            commitment=_sha256(data.get("commitment"), label="primitive commitment"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }


def validate_phase_transition(current: CampaignPhase, target: CampaignPhase) -> None:
    if not isinstance(current, CampaignPhase) or not isinstance(target, CampaignPhase):
        raise QualificationContractError("phase transition requires campaign phases")
    if target not in _PHASE_TRANSITIONS[current]:
        raise QualificationContractError("invalid forward-only phase transition")


def empty_evidence_flags() -> dict[str, bool]:
    return {field: False for field in EVIDENCE_FIELDS}


def compute_twilio_roundtrip_sha256(pcm16k: bytes) -> str:
    """Return the deterministic PCM16 16k -> mulaw 8k -> PCM16 16k digest."""
    if not isinstance(pcm16k, bytes) or not pcm16k or len(pcm16k) % 2:
        raise QualificationContractError("PCM16 input must contain complete samples")
    pcm8k, _ = audioop.ratecv(pcm16k, 2, 1, 16_000, 8_000, None)
    mulaw8 = audioop.lin2ulaw(pcm8k, 2)
    return sha256(mulaw_to_pcm16k(mulaw8)).hexdigest()


def load_corpus_manifest(
    source: str | Path | Mapping[str, Any],
    *,
    require_ready: bool,
) -> CorpusSummary:
    raw, manifest_dir = _load_json_source(source, label="corpus manifest")
    allowed = {
        "schema_id",
        "collection_status",
        "corpus_root",
        "lower_resource_language",
        "attestations",
        "subjects",
        "sessions",
        "activities",
        "no_speech_windows",
    }
    data = _strict_object(raw, allowed=allowed, label="manifest")
    if data.get("schema_id") != CORPUS_SCHEMA_ID:
        raise QualificationContractError("unsupported corpus schema")
    status = data.get("collection_status")
    if status not in {"pending", "ready"}:
        raise QualificationContractError("collection_status is invalid")
    _validate_attestations(data.get("attestations"), ready=status == "ready")
    subjects = _object_list(data.get("subjects"), label="subjects")
    sessions = _object_list(data.get("sessions"), label="sessions")
    activities = _object_list(data.get("activities"), label="activities")
    windows = _object_list(data.get("no_speech_windows"), label="no_speech_windows")
    lower_resource_language = _language(data.get("lower_resource_language"))
    if lower_resource_language in FIXED_LANGUAGES:
        raise QualificationContractError("lower-resource language must be a distinct stratum")

    if status != "ready":
        if require_ready:
            raise QualificationContractError("corpus is not execution ready")
        if any((subjects, sessions, activities, windows)):
            raise QualificationContractError("pending corpus example must not contain assets")
        return CorpusSummary(
            schema_id=CORPUS_SCHEMA_ID,
            collection_status=status,
            activity_count=0,
            holdout_activity_count=0,
            no_speech_window_count=0,
            subject_count=0,
            session_count=0,
            language_counts={},
        )

    if len(activities) != EXACT_ACTIVITY_COUNT:
        raise QualificationContractError("ready corpus must contain exactly 256 activities")
    if len(windows) != EXACT_NO_SPEECH_WINDOW_COUNT:
        raise QualificationContractError("ready corpus must contain exactly 64 no-speech windows")
    if not 1 <= len(sessions) <= MAX_SESSIONS:
        raise QualificationContractError("ready corpus session count is invalid")

    corpus_root = _resolve_corpus_root(data.get("corpus_root"), manifest_dir)
    subject_index = _validate_subjects(subjects, lower_resource_language)
    session_index = _validate_sessions(sessions, subject_index)
    language_counts = _validate_activities(
        activities,
        subject_index=subject_index,
        session_index=session_index,
        corpus_root=corpus_root,
        lower_resource_language=lower_resource_language,
    )
    _validate_no_speech_windows(windows, corpus_root=corpus_root)
    holdout_count = sum(activity.get("split") == "holdout" for activity in activities)
    if holdout_count != EXACT_HOLDOUT_ACTIVITY_COUNT:
        raise QualificationContractError("ready corpus must contain exactly 128 holdout activities")
    return CorpusSummary(
        schema_id=CORPUS_SCHEMA_ID,
        collection_status=status,
        activity_count=len(activities),
        holdout_activity_count=holdout_count,
        no_speech_window_count=len(windows),
        subject_count=len(subjects),
        session_count=len(sessions),
        language_counts=dict(sorted(language_counts.items())),
    )


def load_pricing(source: str | Path | Mapping[str, Any]) -> PricingSchedule:
    if isinstance(source, Mapping):
        artifact_bytes = json.dumps(
            source,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    else:
        try:
            artifact_bytes = Path(source).read_bytes()
        except OSError as exc:
            raise QualificationContractError("pricing is unavailable or invalid") from exc
    raw, _ = _load_json_source(source, label="pricing")
    allowed = {
        "schema_id",
        "model",
        "currency",
        "unit",
        "input_audio_usd",
        "output_audio_usd",
        "input_text_usd",
        "output_text_usd",
        "source_url",
        "retrieved_at",
    }
    data = _strict_object(raw, allowed=allowed, label="pricing")
    if data.get("schema_id") != PRICING_SCHEMA_ID:
        raise QualificationContractError("unsupported pricing schema")
    model = data.get("model")
    if model != "gemini-3.1-flash-live-preview":
        raise QualificationContractError("pricing model is not the pinned candidate")
    if data.get("currency") != "USD" or data.get("unit") != "per_million_tokens":
        raise QualificationContractError("pricing units are invalid")
    source_url = data.get("source_url")
    if source_url != "https://ai.google.dev/gemini-api/docs/pricing":
        raise QualificationContractError("pricing source URL is invalid")
    retrieved_at = data.get("retrieved_at")
    if not isinstance(retrieved_at, str) or not retrieved_at.endswith("Z"):
        raise QualificationContractError("pricing retrieval timestamp is invalid")
    return PricingSchedule(
        schema_id=PRICING_SCHEMA_ID,
        model=model,
        currency="USD",
        unit="per_million_tokens",
        input_audio_usd=_decimal_rate(data.get("input_audio_usd"), "input_audio_usd"),
        output_audio_usd=_decimal_rate(data.get("output_audio_usd"), "output_audio_usd"),
        input_text_usd=_decimal_rate(data.get("input_text_usd"), "input_text_usd"),
        output_text_usd=_decimal_rate(data.get("output_text_usd"), "output_text_usd"),
        source_url=source_url,
        retrieved_at=retrieved_at,
        artifact_sha256=sha256(artifact_bytes).hexdigest(),
    )


def assert_payload_safe(value: object) -> None:
    """Reject published evidence containing payload-bearing field names."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise QualificationContractError("published evidence keys must be strings")
            if key.lower() in FORBIDDEN_REPORT_KEYS:
                raise QualificationContractError(f"forbidden payload field: {key}")
            assert_payload_safe(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_payload_safe(item)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise QualificationContractError("published evidence contains unsupported value")


def _validate_attestations(raw: object, *, ready: bool) -> None:
    allowed = {
        "consent_registry_sha256",
        "holdout_custodian_sha256",
        "paid_project_attestation_sha256",
        "retention_policy_sha256",
        "provider_retention_decision",
        "real_call_data_prohibited",
        "production_audio_prohibited",
        "voiceprint_extraction_prohibited",
    }
    data = _strict_object(raw, allowed=allowed, label="attestations")
    for field in (
        "consent_registry_sha256",
        "holdout_custodian_sha256",
        "paid_project_attestation_sha256",
        "retention_policy_sha256",
    ):
        _sha256(data.get(field), label=field)
    decision = data.get("provider_retention_decision")
    allowed_decisions = {"pending", "zdr_verified", "residual_retention_accepted"}
    if decision not in allowed_decisions or (ready and decision == "pending"):
        raise QualificationContractError("provider retention decision is invalid")
    for field in (
        "real_call_data_prohibited",
        "production_audio_prohibited",
        "voiceprint_extraction_prohibited",
    ):
        if data.get(field) is not True:
            raise QualificationContractError(f"{field} must be true")


def _validate_subjects(
    subjects: list[dict[str, Any]],
    lower_resource_language: str,
) -> dict[str, dict[str, Any]]:
    allowed = {
        "subject_id",
        "language",
        "split",
        "adult_attested",
        "consent_status",
        "consent_version",
        "consent_record_sha256",
        "rights",
    }
    expected_languages = FIXED_LANGUAGES | {lower_resource_language}
    index: dict[str, dict[str, Any]] = {}
    counts: Counter[tuple[str, str]] = Counter()
    for raw in subjects:
        data = _strict_object(raw, allowed=allowed, label="subject")
        subject_id = _safe_id(data.get("subject_id"), label="subject id")
        if subject_id in index:
            raise QualificationContractError("duplicate subject id")
        language = _language(data.get("language"))
        split = data.get("split")
        if language not in expected_languages or split not in VALID_SPLITS:
            raise QualificationContractError("subject language or split is invalid")
        if data.get("adult_attested") is not True or data.get("consent_status") != "active":
            raise QualificationContractError("subject consent is not active")
        if data.get("consent_version") != "gate0b-v1":
            raise QualificationContractError("subject consent version is invalid")
        _sha256(data.get("consent_record_sha256"), label="consent record")
        if data.get("rights") != "gate_0b_qualification_only":
            raise QualificationContractError("subject rights are invalid")
        normalized = dict(data)
        index[subject_id] = normalized
        counts[(language, split)] += 1
    for language in expected_languages:
        for split in VALID_SPLITS:
            if counts[(language, split)] < 2:
                raise QualificationContractError("each language split requires two subjects")
    return index


def _validate_sessions(
    sessions: list[dict[str, Any]],
    subjects: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    allowed = {"session_id", "language", "split", "subject_id", "activity_ids"}
    index: dict[str, dict[str, Any]] = {}
    seen_activities: set[str] = set()
    for raw in sessions:
        data = _strict_object(raw, allowed=allowed, label="session")
        session_id = _safe_id(data.get("session_id"), label="session id")
        if session_id in index:
            raise QualificationContractError("duplicate session id")
        language = _language(data.get("language"))
        split = data.get("split")
        subject_id = _safe_id(data.get("subject_id"), label="session subject id")
        subject = subjects.get(subject_id)
        if subject is None:
            raise QualificationContractError("session subject is missing")
        if subject["split"] != split or subject["language"] != language:
            raise QualificationContractError("session subject split or language mismatch")
        activity_ids_raw = data.get("activity_ids")
        if not isinstance(activity_ids_raw, list):
            raise QualificationContractError("session activity_ids must be an array")
        activity_ids = [_safe_id(value, label="activity id") for value in activity_ids_raw]
        if not 2 <= len(activity_ids) <= MAX_ACTIVITIES_PER_SESSION:
            raise QualificationContractError("session activity count is invalid")
        if len(activity_ids) != len(set(activity_ids)) or seen_activities.intersection(activity_ids):
            raise QualificationContractError("activity appears in multiple sessions")
        seen_activities.update(activity_ids)
        normalized = dict(data)
        normalized["activity_ids"] = tuple(activity_ids)
        index[session_id] = normalized
    return index


def _validate_activities(
    activities: list[dict[str, Any]],
    *,
    subject_index: dict[str, dict[str, Any]],
    session_index: dict[str, dict[str, Any]],
    corpus_root: Path,
    lower_resource_language: str,
) -> Counter[str]:
    allowed = {
        "activity_id",
        "session_id",
        "subject_id",
        "language",
        "split",
        "primary_condition",
        "scenario_tags",
        "audio_path",
        "audio_sha256",
        "twilio_roundtrip_sha256",
        "sample_rate_hz",
        "duration_ms",
        "speech_start_ms",
        "speech_end_ms",
        "script_sha256",
        "script_provenance",
    }
    expected_languages = FIXED_LANGUAGES | {lower_resource_language}
    language_counts: Counter[str] = Counter()
    split_counts: Counter[tuple[str, str]] = Counter()
    condition_counts: Counter[tuple[str, str, str]] = Counter()
    stress_counts: Counter[tuple[str, str]] = Counter()
    stress_languages: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    code_switch_counts: Counter[tuple[str, str, str]] = Counter()
    seen_ids: set[str] = set()
    seen_audio_digests: set[str] = set()
    referenced_activity_ids: set[str] = set()
    for session in session_index.values():
        referenced_activity_ids.update(session["activity_ids"])

    for raw in activities:
        data = _strict_object(raw, allowed=allowed, label="activity")
        activity_id = _safe_id(data.get("activity_id"), label="activity id")
        if activity_id in seen_ids:
            raise QualificationContractError("duplicate activity id")
        seen_ids.add(activity_id)
        session_id = _safe_id(data.get("session_id"), label="activity session id")
        subject_id = _safe_id(data.get("subject_id"), label="activity subject id")
        language = _language(data.get("language"))
        split = data.get("split")
        condition = data.get("primary_condition")
        if language not in expected_languages or split not in VALID_SPLITS:
            raise QualificationContractError("activity language or split is invalid")
        if condition not in PRIMARY_CONDITIONS:
            raise QualificationContractError("activity primary condition is invalid")
        subject = subject_index.get(subject_id)
        if subject is None:
            raise QualificationContractError("activity subject is missing")
        if subject["split"] != split:
            raise QualificationContractError("activity subject split mismatch")
        if subject["language"] != language:
            raise QualificationContractError("activity subject language mismatch")
        session = session_index.get(session_id)
        if session is None or activity_id not in session["activity_ids"]:
            raise QualificationContractError("activity session membership is invalid")
        if (
            session["split"] != split
            or session["language"] != language
            or session["subject_id"] != subject_id
        ):
            raise QualificationContractError("activity session metadata mismatch")
        tags_raw = data.get("scenario_tags")
        if not isinstance(tags_raw, list) or not tags_raw:
            raise QualificationContractError("activity scenario_tags must be non-empty")
        tags = {_safe_id(value, label="scenario tag") for value in tags_raw}
        if not tags.intersection(REQUIRED_STRESS_TAGS | CODE_SWITCH_TAGS):
            raise QualificationContractError("activity has no recognized scenario tag")
        for tag in tags & REQUIRED_STRESS_TAGS:
            stress_counts[(split, tag)] += 1
            stress_languages[(split, tag)].add(language)
        for tag in tags & CODE_SWITCH_TAGS:
            code_switch_counts[(language, split, tag)] += 1

        audio_digest = _sha256(data.get("audio_sha256"), label="activity audio")
        if audio_digest in seen_audio_digests:
            raise QualificationContractError("activity audio digests must be unique")
        seen_audio_digests.add(audio_digest)
        _validate_pcm_asset(
            data,
            corpus_root=corpus_root,
            id_label=activity_id,
            expect_speech=True,
        )
        _sha256(data.get("script_sha256"), label="script")
        if data.get("script_provenance") != "synthetic_v1":
            raise QualificationContractError("script provenance is invalid")
        language_counts[language] += 1
        split_counts[(language, split)] += 1
        condition_counts[(language, split, condition)] += 1

    if seen_ids != referenced_activity_ids:
        raise QualificationContractError("session activity references do not match activities")
    if set(language_counts) != expected_languages or len(language_counts) != EXACT_LANGUAGE_COUNT:
        raise QualificationContractError("ready corpus must contain exactly eight language strata")
    for language in expected_languages:
        if language_counts[language] != EXACT_ACTIVITIES_PER_LANGUAGE:
            raise QualificationContractError("each language must contain exactly 32 activities")
        for split in VALID_SPLITS:
            if split_counts[(language, split)] != EXACT_ACTIVITIES_PER_LANGUAGE_SPLIT:
                raise QualificationContractError("each language split must contain exactly 16 activities")
            for condition in PRIMARY_CONDITIONS:
                if condition_counts[(language, split, condition)] != 4:
                    raise QualificationContractError(
                        "each language split must contain four activities per primary condition"
                    )
            if language != "en":
                for tag in CODE_SWITCH_TAGS:
                    if code_switch_counts[(language, split, tag)] < 1:
                        raise QualificationContractError("code-switch direction coverage is incomplete")
    for split in VALID_SPLITS:
        for tag in REQUIRED_STRESS_TAGS:
            if stress_counts[(split, tag)] < 8 or len(stress_languages[(split, tag)]) < 4:
                raise QualificationContractError("stress coverage is incomplete")
    return language_counts


def _validate_no_speech_windows(windows: list[dict[str, Any]], *, corpus_root: Path) -> None:
    allowed = {
        "window_id",
        "split",
        "condition",
        "audio_path",
        "audio_sha256",
        "twilio_roundtrip_sha256",
        "sample_rate_hz",
        "duration_ms",
    }
    split_counts: Counter[str] = Counter()
    condition_counts: Counter[tuple[str, str]] = Counter()
    seen_ids: set[str] = set()
    for raw in windows:
        data = _strict_object(raw, allowed=allowed, label="no-speech window")
        window_id = _safe_id(data.get("window_id"), label="window id")
        if window_id in seen_ids:
            raise QualificationContractError("duplicate no-speech window id")
        seen_ids.add(window_id)
        split = data.get("split")
        if split not in VALID_SPLITS:
            raise QualificationContractError("no-speech split is invalid")
        if data.get("condition") not in {"silence", "background_noise"}:
            raise QualificationContractError("no-speech condition is invalid")
        _validate_pcm_asset(
            data,
            corpus_root=corpus_root,
            id_label=window_id,
            expect_speech=False,
            no_speech_condition=data["condition"],
        )
        split_counts[split] += 1
        condition_counts[(split, data["condition"])] += 1
    if split_counts != Counter({"development": 32, "holdout": 32}):
        raise QualificationContractError("no-speech windows must split 32/32")
    if any(
        condition_counts[(split, condition)] != 16
        for split in VALID_SPLITS
        for condition in ("silence", "background_noise")
    ):
        raise QualificationContractError(
            "each split must contain 16 silence and 16 background-noise windows"
        )


def _validate_pcm_asset(
    data: Mapping[str, Any],
    *,
    corpus_root: Path,
    id_label: str,
    expect_speech: bool,
    no_speech_condition: str | None = None,
) -> None:
    relative = data.get("audio_path")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise QualificationContractError(f"{id_label} audio path must be relative")
    candidate = corpus_root / relative
    if candidate.is_symlink():
        raise QualificationContractError(f"{id_label} audio asset must not be a symlink")
    path = candidate.resolve()
    try:
        path.relative_to(corpus_root)
    except ValueError as exc:
        raise QualificationContractError(f"{id_label} audio path escapes corpus root") from exc
    if not path.is_file():
        raise QualificationContractError(f"{id_label} audio asset is unavailable")
    payload = path.read_bytes()
    if not payload or len(payload) % 2:
        raise QualificationContractError(f"{id_label} PCM16 byte length is invalid")
    if sha256(payload).hexdigest() != data.get("audio_sha256"):
        raise QualificationContractError(f"{id_label} audio digest mismatch")
    if data.get("sample_rate_hz") != 16_000:
        raise QualificationContractError(f"{id_label} sample rate must be 16000")
    duration_ms = data.get("duration_ms")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or not 20 <= duration_ms <= 30_000:
        raise QualificationContractError(f"{id_label} duration is invalid")
    expected_bytes = duration_ms * 16_000 * 2 // 1_000
    if len(payload) != expected_bytes:
        raise QualificationContractError(f"{id_label} duration does not match PCM length")
    if compute_twilio_roundtrip_sha256(payload) != data.get("twilio_roundtrip_sha256"):
        raise QualificationContractError(f"{id_label} roundtrip digest mismatch")
    samples = struct.unpack(f"<{len(payload) // 2}h", payload)
    silence_ratio = sum(abs(value) <= 8 for value in samples) / len(samples)
    clipping_ratio = sum(abs(value) >= 32_760 for value in samples) / len(samples)
    peak = max(abs(value) for value in samples)
    if expect_speech:
        start = data.get("speech_start_ms")
        end = data.get("speech_end_ms")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or end > duration_ms
        ):
            raise QualificationContractError(f"{id_label} speech boundaries are invalid")
        if silence_ratio >= 0.98 or peak < 100 or clipping_ratio > 0.01:
            raise QualificationContractError(f"{id_label} speech signal bounds are invalid")
    elif no_speech_condition == "silence":
        if silence_ratio < 0.95 or peak > 64 or clipping_ratio > 0.01:
            raise QualificationContractError(f"{id_label} silence signal bounds are invalid")
    elif no_speech_condition == "background_noise":
        nontrivial_ratio = sum(abs(value) >= 64 for value in samples) / len(samples)
        mean_square = sum(value * value for value in samples) // len(samples)
        if (
            nontrivial_ratio < 0.80
            or not 4_096 <= mean_square <= 64_000_000
            or not 64 <= peak <= 8_000
            or clipping_ratio > 0.01
        ):
            raise QualificationContractError(
                f"{id_label} background-noise signal bounds are invalid"
            )
    else:
        raise QualificationContractError(f"{id_label} no-speech condition is invalid")


def _resolve_corpus_root(value: object, manifest_dir: Path | None) -> Path:
    if not isinstance(value, str) or not value:
        raise QualificationContractError("corpus_root is required")
    path = Path(value)
    if not path.is_absolute():
        if manifest_dir is None:
            raise QualificationContractError("relative corpus_root requires a manifest path")
        path = manifest_dir / path
    resolved = path.resolve()
    if not resolved.is_dir():
        raise QualificationContractError("corpus_root must be an existing directory")
    return resolved


def _load_json_source(
    source: str | Path | Mapping[str, Any],
    *,
    label: str,
) -> tuple[Mapping[str, Any], Path | None]:
    if isinstance(source, Mapping):
        return source, None
    path = Path(source)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationContractError(f"{label} is unavailable or invalid") from exc
    return raw, path.resolve().parent


def _strict_object(
    raw: object,
    *,
    allowed: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise QualificationContractError(f"{label} must be an object")
    unknown = set(raw) - allowed
    if unknown:
        raise QualificationContractError(f"unknown {label} field")
    missing = allowed - set(raw)
    if missing:
        raise QualificationContractError(f"missing {label} field")
    return dict(raw)


def _object_list(raw: object, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise QualificationContractError(f"{label} must be an array of objects")
    return [dict(item) for item in raw]


def _safe_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_PATTERN.fullmatch(value):
        raise QualificationContractError(f"{label} must be a safe identifier")
    return value


def _language(value: object) -> str:
    if not isinstance(value, str) or not LANGUAGE_PATTERN.fullmatch(value):
        raise QualificationContractError("language tag is invalid")
    return value


def _sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise QualificationContractError(f"{label} must be SHA-256")
    return value


def _bounded_int(data: Mapping[str, Any], field: str, *, maximum: int) -> int:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise QualificationContractError(f"{field} is outside its bound")
    return value


def _optional_bounded_int(value: object, *, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise QualificationContractError("optional integer is outside its bound")
    return value


def _decimal_rate(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise QualificationContractError(f"{field} must be a decimal string")
    try:
        rate = Decimal(value)
    except InvalidOperation as exc:
        raise QualificationContractError(f"{field} is invalid") from exc
    if not rate.is_finite() or rate < 0:
        raise QualificationContractError(f"{field} is invalid")
    return rate
