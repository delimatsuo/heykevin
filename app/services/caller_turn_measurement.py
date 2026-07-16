"""Independent, payload-safe measurement primitives for Gate 0B evidence."""

from __future__ import annotations

import base64
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.services.caller_turn_alignment import (
    ActivityReference,
    AlignmentPolicy,
    AlignmentStatus,
    CriticalSpan,
    CriticalSpanKind,
    FragmentMode,
    align_caller_turn_events,
    align_fragments,
    normalize_text,
    reconstruct_fragments,
)
from app.services.qualification_environment import (
    execution_identity_report_sha256,
    validate_execution_identity_report,
)
from app.services.caller_turns import (
    CallerTurnAssembler,
    CallerTurnEvent,
    CallerTurnEventKind,
    RetrospectiveCallerTurn,
)
from app.services.qualification_identity import canonical_json_bytes


ACTIVITY_PRIMITIVE_SCHEMA_ID = "gate_0b_activity_primitive_v2"
NO_SPEECH_PRIMITIVE_SCHEMA_ID = "gate_0b_no_speech_primitive_v1"
AUDIT_CAPSULE_SCHEMA_ID = "gate_0b_audit_capsule_v7"
CAPSULE_ACCOUNTING_SCHEMA_ID = "gate_0b_capsule_accounting_v1"
SEALED_CAPSULE_SCHEMA_ID = "gate_0b_sealed_capsule_v1"
SIGNED_ROOT_SCHEMA_ID = "gate_0b_signed_record_root_v1"
POLICIES_MS = frozenset({100, 250, 500, 750})
VALID_SPLITS = frozenset({"development", "holdout"})
VALID_LANGUAGES = frozenset({"ar", "en", "es", "fr", "hi", "ht", "pt", "zh"})
VALID_LIFECYCLE_STATUSES = frozenset(
    {
        "retrospective_complete",
        "partial",
        "cancelled",
        "dropped",
        "missing",
        "duplicate",
    }
)
VALID_ASSIGNMENT_STATUSES = frozenset({"matched", "ambiguous", "unassigned"})
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SHA256 = re.compile(r"[0-9a-f]{64}")
PROVIDER_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MAX_COUNTER = 100_000
MAX_TEXT_LENGTH = 16_000
MAX_CAPSULE_BYTES = 16 * 1024 * 1024
MAX_ACCOUNTING_COUNTER = 100_000_000
MAX_ACCOUNTING_ELAPSED_MS = 120_000
MAX_ACCOUNTING_OUTPUT_BYTES = 120 * 24_000 * 2
RECORD_HMAC_DOMAIN = b"gate-0b-record-v1\x00"
CAPSULE_KDF_INFO = b"gate-0b-audit-capsule-v1"
FORBIDDEN_CAPSULE_KEYS = frozenset(
    {
        "audio",
        "audio_path",
        "caller_id",
        "contractor_id",
        "credential",
        "exception",
        "file_path",
        "path",
        "phone",
        "prompt",
        "provider_request_id",
        "provider_session_id",
        "subject_id",
        "tool_arguments",
    }
)
WIRE_FACT_KINDS = frozenset(
    {
        "abnormal_close",
        "audio_after_terminal",
        "audio_received",
        "caller_activity_end",
        "caller_speech_end",
        "caller_activity_start",
        "caller_audio_sent",
        "connection_open",
        "false_activity",
        "goaway",
        "interrupted",
        "malformed",
        "observation_complete",
        "response_open",
        "response_terminal",
        "stream_closed",
        "teardown_complete",
        "teardown_failed",
        "tool_call_cancelled",
        "tool_call_open",
        "usage_received",
    }
)
CAPSULE_FAILURE_CODES = frozenset(
    {
        "audio_after_terminal",
        "connector_failure",
        "cost_reservation_exhausted",
        "expected_interaction_missing",
        "expected_tool_call_missing",
        "interaction_message_missing",
        "malformed_tool_cancellation",
        "malformed_message",
        "message_too_large",
        "observation_window_incomplete",
        "premature_current_response",
        "premature_usage_metadata",
        "provider_closed",
        "provider_goaway",
        "provider_request_reservation_exhausted",
        "reducer_disagreement",
        "response_crossed_restart",
        "response_gap_exceeded",
        "response_terminal_missing",
        "run_output_audio_cap_exceeded",
        "runaway_output",
        "session_cost_cap_exceeded",
        "session_timeout",
        "setup_rejected",
        "teardown_failure",
        "tool_cancellation_mismatch",
        "overlapping_tool_call",
        "unattributed_response",
        "unexpected_interaction_audio",
        "usage_metadata_inconsistent",
        "usage_metadata_missing",
    }
)


class MeasurementError(ValueError):
    """Raised when measurement evidence violates its fixed contract."""


@dataclass(frozen=True, slots=True)
class CriticalSpanFact:
    kind: str
    exact: bool

    def __post_init__(self) -> None:
        try:
            CriticalSpanKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise MeasurementError("critical span fact kind is invalid") from exc
        if not isinstance(self.exact, bool):
            raise MeasurementError("critical span fact exactness must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "exact": self.exact}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CriticalSpanFact":
        data = _strict_mapping(raw, {"kind", "exact"}, label="critical span fact")
        return cls(kind=data["kind"], exact=data["exact"])


@dataclass(frozen=True, slots=True)
class WireObservation:
    timing_covered: bool = False
    first_audio_ms: int | None = None
    interruption_tail_ms: int | None = None
    premature_current_audio_count: int = 0
    audio_after_terminal_count: int = 0
    response_gap_violation_count: int = 0
    abnormal_close_count: int = 0
    runaway_output_count: int = 0
    response_timeout_count: int = 0
    malformed_count: int = 0
    teardown_violation_count: int = 0
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.timing_covered, bool):
            raise MeasurementError("timing coverage must be boolean")
        _optional_bounded_int(self.first_audio_ms, label="first audio", maximum=120_000)
        _optional_bounded_int(
            self.interruption_tail_ms,
            label="interruption tail",
            maximum=120_000,
        )
        for label, value in (
            ("premature current audio", self.premature_current_audio_count),
            ("audio after terminal", self.audio_after_terminal_count),
            ("response gap", self.response_gap_violation_count),
            ("abnormal close", self.abnormal_close_count),
            ("runaway output", self.runaway_output_count),
            ("response timeout", self.response_timeout_count),
            ("malformed", self.malformed_count),
            ("teardown violation", self.teardown_violation_count),
        ):
            _bounded_int(value, label=label)
        _optional_safe_id(self.error_code, label="wire error code")
        if self.timing_covered != (self.first_audio_ms is not None):
            raise MeasurementError("timing coverage and first audio must agree")


@dataclass(frozen=True, slots=True)
class ActivityMeasurementInput:
    policy_ms: int
    activity_ordinal: int
    split: str
    language: str
    condition: str
    scenario_tags: tuple[str, ...]
    references: tuple[ActivityReference, ...]
    events: tuple[CallerTurnEvent, ...]
    expected_epoch: int
    expected_lifecycle_status: str
    advance_to_ms: int
    wire: WireObservation

    def __post_init__(self) -> None:
        _validate_policy(self.policy_ms)
        _bounded_int(self.activity_ordinal, label="activity ordinal", maximum=255)
        _validate_split(self.split)
        _validate_language(self.language)
        _safe_id(self.condition, label="condition")
        _safe_id_tuple(self.scenario_tags, label="scenario tags", maximum=16)
        if (
            not isinstance(self.references, tuple)
            or not self.references
            or any(not isinstance(value, ActivityReference) for value in self.references)
        ):
            raise MeasurementError("measurement references are invalid")
        if len({value.activity_ordinal for value in self.references}) != len(self.references):
            raise MeasurementError("measurement reference ordinals must be unique")
        if self.activity_ordinal not in {
            value.activity_ordinal for value in self.references
        }:
            raise MeasurementError("expected activity reference is missing")
        expected_reference = next(
            value
            for value in self.references
            if value.activity_ordinal == self.activity_ordinal
        )
        if expected_reference.language != self.language:
            raise MeasurementError("measurement language and reference must agree")
        if (
            not isinstance(self.events, tuple)
            or any(not isinstance(value, CallerTurnEvent) for value in self.events)
        ):
            raise MeasurementError("measurement events are invalid")
        _bounded_int(self.expected_epoch, label="expected epoch", maximum=1_000)
        if self.expected_lifecycle_status not in VALID_LIFECYCLE_STATUSES:
            raise MeasurementError("expected lifecycle status is invalid")
        _bounded_int(self.advance_to_ms, label="advance time", maximum=7_200_000)
        if self.events and self.advance_to_ms < self.events[-1].at_ms:
            raise MeasurementError("advance time precedes the final event")
        if not isinstance(self.wire, WireObservation):
            raise MeasurementError("wire observation is invalid")


@dataclass(frozen=True, slots=True)
class _ActivityAssemblyObservation:
    turns: tuple[RetrospectiveCallerTurn, ...]
    cross_activity_merge_count: int
    late_fragment_mutation_count: int
    stale_count: int


@dataclass(frozen=True, slots=True)
class ActivityPrimitiveRecord:
    schema_id: str
    policy_ms: int
    activity_ordinal: int
    split: str
    language: str
    condition: str
    scenario_tags: tuple[str, ...]
    assignment_status: str
    expected_lifecycle_status: str
    observed_lifecycle_status: str
    assembled_turn_count: int
    fragment_count: int
    event_count: int
    reference_characters: int
    hypothesis_characters: int
    substitutions: int
    insertions: int
    deletions: int
    reference_words: int | None
    hypothesis_words: int | None
    word_substitutions: int | None
    word_insertions: int | None
    word_deletions: int | None
    ambiguity_margin_micros: int
    critical_spans: tuple[CriticalSpanFact, ...]
    contamination_count: int
    duplicate_count: int
    cross_epoch_acceptance_count: int
    late_fragment_mutation_count: int
    stale_count: int
    timing_covered: bool
    first_audio_ms: int | None
    interruption_tail_ms: int | None
    premature_current_audio_count: int
    audio_after_terminal_count: int
    response_gap_violation_count: int
    abnormal_close_count: int
    runaway_output_count: int
    response_timeout_count: int
    malformed_count: int
    teardown_violation_count: int
    error_code: str | None
    commitment: str

    def __post_init__(self) -> None:
        if self.schema_id != ACTIVITY_PRIMITIVE_SCHEMA_ID:
            raise MeasurementError("activity primitive schema is invalid")
        _validate_policy(self.policy_ms)
        _bounded_int(self.activity_ordinal, label="activity ordinal", maximum=255)
        _validate_split(self.split)
        _validate_language(self.language)
        _safe_id(self.condition, label="condition")
        _safe_id_tuple(self.scenario_tags, label="scenario tags", maximum=16)
        if self.assignment_status not in VALID_ASSIGNMENT_STATUSES:
            raise MeasurementError("assignment status is invalid")
        if self.expected_lifecycle_status not in VALID_LIFECYCLE_STATUSES:
            raise MeasurementError("expected lifecycle status is invalid")
        if self.observed_lifecycle_status not in VALID_LIFECYCLE_STATUSES:
            raise MeasurementError("observed lifecycle status is invalid")
        for label, value in (
            ("assembled turn", self.assembled_turn_count),
            ("fragment", self.fragment_count),
            ("event", self.event_count),
            ("reference character", self.reference_characters),
            ("hypothesis character", self.hypothesis_characters),
            ("substitution", self.substitutions),
            ("insertion", self.insertions),
            ("deletion", self.deletions),
            ("ambiguity margin", self.ambiguity_margin_micros),
            ("contamination", self.contamination_count),
            ("duplicate", self.duplicate_count),
            ("cross epoch acceptance", self.cross_epoch_acceptance_count),
            ("late fragment mutation", self.late_fragment_mutation_count),
            ("stale", self.stale_count),
            ("premature current audio", self.premature_current_audio_count),
            ("audio after terminal", self.audio_after_terminal_count),
            ("response gap", self.response_gap_violation_count),
            ("abnormal close", self.abnormal_close_count),
            ("runaway output", self.runaway_output_count),
            ("response timeout", self.response_timeout_count),
            ("malformed", self.malformed_count),
            ("teardown violation", self.teardown_violation_count),
        ):
            _bounded_int(
                value,
                label=label,
                maximum=1_000_000 if label == "ambiguity margin" else MAX_COUNTER,
            )
        if self.reference_characters < 1:
            raise MeasurementError("reference character count must be positive")
        if self.substitutions + self.insertions + self.deletions > (
            self.reference_characters + self.hypothesis_characters
        ):
            raise MeasurementError("character edit counts are contradictory")
        if (
            self.substitutions + self.deletions > self.reference_characters
            or self.substitutions + self.insertions > self.hypothesis_characters
        ):
            raise MeasurementError("character edit counts exceed sequence lengths")
        word_fields = (
            self.reference_words,
            self.hypothesis_words,
            self.word_substitutions,
            self.word_insertions,
            self.word_deletions,
        )
        if any(value is None for value in word_fields):
            if any(value is not None for value in word_fields):
                raise MeasurementError("word measurement fields must be jointly present")
            if self.language != "zh":
                raise MeasurementError("segmented language requires word measurements")
        else:
            if self.language == "zh":
                raise MeasurementError("Chinese records cannot invent word measurements")
            for value in word_fields:
                _bounded_int(value, label="word measurement")
            if self.reference_words == 0:
                raise MeasurementError("reference word count must be positive")
            if self.word_substitutions + self.word_insertions + self.word_deletions > (
                self.reference_words + self.hypothesis_words
            ):
                raise MeasurementError("word edit counts are contradictory")
            if (
                self.word_substitutions + self.word_deletions > self.reference_words
                or self.word_substitutions + self.word_insertions > self.hypothesis_words
            ):
                raise MeasurementError("word edit counts exceed sequence lengths")
        if (
            not isinstance(self.critical_spans, tuple)
            or len(self.critical_spans) > 16
            or any(not isinstance(value, CriticalSpanFact) for value in self.critical_spans)
        ):
            raise MeasurementError("critical span facts are invalid")
        if len({value.kind for value in self.critical_spans}) != len(self.critical_spans):
            raise MeasurementError("critical span fact kinds must be unique")
        if self.assignment_status == "matched" and self.ambiguity_margin_micros < 50_000:
            raise MeasurementError("matched assignment lacks the ambiguity margin")
        if self.assignment_status == "ambiguous" and self.ambiguity_margin_micros >= 50_000:
            raise MeasurementError("ambiguous assignment contradicts its margin")
        if self.assembled_turn_count == 0 and self.observed_lifecycle_status != "missing":
            raise MeasurementError("missing assembly lifecycle is contradictory")
        if self.assembled_turn_count == 1 and self.observed_lifecycle_status in {
            "missing",
            "duplicate",
        }:
            raise MeasurementError("single assembly lifecycle is contradictory")
        if self.assembled_turn_count > 1 and self.observed_lifecycle_status != "duplicate":
            raise MeasurementError("duplicate assembly lifecycle is contradictory")
        if self.duplicate_count != max(0, self.assembled_turn_count - 1):
            raise MeasurementError("duplicate count contradicts assembled turns")
        if not isinstance(self.timing_covered, bool):
            raise MeasurementError("timing coverage must be boolean")
        _optional_bounded_int(self.first_audio_ms, label="first audio", maximum=120_000)
        _optional_bounded_int(
            self.interruption_tail_ms,
            label="interruption tail",
            maximum=120_000,
        )
        if self.timing_covered != (self.first_audio_ms is not None):
            raise MeasurementError("timing coverage and first audio must agree")
        _optional_safe_id(self.error_code, label="error code")
        if self.commitment and not SHA256.fullmatch(self.commitment):
            raise MeasurementError("record commitment must be SHA-256")

    def unsigned_dict(self) -> dict[str, Any]:
        value = self.to_dict()
        value.pop("commitment")
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "policy_ms": self.policy_ms,
            "activity_ordinal": self.activity_ordinal,
            "split": self.split,
            "language": self.language,
            "condition": self.condition,
            "scenario_tags": list(self.scenario_tags),
            "assignment_status": self.assignment_status,
            "expected_lifecycle_status": self.expected_lifecycle_status,
            "observed_lifecycle_status": self.observed_lifecycle_status,
            "assembled_turn_count": self.assembled_turn_count,
            "fragment_count": self.fragment_count,
            "event_count": self.event_count,
            "reference_characters": self.reference_characters,
            "hypothesis_characters": self.hypothesis_characters,
            "substitutions": self.substitutions,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "reference_words": self.reference_words,
            "hypothesis_words": self.hypothesis_words,
            "word_substitutions": self.word_substitutions,
            "word_insertions": self.word_insertions,
            "word_deletions": self.word_deletions,
            "ambiguity_margin_micros": self.ambiguity_margin_micros,
            "critical_spans": [value.to_dict() for value in self.critical_spans],
            "contamination_count": self.contamination_count,
            "duplicate_count": self.duplicate_count,
            "cross_epoch_acceptance_count": self.cross_epoch_acceptance_count,
            "late_fragment_mutation_count": self.late_fragment_mutation_count,
            "stale_count": self.stale_count,
            "timing_covered": self.timing_covered,
            "first_audio_ms": self.first_audio_ms,
            "interruption_tail_ms": self.interruption_tail_ms,
            "premature_current_audio_count": self.premature_current_audio_count,
            "audio_after_terminal_count": self.audio_after_terminal_count,
            "response_gap_violation_count": self.response_gap_violation_count,
            "abnormal_close_count": self.abnormal_close_count,
            "runaway_output_count": self.runaway_output_count,
            "response_timeout_count": self.response_timeout_count,
            "malformed_count": self.malformed_count,
            "teardown_violation_count": self.teardown_violation_count,
            "error_code": self.error_code,
            "commitment": self.commitment,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ActivityPrimitiveRecord":
        fields = set(cls.__dataclass_fields__)
        data = _strict_mapping(raw, fields, label="activity primitive")
        values = dict(data)
        values["scenario_tags"] = _string_tuple(data["scenario_tags"], label="scenario tags")
        critical = data["critical_spans"]
        if not isinstance(critical, list):
            raise MeasurementError("critical spans must be an array")
        values["critical_spans"] = tuple(CriticalSpanFact.from_dict(value) for value in critical)
        return cls(**values)

    @classmethod
    def with_commitment(
        cls,
        record: "ActivityPrimitiveRecord",
        *,
        commitment_key: bytes,
    ) -> "ActivityPrimitiveRecord":
        if not isinstance(record, cls):
            raise TypeError("record must be an ActivityPrimitiveRecord")
        unsigned = replace(record, commitment="")
        commitment = _record_commitment(unsigned.unsigned_dict(), key=commitment_key)
        return replace(unsigned, commitment=commitment)


@dataclass(frozen=True, slots=True)
class NoSpeechPrimitiveRecord:
    schema_id: str
    window_ordinal: int
    split: str
    condition: str
    false_activity_count: int
    model_audio_chunk_count: int
    abnormal_close_count: int
    audio_after_teardown_count: int
    error_code: str | None
    commitment: str

    def __post_init__(self) -> None:
        if self.schema_id != NO_SPEECH_PRIMITIVE_SCHEMA_ID:
            raise MeasurementError("no-speech primitive schema is invalid")
        _bounded_int(self.window_ordinal, label="window ordinal", maximum=63)
        _validate_split(self.split)
        _safe_id(self.condition, label="condition")
        for label, value in (
            ("false activity", self.false_activity_count),
            ("model audio chunk", self.model_audio_chunk_count),
            ("abnormal close", self.abnormal_close_count),
            ("audio after teardown", self.audio_after_teardown_count),
        ):
            _bounded_int(value, label=label)
        _optional_safe_id(self.error_code, label="error code")
        if self.commitment and not SHA256.fullmatch(self.commitment):
            raise MeasurementError("record commitment must be SHA-256")

    def unsigned_dict(self) -> dict[str, Any]:
        value = self.to_dict()
        value.pop("commitment")
        return value

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NoSpeechPrimitiveRecord":
        data = _strict_mapping(
            raw,
            set(cls.__dataclass_fields__),
            label="no-speech primitive",
        )
        return cls(**dict(data))

    @classmethod
    def with_commitment(
        cls,
        record: "NoSpeechPrimitiveRecord",
        *,
        commitment_key: bytes,
    ) -> "NoSpeechPrimitiveRecord":
        if not isinstance(record, cls):
            raise TypeError("record must be a NoSpeechPrimitiveRecord")
        unsigned = replace(record, commitment="")
        commitment = _record_commitment(unsigned.unsigned_dict(), key=commitment_key)
        return replace(unsigned, commitment=commitment)


def require_reducer_agreement(
    primary: tuple[CallerTurnEvent, ...],
    independent: tuple[CallerTurnEvent, ...],
) -> tuple[CallerTurnEvent, ...]:
    """Reject unless two separately supplied reductions are byte-equivalent."""
    for label, events in (("primary", primary), ("independent", independent)):
        if (
            not isinstance(events, tuple)
            or any(not isinstance(event, CallerTurnEvent) for event in events)
        ):
            raise MeasurementError(f"{label} reducer output is invalid")
    if canonical_json_bytes([_event_dict(value) for value in primary]) != canonical_json_bytes(
        [_event_dict(value) for value in independent]
    ):
        raise MeasurementError("independent reducer disagreement")
    return primary


def _fragment_is_foreign(
    fragment: str,
    *,
    activity_ordinal: int,
    references: tuple[ActivityReference, ...],
    policy: AlignmentPolicy,
) -> bool:
    alignment = align_fragments((fragment,), references=references, policy=policy)
    if (
        alignment.status is AlignmentStatus.MATCHED
        and alignment.activity_ordinal != activity_ordinal
    ):
        return True

    containing_ordinals = {
        reference.activity_ordinal
        for reference in references
        if _reference_contains_fragment(reference, fragment)
    }
    if containing_ordinals and activity_ordinal not in containing_ordinals:
        return True

    expected_reference = next(
        reference
        for reference in references
        if reference.activity_ordinal == activity_ordinal
    )
    if any(
        reference.activity_ordinal != activity_ordinal
        and _fragment_contains_unique_foreign_sequence(
            fragment,
            expected_reference=expected_reference,
            foreign_reference=reference,
        )
        for reference in references
    ):
        return True
    return _fragment_contains_foreign_token(
        fragment,
        activity_ordinal=activity_ordinal,
        references=references,
    )


def _reference_contains_fragment(reference: ActivityReference, fragment: str) -> bool:
    normalized_reference = normalize_text(reference.text, reference.language)
    normalized_fragment = normalize_text(fragment, reference.language)
    if not normalized_fragment.characters:
        return False
    if normalized_reference.words is not None and normalized_fragment.words is not None:
        return _contains_tokens(normalized_reference.words, normalized_fragment.words)
    return _contains_tokens(
        normalized_reference.characters,
        normalized_fragment.characters,
    )


def _fragment_contains_unique_foreign_sequence(
    fragment: str,
    *,
    expected_reference: ActivityReference,
    foreign_reference: ActivityReference,
) -> bool:
    normalized_fragment = normalize_text(fragment, foreign_reference.language)
    normalized_expected = normalize_text(
        expected_reference.text,
        foreign_reference.language,
    )
    normalized_foreign = normalize_text(
        foreign_reference.text,
        foreign_reference.language,
    )
    if (
        normalized_fragment.words is not None
        and normalized_expected.words is not None
        and normalized_foreign.words is not None
    ):
        fragment_tokens = normalized_fragment.words
        expected_tokens = normalized_expected.words
        foreign_tokens = normalized_foreign.words
    else:
        fragment_tokens = normalized_fragment.characters
        expected_tokens = normalized_expected.characters
        foreign_tokens = normalized_foreign.characters
    return any(
        _contains_tokens(fragment_tokens, candidate)
        and not _contains_tokens(expected_tokens, candidate)
        for candidate in (
            foreign_tokens[index : index + 2]
            for index in range(len(foreign_tokens) - 1)
        )
    )


def _fragment_contains_foreign_token(
    fragment: str,
    *,
    activity_ordinal: int,
    references: tuple[ActivityReference, ...],
) -> bool:
    expected_reference = next(
        reference
        for reference in references
        if reference.activity_ordinal == activity_ordinal
    )
    foreign_references = tuple(
        reference
        for reference in references
        if reference.activity_ordinal != activity_ordinal
    )
    for language in sorted({reference.language for reference in foreign_references}):
        fragment_tokens = set(_normalized_tokens(fragment, language))
        expected_tokens = set(_normalized_tokens(expected_reference.text, language))
        candidate_tokens = {
            token
            for reference in foreign_references
            if reference.language == language
            for token in _normalized_tokens(reference.text, language)
        }
        if any(
            token in candidate_tokens
            and token not in expected_tokens
            for token in fragment_tokens
        ):
            return True
    return False


def _normalized_tokens(text: str, language: str) -> tuple[str, ...]:
    normalized = normalize_text(text, language)
    return normalized.characters if normalized.words is None else normalized.words


def _contains_tokens(container: tuple[str, ...], target: tuple[str, ...]) -> bool:
    return bool(target) and any(
        container[index : index + len(target)] == target
        for index in range(len(container) - len(target) + 1)
    )


def measure_activity(
    measurement: ActivityMeasurementInput,
    *,
    alignment_policy: AlignmentPolicy,
    commitment_key: bytes,
) -> ActivityPrimitiveRecord:
    """Recompute one activity primitive from typed audit evidence."""
    if not isinstance(measurement, ActivityMeasurementInput):
        raise TypeError("measurement must be an ActivityMeasurementInput")
    if not isinstance(alignment_policy, AlignmentPolicy):
        raise TypeError("alignment_policy must be an AlignmentPolicy")
    assembly = _assemble_session_events(
        measurement.events,
        event_activity_ordinals=(measurement.activity_ordinal,) * len(measurement.events),
        activity_expected_epochs={
            measurement.activity_ordinal: measurement.expected_epoch
        },
        policy_ms=measurement.policy_ms,
        advance_to_ms=measurement.advance_to_ms,
    )[measurement.activity_ordinal]
    return _measure_activity(
        measurement,
        alignment_policy=alignment_policy,
        commitment_key=commitment_key,
        assembly=assembly,
    )


def _measure_activity(
    measurement: ActivityMeasurementInput,
    *,
    alignment_policy: AlignmentPolicy,
    commitment_key: bytes,
    assembly: _ActivityAssemblyObservation,
) -> ActivityPrimitiveRecord:
    expected_reference = next(
        value
        for value in measurement.references
        if value.activity_ordinal == measurement.activity_ordinal
    )
    alignment = align_caller_turn_events(
        measurement.events,
        references=measurement.references,
        policy=alignment_policy,
    )
    fragments = tuple(
        event.text
        for event in measurement.events
        if event.kind is CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT
    )
    expected_alignment = align_fragments(
        fragments,
        references=(expected_reference,),
        policy=alignment_policy,
    )
    foreign_fragment_count = sum(
        _fragment_is_foreign(
            fragment,
            activity_ordinal=measurement.activity_ordinal,
            references=measurement.references,
            policy=alignment_policy,
        )
        for fragment in fragments
    )
    reconstructed = reconstruct_fragments(fragments, policy=alignment_policy)
    normalized_reference = normalize_text(expected_reference.text, measurement.language)
    normalized_hypothesis = normalize_text(reconstructed, measurement.language)

    turns = assembly.turns
    if not turns:
        lifecycle = "missing"
    elif len(turns) > 1:
        lifecycle = "duplicate"
    else:
        lifecycle = turns[0].status.value

    word_edits = expected_alignment.word_edits
    word_values = (
        (
            len(normalized_reference.words),
            len(normalized_hypothesis.words),
            word_edits.substitutions,
            word_edits.insertions,
            word_edits.deletions,
        )
        if normalized_reference.words is not None
        and normalized_hypothesis.words is not None
        and word_edits is not None
        else (None, None, None, None, None)
    )
    margin = alignment.ambiguity_margin
    margin_micros = (
        1_000_000
        if margin is None
        else min(1_000_000, _fraction_to_micros(margin.numerator, margin.denominator))
    )
    wire = measurement.wire
    record = ActivityPrimitiveRecord(
        schema_id=ACTIVITY_PRIMITIVE_SCHEMA_ID,
        policy_ms=measurement.policy_ms,
        activity_ordinal=measurement.activity_ordinal,
        split=measurement.split,
        language=measurement.language,
        condition=measurement.condition,
        scenario_tags=measurement.scenario_tags,
        assignment_status=alignment.status.value,
        expected_lifecycle_status=measurement.expected_lifecycle_status,
        observed_lifecycle_status=lifecycle,
        assembled_turn_count=len(turns),
        fragment_count=len(fragments),
        event_count=len(measurement.events),
        reference_characters=len(normalized_reference.characters),
        hypothesis_characters=len(normalized_hypothesis.characters),
        substitutions=expected_alignment.character_edits.substitutions,
        insertions=expected_alignment.character_edits.insertions,
        deletions=expected_alignment.character_edits.deletions,
        reference_words=word_values[0],
        hypothesis_words=word_values[1],
        word_substitutions=word_values[2],
        word_insertions=word_values[3],
        word_deletions=word_values[4],
        ambiguity_margin_micros=margin_micros,
        critical_spans=tuple(
            CriticalSpanFact(kind=value.kind.value, exact=value.exact)
            for value in expected_alignment.critical_spans
        ),
        contamination_count=max(
            foreign_fragment_count,
            assembly.cross_activity_merge_count,
            int(
                alignment.activity_ordinal is not None
                and alignment.activity_ordinal != measurement.activity_ordinal
            ),
        ),
        duplicate_count=max(0, len(turns) - 1),
        cross_epoch_acceptance_count=sum(
            turn.epoch != measurement.expected_epoch for turn in turns
        ),
        late_fragment_mutation_count=assembly.late_fragment_mutation_count,
        stale_count=assembly.stale_count,
        timing_covered=wire.timing_covered,
        first_audio_ms=wire.first_audio_ms,
        interruption_tail_ms=wire.interruption_tail_ms,
        premature_current_audio_count=wire.premature_current_audio_count,
        audio_after_terminal_count=wire.audio_after_terminal_count,
        response_gap_violation_count=wire.response_gap_violation_count,
        abnormal_close_count=wire.abnormal_close_count,
        runaway_output_count=wire.runaway_output_count,
        response_timeout_count=wire.response_timeout_count,
        malformed_count=wire.malformed_count,
        teardown_violation_count=wire.teardown_violation_count,
        error_code=wire.error_code,
        commitment="",
    )
    return ActivityPrimitiveRecord.with_commitment(record, commitment_key=commitment_key)


def _assemble_session_events(
    events: tuple[CallerTurnEvent, ...],
    *,
    event_activity_ordinals: tuple[int, ...],
    activity_expected_epochs: Mapping[int, int],
    policy_ms: int,
    advance_to_ms: int,
) -> dict[int, _ActivityAssemblyObservation]:
    if len(events) != len(event_activity_ordinals):
        raise MeasurementError("session event ownership cardinality is invalid")
    if not activity_expected_epochs:
        raise MeasurementError("session activity epochs are missing")
    activity_ordinals = tuple(activity_expected_epochs)
    if set(event_activity_ordinals) - set(activity_ordinals):
        raise MeasurementError("session event lacks causal activity ownership")
    _validate_policy(policy_ms)
    _bounded_int(advance_to_ms, label="advance time", maximum=7_200_000)
    if events and advance_to_ms < events[-1].at_ms:
        raise MeasurementError("advance time precedes the final event")
    for epoch in activity_expected_epochs.values():
        _bounded_int(epoch, label="expected epoch", maximum=1_000)

    assembler = CallerTurnAssembler(
        active_epoch=min(activity_expected_epochs.values()),
        quiescence_ms=policy_ms,
    )
    turns_by_activity: dict[int, list[RetrospectiveCallerTurn]] = {
        ordinal: [] for ordinal in activity_ordinals
    }
    merge_counts: Counter[int] = Counter()
    late_fragment_counts: Counter[int] = Counter()
    stale_counts: Counter[int] = Counter()
    pending_owners: list[int] = []

    def attribute_turn(
        turn: RetrospectiveCallerTurn,
        owners: tuple[int, ...],
    ) -> frozenset[int]:
        unique_owners = frozenset(owners)
        if not unique_owners:
            raise MeasurementError("assembled turn lacks causal activity ownership")
        for owner in unique_owners:
            turns_by_activity[owner].append(turn)
        if len(unique_owners) > 1:
            merge_counts.update(unique_owners)
        return unique_owners

    for event, owner in zip(events, event_activity_ordinals, strict=True):
        # Gemini tool cancellation controls the prior model response. The wire
        # record measures it, but it is not a terminal for the caller activity
        # whose speech caused the interruption.
        if event.kind is CallerTurnEventKind.TOOL_CALL_CANCELLED:
            continue
        pending_before = tuple(pending_owners)
        deadline_before = assembler.next_deadline_ms
        stale_before = assembler.stale_event_count
        duplicates_before = assembler.duplicate_event_count
        emitted = assembler.ingest(event)
        stale_delta = assembler.stale_event_count - stale_before
        duplicate_delta = assembler.duplicate_event_count - duplicates_before
        ignored = bool(stale_delta or duplicate_delta)
        current_was_emitted = False
        emitted_owner_sets: list[frozenset[int]] = []

        for turn in emitted:
            if turn.event_count == len(pending_before):
                turn_owners = pending_before
            elif not ignored and turn.event_count == len(pending_before) + 1:
                turn_owners = (*pending_before, owner)
                current_was_emitted = True
            else:
                raise MeasurementError("assembled turn ownership is inconsistent")
            emitted_owner_sets.append(attribute_turn(turn, turn_owners))

        if stale_delta:
            stale_counts[owner] += stale_delta
            continue

        expired_before_event = (
            deadline_before is not None and event.at_ms >= deadline_before
        )
        if expired_before_event or emitted:
            pending_owners.clear()

        if ignored:
            continue

        if event.kind is CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT:
            same_owner_late_turn = any(
                owner in emitted_owners for emitted_owners in emitted_owner_sets
            )
            if same_owner_late_turn:
                late_fragment_counts[owner] += 1

        if event.kind is CallerTurnEventKind.RECONNECT_STARTED:
            pending_owners.clear()
        elif event.kind in {
            CallerTurnEventKind.TOOL_CALL_CANCELLED,
            CallerTurnEventKind.CONNECTION_CLOSED,
            CallerTurnEventKind.PIPELINE_STOPPED,
        }:
            pending_owners.clear()
        elif current_was_emitted:
            pending_owners.clear()
        else:
            pending_owners.append(owner)

    for turn in assembler.advance_time(advance_to_ms):
        if turn.event_count != len(pending_owners):
            raise MeasurementError("assembled turn ownership is inconsistent")
        attribute_turn(turn, tuple(pending_owners))

    if assembler.stale_event_count != sum(stale_counts.values()):
        raise MeasurementError("session stale-event attribution is inconsistent")
    return {
        ordinal: _ActivityAssemblyObservation(
            turns=tuple(turns_by_activity[ordinal]),
            cross_activity_merge_count=merge_counts[ordinal],
            late_fragment_mutation_count=late_fragment_counts[ordinal],
            stale_count=stale_counts[ordinal],
        )
        for ordinal in activity_ordinals
    }


def verify_record_commitment(
    record: ActivityPrimitiveRecord | NoSpeechPrimitiveRecord,
    *,
    commitment_key: bytes,
) -> bool:
    if not isinstance(record, (ActivityPrimitiveRecord, NoSpeechPrimitiveRecord)):
        raise TypeError("record is not a Gate 0B primitive")
    expected = _record_commitment(record.unsigned_dict(), key=commitment_key)
    return bool(record.commitment) and hmac.compare_digest(record.commitment, expected)


def seal_audit_capsule(
    payload: Mapping[str, Any],
    *,
    custodian_public_key: bytes,
    custodian_key_id: str,
) -> dict[str, Any]:
    """Seal an allowlisted capsule without access to the custodian private key."""
    validated = _validate_audit_capsule(payload)
    serialized = canonical_json_bytes(validated)
    if len(serialized) > MAX_CAPSULE_BYTES:
        raise MeasurementError("audit capsule exceeds the fixed size bound")
    key_id = _safe_id(custodian_key_id, label="custodian key ID")
    try:
        recipient = X25519PublicKey.from_public_bytes(custodian_public_key)
    except (TypeError, ValueError) as exc:
        raise MeasurementError("custodian public key is invalid") from exc
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    key = _capsule_key(ephemeral.exchange(recipient), key_id=key_id)
    nonce = os.urandom(12)
    header = {
        "schema_id": SEALED_CAPSULE_SCHEMA_ID,
        "custodian_key_id": key_id,
        "ephemeral_public_key": base64.b64encode(ephemeral_public).decode("ascii"),
    }
    ciphertext = AESGCM(key).encrypt(nonce, serialized, canonical_json_bytes(header))
    return {
        **header,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def open_audit_capsule(
    envelope: Mapping[str, Any],
    *,
    custodian_private_key: X25519PrivateKey,
    expected_key_id: str,
) -> dict[str, Any]:
    data = _strict_mapping(
        envelope,
        {
            "schema_id",
            "custodian_key_id",
            "ephemeral_public_key",
            "nonce",
            "ciphertext",
        },
        label="sealed audit capsule",
    )
    if data["schema_id"] != SEALED_CAPSULE_SCHEMA_ID:
        raise MeasurementError("sealed audit capsule schema is invalid")
    if data["custodian_key_id"] != expected_key_id:
        raise MeasurementError("audit custodian key identity mismatch")
    if not isinstance(custodian_private_key, X25519PrivateKey):
        raise TypeError("custodian_private_key must be an X25519PrivateKey")
    try:
        for field in ("ephemeral_public_key", "nonce", "ciphertext"):
            if not isinstance(data[field], str) or len(data[field]) > MAX_CAPSULE_BYTES * 2:
                raise MeasurementError("sealed audit capsule encoding is invalid")
        ephemeral_bytes = base64.b64decode(data["ephemeral_public_key"], validate=True)
        nonce = base64.b64decode(data["nonce"], validate=True)
        ciphertext = base64.b64decode(data["ciphertext"], validate=True)
        ephemeral = X25519PublicKey.from_public_bytes(ephemeral_bytes)
    except (TypeError, ValueError) as exc:
        raise MeasurementError("sealed audit capsule encoding is invalid") from exc
    header = {
        "schema_id": data["schema_id"],
        "custodian_key_id": data["custodian_key_id"],
        "ephemeral_public_key": data["ephemeral_public_key"],
    }
    key = _capsule_key(custodian_private_key.exchange(ephemeral), key_id=expected_key_id)
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, canonical_json_bytes(header))
    try:
        decoded = json.loads(plaintext)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MeasurementError("audit capsule plaintext is invalid") from exc
    return _validate_audit_capsule(decoded)


def derive_audit_capsule_accounting(
    capsule: Mapping[str, Any],
) -> tuple[dict[str, int | bool], tuple[str, ...]]:
    """Aggregate bounded usage facts from one independently opened capsule."""
    data = _validate_audit_capsule(capsule)
    units = data["accounting"]["units"]
    total_elapsed_ms = sum(unit["observed_elapsed_ms"] for unit in units)
    total_input_audio_ms = sum(unit["input_audio_duration_ms"] for unit in units)
    total_output_audio_bytes = sum(unit["output_audio_bytes"] for unit in units)
    usage: dict[str, int | bool] = {
        "metadata_complete": all(unit["metadata_complete"] for unit in units),
        "provider_requests": sum(unit["provider_request_count"] for unit in units),
        "observed_elapsed_ms": total_elapsed_ms,
        "input_audio_duration_ms": total_input_audio_ms,
        "output_audio_bytes": total_output_audio_bytes,
        "wall_clock_seconds": (total_elapsed_ms + 999) // 1_000,
        "input_audio_seconds": (total_input_audio_ms + 999) // 1_000,
        "output_audio_seconds": (total_output_audio_bytes + 47_999) // 48_000,
        "input_audio_tokens": sum(unit["input_audio_tokens"] for unit in units),
        "output_audio_tokens": sum(unit["output_audio_tokens"] for unit in units),
        "input_text_tokens": sum(unit["input_text_tokens"] for unit in units),
        "output_text_tokens": sum(unit["output_text_tokens"] for unit in units),
    }
    failures = tuple(
        unit["error_code"]
        for unit in units
        if not unit["complete"] and unit["error_code"] is not None
    )
    return usage, failures


def usage_evidence_sha256(
    usage: Mapping[str, Any],
    *,
    provider_requests: int,
    cost_microusd: int,
) -> str:
    """Digest the token, request, and cost facts signed into custody receipts."""
    token_fields = (
        "input_audio_tokens",
        "output_audio_tokens",
        "input_text_tokens",
        "output_text_tokens",
    )
    values = {
        field: _bounded_int(
            usage.get(field),
            label=f"usage {field}",
            maximum=MAX_ACCOUNTING_COUNTER,
        )
        for field in token_fields
    }
    values["provider_requests"] = _bounded_int(
        provider_requests,
        label="usage provider requests",
        maximum=1_000,
    )
    values["cost_microusd"] = _bounded_int(
        cost_microusd,
        label="usage cost",
        maximum=100_000_000,
    )
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_id": "gate_0b_usage_evidence_v1",
                **values,
            }
        )
    ).hexdigest()


def combined_usage_evidence_sha256(
    *,
    development_usage_evidence_sha256: str,
    holdout_usage_evidence_sha256: str,
    provider_requests: int,
    cost_microusd: int,
) -> str:
    """Bind both split digests and cumulative accounting into one final receipt."""
    for value in (development_usage_evidence_sha256, holdout_usage_evidence_sha256):
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            raise MeasurementError("split usage evidence digest is invalid")
    requests = _bounded_int(
        provider_requests,
        label="combined usage provider requests",
        maximum=1_000,
    )
    cost = _bounded_int(
        cost_microusd,
        label="combined usage cost",
        maximum=100_000_000,
    )
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_id": "gate_0b_combined_usage_evidence_v1",
                "development_usage_evidence_sha256": development_usage_evidence_sha256,
                "holdout_usage_evidence_sha256": holdout_usage_evidence_sha256,
                "provider_requests": requests,
                "cost_microusd": cost,
            }
        )
    ).hexdigest()


def derive_primitive_records_from_capsule(
    capsule: Mapping[str, Any],
    *,
    policies_ms: tuple[int, ...],
    commitment_key: bytes,
) -> tuple[tuple[ActivityPrimitiveRecord, ...], tuple[NoSpeechPrimitiveRecord, ...]]:
    """Independently derive committed primitives from one opened custody capsule."""
    data = _validate_audit_capsule(capsule)
    if (
        not isinstance(policies_ms, tuple)
        or not policies_ms
        or len(policies_ms) != len(set(policies_ms))
    ):
        raise MeasurementError("capsule policy set is invalid")
    for policy_ms in policies_ms:
        _validate_policy(policy_ms)

    sessions = data["sessions"]
    activities = data["activities"]
    windows = data["no_speech_windows"]
    splits = {value["split"] for value in (*activities, *windows)}
    if len(splits) != 1:
        raise MeasurementError("audit capsule must contain exactly one split")
    split = next(iter(splits))
    if split == "holdout" and policies_ms != (data["policy_ms"],):
        raise MeasurementError("holdout capsule must use only its selected policy")

    session_index = {value["session_ordinal"]: value for value in sessions}
    by_session: dict[int, list[Mapping[str, Any]]] = {}
    for activity in activities:
        by_session.setdefault(activity["session_ordinal"], []).append(activity)

    activity_records: list[ActivityPrimitiveRecord] = []
    for session_activities in by_session.values():
        session = session_index[session_activities[0]["session_ordinal"]]
        references = tuple(_capsule_reference(activity) for activity in session_activities)
        events = tuple(CallerTurnEvent.from_dict(value) for value in session["events"])
        attributed_events = _attribute_session_events(
            events,
            event_activity_ordinals=session["event_activity_ordinals"],
            references=references,
        )
        assembly_by_policy = {
            policy_ms: _assemble_session_events(
                events,
                event_activity_ordinals=tuple(session["event_activity_ordinals"]),
                activity_expected_epochs={
                    activity["activity_ordinal"]: activity["expected_epoch"]
                    for activity in session_activities
                },
                policy_ms=policy_ms,
                advance_to_ms=max(
                    activity["advance_to_ms"] for activity in session_activities
                ),
            )
            for policy_ms in policies_ms
        }
        for activity in session_activities:
            wire, activity_end_ms = _capsule_wire_observation(
                session["wire_facts"],
                activity_ordinal=activity["activity_ordinal"],
                scenario_tags=tuple(activity["scenario_tags"]),
                speech_end_at_ms=activity["speech_end_at_ms"],
            )
            if activity["advance_to_ms"] < activity_end_ms + max(policies_ms):
                raise MeasurementError("capsule observation window is shorter than policy set")
            for policy_ms in policies_ms:
                activity_records.append(
                    _measure_activity(
                        ActivityMeasurementInput(
                            policy_ms=policy_ms,
                            activity_ordinal=activity["activity_ordinal"],
                            split=activity["split"],
                            language=activity["language"],
                            condition=activity["condition"],
                            scenario_tags=tuple(activity["scenario_tags"]),
                            references=references,
                            events=attributed_events.get(activity["activity_ordinal"], ()),
                            expected_epoch=activity["expected_epoch"],
                            expected_lifecycle_status=activity["expected_lifecycle_status"],
                            advance_to_ms=activity["advance_to_ms"],
                            wire=wire,
                        ),
                        alignment_policy=AlignmentPolicy(fragment_mode=FragmentMode.DELTA),
                        commitment_key=commitment_key,
                        assembly=assembly_by_policy[policy_ms][
                            activity["activity_ordinal"]
                        ],
                    )
                )

    no_speech_records = tuple(
        NoSpeechPrimitiveRecord.with_commitment(
            NoSpeechPrimitiveRecord(
                schema_id=NO_SPEECH_PRIMITIVE_SCHEMA_ID,
                window_ordinal=window["window_ordinal"],
                split=window["split"],
                condition=window["condition"],
                false_activity_count=_fact_count(window["wire_facts"], "false_activity"),
                model_audio_chunk_count=_fact_count(window["wire_facts"], "audio_received"),
                abnormal_close_count=_fact_count(window["wire_facts"], "abnormal_close"),
                audio_after_teardown_count=_audio_after_teardown_count(
                    window["wire_facts"]
                ),
                error_code=_wire_error_code(window["wire_facts"]),
                commitment="",
            ),
            commitment_key=commitment_key,
        )
        for window in windows
    )
    return tuple(activity_records), no_speech_records


def _attribute_session_events(
    events: tuple[CallerTurnEvent, ...],
    *,
    event_activity_ordinals: list[int],
    references: tuple[ActivityReference, ...],
) -> dict[int, tuple[CallerTurnEvent, ...]]:
    if len(events) != len(event_activity_ordinals):
        raise MeasurementError("session event ownership cardinality is invalid")
    attributed: dict[int, list[CallerTurnEvent]] = {}
    reference_ordinals = {reference.activity_ordinal for reference in references}
    for event, activity_ordinal in zip(
        events,
        event_activity_ordinals,
        strict=True,
    ):
        if activity_ordinal not in reference_ordinals:
            raise MeasurementError("session event lacks causal activity ownership")
        attributed.setdefault(activity_ordinal, []).append(event)
    return {ordinal: tuple(values) for ordinal, values in attributed.items()}


def build_signed_record_root(
    *,
    activity_records: tuple[ActivityPrimitiveRecord, ...],
    no_speech_records: tuple[NoSpeechPrimitiveRecord, ...],
    campaign_id: str,
    signing_key: Ed25519PrivateKey,
    key_id: str,
) -> dict[str, Any]:
    campaign = _safe_id(campaign_id, label="campaign ID")
    signer = _safe_id(key_id, label="record root key ID")
    if not isinstance(signing_key, Ed25519PrivateKey):
        raise TypeError("signing_key must be an Ed25519PrivateKey")
    payload = {
        "schema_id": SIGNED_ROOT_SCHEMA_ID,
        "campaign_id": campaign,
        "leaf_count": len(activity_records) + len(no_speech_records),
        "merkle_root_sha256": compute_record_merkle_root(
            activity_records=activity_records,
            no_speech_records=no_speech_records,
        ),
    }
    return {
        "key_id": signer,
        "payload": payload,
        "signature": base64.b64encode(signing_key.sign(canonical_json_bytes(payload))).decode(
            "ascii"
        ),
    }


def verify_signed_record_root(
    envelope: Mapping[str, Any],
    *,
    activity_records: tuple[ActivityPrimitiveRecord, ...],
    no_speech_records: tuple[NoSpeechPrimitiveRecord, ...],
    campaign_id: str,
    public_key: bytes,
    expected_key_id: str,
) -> bool:
    try:
        data = _strict_mapping(
            envelope,
            {"key_id", "payload", "signature"},
            label="signed record root",
        )
        if data["key_id"] != expected_key_id:
            return False
        payload = _strict_mapping(
            data["payload"],
            {"schema_id", "campaign_id", "leaf_count", "merkle_root_sha256"},
            label="signed record root payload",
        )
        expected = {
            "schema_id": SIGNED_ROOT_SCHEMA_ID,
            "campaign_id": _safe_id(campaign_id, label="campaign ID"),
            "leaf_count": len(activity_records) + len(no_speech_records),
            "merkle_root_sha256": compute_record_merkle_root(
                activity_records=activity_records,
                no_speech_records=no_speech_records,
            ),
        }
        if dict(payload) != expected:
            return False
        signature = base64.b64decode(data["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature,
            canonical_json_bytes(payload),
        )
        return True
    except (KeyError, TypeError, ValueError, InvalidSignature, MeasurementError):
        return False


def compute_record_merkle_root(
    *,
    activity_records: tuple[ActivityPrimitiveRecord, ...],
    no_speech_records: tuple[NoSpeechPrimitiveRecord, ...],
) -> str:
    leaves = []
    for record in activity_records:
        if not isinstance(record, ActivityPrimitiveRecord):
            raise TypeError("activity record set is invalid")
        sort_key = ("activity", record.split, record.policy_ms, record.activity_ordinal)
        leaves.append((sort_key, b"activity\x00" + canonical_json_bytes(record.to_dict())))
    for record in no_speech_records:
        if not isinstance(record, NoSpeechPrimitiveRecord):
            raise TypeError("no-speech record set is invalid")
        sort_key = ("no_speech", record.split, 0, record.window_ordinal)
        leaves.append((sort_key, b"no-speech\x00" + canonical_json_bytes(record.to_dict())))
    if not leaves:
        return hashlib.sha256(b"gate-0b-empty-merkle-v1").hexdigest()
    level = [hashlib.sha256(payload).digest() for _, payload in sorted(leaves)]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(b"gate-0b-node-v1\x00" + level[index] + level[index + 1]).digest()
            for index in range(0, len(level), 2)
        ]
    return level[0].hex()


def _record_commitment(value: Mapping[str, Any], *, key: bytes) -> str:
    if not isinstance(key, bytes) or len(key) != 32:
        raise MeasurementError("commitment key must be exactly 32 bytes")
    return hmac.new(key, RECORD_HMAC_DOMAIN + canonical_json_bytes(value), hashlib.sha256).hexdigest()


def _capsule_key(shared_secret: bytes, *, key_id: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(key_id.encode("ascii")).digest(),
        info=CAPSULE_KDF_INFO,
    ).derive(shared_secret)


def _validate_audit_capsule(raw: Mapping[str, Any]) -> dict[str, Any]:
    _reject_forbidden_capsule_keys(raw)
    data = _strict_mapping(
        raw,
        {
            "schema_id",
            "campaign_id",
            "policy_ms",
            "source_fact_bundle_sha256",
            "execution_started_at",
            "execution_completed_at",
            "provider_revision",
            "runtime_identity_before_sha256",
            "runtime_identity_after_sha256",
            "runtime_identity_before",
            "runtime_identity_after",
            "accounting",
            "sessions",
            "activities",
            "no_speech_windows",
        },
        label="audit capsule",
    )
    if data["schema_id"] != AUDIT_CAPSULE_SCHEMA_ID:
        raise MeasurementError("audit capsule schema is invalid")
    _safe_id(data["campaign_id"], label="campaign ID")
    _validate_policy(data["policy_ms"])
    for field in (
        "source_fact_bundle_sha256",
        "runtime_identity_before_sha256",
        "runtime_identity_after_sha256",
    ):
        if not isinstance(data[field], str) or not SHA256.fullmatch(data[field]):
            raise MeasurementError("audit capsule identity digest is invalid")
    if data["runtime_identity_before_sha256"] != data["runtime_identity_after_sha256"]:
        raise MeasurementError("audit capsule runtime identity drifted")
    try:
        runtime_before = validate_execution_identity_report(data["runtime_identity_before"])
        runtime_after = validate_execution_identity_report(data["runtime_identity_after"])
    except ValueError as exc:
        raise MeasurementError("audit capsule runtime identity is invalid") from exc
    if (
        runtime_before != runtime_after
        or execution_identity_report_sha256(runtime_before)
        != data["runtime_identity_before_sha256"]
        or execution_identity_report_sha256(runtime_after)
        != data["runtime_identity_after_sha256"]
    ):
        raise MeasurementError("audit capsule runtime identity drifted")
    started_at = _parse_utc_timestamp(data["execution_started_at"])
    completed_at = _parse_utc_timestamp(data["execution_completed_at"])
    if completed_at < started_at:
        raise MeasurementError("audit capsule execution timestamps are invalid")
    provider_revision = data["provider_revision"]
    if provider_revision is not None and (
        not isinstance(provider_revision, str)
        or not PROVIDER_REVISION.fullmatch(provider_revision)
    ):
        raise MeasurementError("audit capsule provider revision is invalid")
    sessions = data["sessions"]
    activities = data["activities"]
    windows = data["no_speech_windows"]
    if not isinstance(sessions, list) or len(sessions) > 64:
        raise MeasurementError("audit capsule sessions are invalid")
    if not isinstance(activities, list) or len(activities) > 256:
        raise MeasurementError("audit capsule activities are invalid")
    if not isinstance(windows, list) or len(windows) > 64:
        raise MeasurementError("audit capsule no-speech windows are invalid")
    accounting = _validate_capsule_accounting(data["accounting"])
    session_index: dict[int, Mapping[str, Any]] = {}
    session_events: dict[int, tuple[CallerTurnEvent, ...]] = {}
    for raw_session in sessions:
        session = _strict_mapping(
            raw_session,
            {
                "session_ordinal",
                "split",
                "events",
                "event_activity_ordinals",
                "wire_facts",
            },
            label="audit capsule session",
        )
        session_ordinal = _bounded_int(
            session["session_ordinal"], label="session ordinal", maximum=63
        )
        if session_ordinal in session_index:
            raise MeasurementError("audit capsule session identity is duplicated")
        _validate_split(session["split"])
        events = session["events"]
        if not isinstance(events, list) or len(events) > 10_000:
            raise MeasurementError("audit capsule events are invalid")
        typed_events = tuple(CallerTurnEvent.from_dict(event) for event in events)
        _validate_wire_facts(session["wire_facts"])
        _validate_response_wire_state(session["wire_facts"])
        _validate_event_activity_ordinals(
            session["event_activity_ordinals"],
            events=typed_events,
            wire_facts=session["wire_facts"],
        )
        session_index[session_ordinal] = session
        session_events[session_ordinal] = typed_events
    activity_ordinals: set[int] = set()
    for raw_activity in activities:
        activity = _strict_mapping(
            raw_activity,
            {
                "activity_ordinal",
                "session_ordinal",
                "split",
                "language",
                "condition",
                "scenario_tags",
                "reference_text",
                "critical_spans",
                "expected_lifecycle_status",
                "expected_epoch",
                "speech_end_at_ms",
                "advance_to_ms",
            },
            label="audit capsule activity",
        )
        ordinal = _bounded_int(
            activity["activity_ordinal"],
            label="activity ordinal",
            maximum=255,
        )
        if ordinal in activity_ordinals:
            raise MeasurementError("audit capsule activity identity is duplicated")
        activity_ordinals.add(ordinal)
        session_ordinal = _bounded_int(
            activity["session_ordinal"], label="session ordinal", maximum=63
        )
        language = _validate_language(activity["language"])
        _validate_split(activity["split"])
        _safe_id(activity["condition"], label="condition")
        _string_tuple(activity["scenario_tags"], label="scenario tags")
        spans = activity["critical_spans"]
        if not isinstance(spans, list) or len(spans) > 16:
            raise MeasurementError("audit capsule critical spans are invalid")
        typed_spans = []
        for raw_span in spans:
            span = _strict_mapping(
                raw_span,
                {"kind", "text", "language"},
                label="audit capsule critical span",
            )
            typed_spans.append(
                CriticalSpan(
                    kind=CriticalSpanKind(span["kind"]),
                    text=span["text"],
                    language=span["language"],
                )
            )
        ActivityReference(
            activity_ordinal=ordinal,
            language=language,
            text=activity["reference_text"],
            critical_spans=tuple(typed_spans),
        )
        session = session_index.get(session_ordinal)
        if session is None or session["split"] != activity["split"]:
            raise MeasurementError("audit capsule activity session is invalid")
        if activity["expected_lifecycle_status"] not in VALID_LIFECYCLE_STATUSES:
            raise MeasurementError("audit capsule lifecycle status is invalid")
        _bounded_int(activity["expected_epoch"], label="expected epoch", maximum=1_000)
        _bounded_int(activity["speech_end_at_ms"], label="speech end", maximum=120_000)
        _bounded_int(activity["advance_to_ms"], label="advance time", maximum=7_200_000)
    for raw_window in windows:
        window = _strict_mapping(
            raw_window,
            {"window_ordinal", "split", "condition", "wire_facts"},
            label="audit capsule no-speech window",
        )
        _bounded_int(window["window_ordinal"], label="window ordinal", maximum=63)
        _validate_split(window["split"])
        _safe_id(window["condition"], label="condition")
        _validate_wire_facts(window["wire_facts"])
        _validate_response_wire_state(window["wire_facts"])
    activity_session_ordinals = {activity["session_ordinal"] for activity in activities}
    if activity_session_ordinals != set(session_index):
        raise MeasurementError("audit capsule session population is invalid")
    expected_units = {
        *(("session", session["session_ordinal"]) for session in sessions),
        *(("no_speech_window", window["window_ordinal"]) for window in windows),
    }
    actual_units = {(unit["kind"], unit["ordinal"]) for unit in accounting["units"]}
    evidence_splits = {
        *(session["split"] for session in sessions),
        *(window["split"] for window in windows),
    }
    if (
        not expected_units
        or actual_units != expected_units
        or evidence_splits != {accounting["split"]}
    ):
        raise MeasurementError("audit capsule accounting identity is invalid")
    accounting_index = {
        (unit["kind"], unit["ordinal"]): unit for unit in accounting["units"]
    }
    for session_ordinal, session in session_index.items():
        session_activities = tuple(
            activity
            for activity in activities
            if activity["session_ordinal"] == session_ordinal
        )
        _validate_session_connection_topology(
            events=session_events[session_ordinal],
            event_activity_ordinals=session["event_activity_ordinals"],
            wire_facts=session["wire_facts"],
            activities=session_activities,
            provider_request_count=accounting_index[
                ("session", session_ordinal)
            ]["provider_request_count"],
        )
    for window in windows:
        _validate_connection_open_topology(
            window["wire_facts"],
            provider_request_count=accounting_index[
                ("no_speech_window", window["window_ordinal"])
            ]["provider_request_count"],
        )
    return json.loads(canonical_json_bytes(data))


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise MeasurementError("audit capsule timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise MeasurementError("audit capsule timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise MeasurementError("audit capsule timestamp is invalid")
    normalized = parsed.astimezone(timezone.utc)
    if value != normalized.isoformat(timespec="seconds").replace("+00:00", "Z"):
        raise MeasurementError("audit capsule timestamp is invalid")
    return normalized


def _validate_capsule_accounting(raw: object) -> dict[str, Any]:
    accounting = _strict_mapping(
        raw,
        {"schema_id", "split", "units"},
        label="audit capsule accounting",
    )
    if accounting["schema_id"] != CAPSULE_ACCOUNTING_SCHEMA_ID:
        raise MeasurementError("audit capsule accounting schema is invalid")
    split = _validate_split(accounting["split"])
    raw_units = accounting["units"]
    if not isinstance(raw_units, list) or not 1 <= len(raw_units) <= 128:
        raise MeasurementError("audit capsule accounting units are invalid")
    units = []
    identities: set[tuple[str, int]] = set()
    counter_fields = {
        "provider_request_count",
        "input_audio_tokens",
        "output_audio_tokens",
        "input_text_tokens",
        "output_text_tokens",
    }
    for raw_unit in raw_units:
        unit = _strict_mapping(
            raw_unit,
            {
                "kind",
                "ordinal",
                "metadata_complete",
                "complete",
                "error_code",
                "provider_request_count",
                "observed_elapsed_ms",
                "input_audio_duration_ms",
                "output_audio_bytes",
                "input_audio_tokens",
                "output_audio_tokens",
                "input_text_tokens",
                "output_text_tokens",
            },
            label="audit capsule accounting unit",
        )
        if unit["kind"] not in {"session", "no_speech_window"}:
            raise MeasurementError("audit capsule accounting unit kind is invalid")
        ordinal = _bounded_int(unit["ordinal"], label="accounting ordinal", maximum=63)
        identity = (unit["kind"], ordinal)
        if identity in identities:
            raise MeasurementError("audit capsule accounting unit identity is duplicated")
        identities.add(identity)
        if not isinstance(unit["metadata_complete"], bool) or not isinstance(
            unit["complete"], bool
        ):
            raise MeasurementError("audit capsule accounting completion is invalid")
        error_code = unit["error_code"]
        if unit["complete"]:
            if not unit["metadata_complete"] or error_code is not None:
                raise MeasurementError("audit capsule accounting success is inconsistent")
        elif error_code not in CAPSULE_FAILURE_CODES:
            raise MeasurementError("audit capsule accounting failure code is invalid")
        for field in counter_fields:
            _bounded_int(
                unit[field],
                label=f"accounting {field}",
                maximum=MAX_ACCOUNTING_COUNTER,
            )
        _bounded_int(
            unit["observed_elapsed_ms"],
            label="accounting observed elapsed time",
            maximum=MAX_ACCOUNTING_ELAPSED_MS,
        )
        _bounded_int(
            unit["input_audio_duration_ms"],
            label="accounting input audio duration",
            maximum=MAX_ACCOUNTING_ELAPSED_MS,
        )
        _bounded_int(
            unit["output_audio_bytes"],
            label="accounting output audio bytes",
            maximum=MAX_ACCOUNTING_OUTPUT_BYTES,
        )
        units.append(unit)
    return {"schema_id": CAPSULE_ACCOUNTING_SCHEMA_ID, "split": split, "units": units}


def _capsule_reference(activity: Mapping[str, Any]) -> ActivityReference:
    return ActivityReference(
        activity_ordinal=activity["activity_ordinal"],
        language=activity["language"],
        text=activity["reference_text"],
        critical_spans=tuple(
            CriticalSpan(
                kind=CriticalSpanKind(span["kind"]),
                text=span["text"],
                language=span["language"],
            )
            for span in activity["critical_spans"]
        ),
    )


def _capsule_wire_observation(
    facts: list[Mapping[str, Any]],
    *,
    activity_ordinal: int,
    scenario_tags: tuple[str, ...],
    speech_end_at_ms: int,
) -> tuple[WireObservation, int]:
    ordered = sorted(facts, key=lambda value: (value["at_ms"], value["sequence"]))
    starts = [
        value
        for value in ordered
        if value["kind"] == "caller_activity_start"
        and value["activity_ordinal"] == activity_ordinal
    ]
    ends = [
        value
        for value in ordered
        if value["kind"] == "caller_activity_end"
        and value["activity_ordinal"] == activity_ordinal
    ]
    speech_ends = [
        value
        for value in ordered
        if value["kind"] == "caller_speech_end"
        and value["activity_ordinal"] == activity_ordinal
    ]
    if (
        len(starts) != 1
        or len(ends) != 1
        or _wire_position(ends[0]) <= _wire_position(starts[0])
    ):
        raise MeasurementError("capsule activity boundaries are invalid")
    if (
        len(speech_ends) != 1
        or speech_ends[0]["at_ms"] != speech_end_at_ms
        or not _wire_position(starts[0])
        < _wire_position(speech_ends[0])
        <= _wire_position(ends[0])
    ):
        raise MeasurementError("capsule speech boundary is invalid")
    response_opens = [
        value
        for value in ordered
        if value["kind"] == "response_open"
        and value["activity_ordinal"] == activity_ordinal
    ]
    if len(response_opens) > 1:
        raise MeasurementError("capsule response-open facts are invalid")
    target_audio = [
        value
        for value in ordered
        if value["kind"] == "audio_received"
        and value["activity_ordinal"] == activity_ordinal
    ]
    if target_audio and not response_opens:
        raise MeasurementError("capsule audio is missing a response-open fact")
    if response_opens and target_audio and any(
        value["response_ordinal"] != response_opens[0]["response_ordinal"]
        for value in target_audio
    ):
        raise MeasurementError("capsule response attribution is inconsistent")
    first_audio_ms = min((value["at_ms"] for value in target_audio), default=None)
    if first_audio_ms is not None:
        first_audio_ms = max(0, first_audio_ms - speech_end_at_ms)
    premature_count = sum(
        _wire_position(value) < _wire_position(ends[0]) for value in target_audio
    )
    terminal_facts = [
        value for value in ordered if value["kind"] == "response_terminal"
    ]
    terminal_counts = Counter(value["response_ordinal"] for value in terminal_facts)
    if any(count != 1 for count in terminal_counts.values()):
        raise MeasurementError("capsule response-terminal facts are invalid")
    terminals = {
        value["response_ordinal"]: _wire_position(value) for value in terminal_facts
    }
    audio_after_terminal_count = sum(
        value["response_ordinal"] in terminals
        and _wire_position(value) > terminals[value["response_ordinal"]]
        for value in target_audio
    )
    response_gap_count = 0
    by_response: dict[int, list[int]] = {}
    for value in target_audio:
        by_response.setdefault(value["response_ordinal"], []).append(value["at_ms"])
    for times in by_response.values():
        response_gap_count += sum(
            later - earlier > 500 for earlier, later in zip(times, times[1:], strict=False)
        )
    response_timeout_count = sum(
        value["response_ordinal"] not in terminals for value in response_opens
    )
    interruption_tail_ms: int | None = None
    caller_audio = [
        value
        for value in ordered
        if value["kind"] == "caller_audio_sent"
        and value["activity_ordinal"] == activity_ordinal
    ]
    if caller_audio:
        trigger = caller_audio[0]
        prior_responses = [
            value
            for value in ordered
            if value["kind"] == "response_open"
            and value["activity_ordinal"] != activity_ordinal
            and value["epoch"] == trigger["epoch"]
            and (value["at_ms"], value["sequence"])
            < (trigger["at_ms"], trigger["sequence"])
            and not any(
                terminal["kind"] == "response_terminal"
                and terminal["response_ordinal"] == value["response_ordinal"]
                and terminal["epoch"] == trigger["epoch"]
                and (terminal["at_ms"], terminal["sequence"])
                < (trigger["at_ms"], trigger["sequence"])
                for terminal in ordered
            )
        ]
        if prior_responses:
            prior_response = prior_responses[-1]
            evidence_complete = True
            if "tool_cancellation_interruption" in scenario_tags:
                prior_ordinal = prior_response["activity_ordinal"]
                open_tools = [
                    value
                    for value in ordered
                    if value["kind"] == "tool_call_open"
                    and value["activity_ordinal"] == prior_ordinal
                    and value["epoch"] == trigger["epoch"]
                    and (value["at_ms"], value["sequence"])
                    < (trigger["at_ms"], trigger["sequence"])
                ]
                cancellations = [
                    value
                    for value in ordered
                    if value["kind"] == "tool_call_cancelled"
                    and value["activity_ordinal"] == activity_ordinal
                    and value["epoch"] == trigger["epoch"]
                    and (value["at_ms"], value["sequence"])
                    >= (trigger["at_ms"], trigger["sequence"])
                ]
                interruptions = [
                    value
                    for value in ordered
                    if value["kind"] == "interrupted"
                    and value["activity_ordinal"] == activity_ordinal
                    and value["epoch"] == trigger["epoch"]
                    and (value["at_ms"], value["sequence"])
                    >= (trigger["at_ms"], trigger["sequence"])
                ]
                evidence_complete = bool(
                    open_tools and cancellations and interruptions
                )
            if evidence_complete:
                tail_audio = [
                    value["at_ms"]
                    for value in ordered
                    if value["kind"] == "audio_received"
                    and value["response_ordinal"]
                    == prior_response["response_ordinal"]
                    and value["epoch"] == trigger["epoch"]
                    and (value["at_ms"], value["sequence"])
                    >= (trigger["at_ms"], trigger["sequence"])
                ]
                interruption_tail_ms = (
                    max(value - trigger["at_ms"] for value in tail_audio)
                    if tail_audio
                    else 0
                )
    applicable = [
        value
        for value in ordered
        if value["activity_ordinal"] in {None, activity_ordinal}
    ]
    abnormal_close_count = _fact_count(applicable, "abnormal_close") + _fact_count(
        applicable, "goaway"
    )
    malformed_count = _fact_count(applicable, "malformed")
    teardown_violation_count = _fact_count(applicable, "teardown_failed")
    runaway_output_count = int(
        sum(value["audio_bytes"] for value in target_audio) > MAX_ACCOUNTING_OUTPUT_BYTES
    )
    return (
        WireObservation(
            timing_covered=first_audio_ms is not None,
            first_audio_ms=first_audio_ms,
            interruption_tail_ms=interruption_tail_ms,
            premature_current_audio_count=premature_count,
            audio_after_terminal_count=audio_after_terminal_count,
            response_gap_violation_count=response_gap_count,
            abnormal_close_count=abnormal_close_count,
            runaway_output_count=runaway_output_count,
            response_timeout_count=response_timeout_count,
            malformed_count=malformed_count,
            teardown_violation_count=teardown_violation_count,
            error_code=_wire_error_code(applicable),
        ),
        ends[0]["at_ms"],
    )


def _wire_position(fact: Mapping[str, Any]) -> tuple[int, int]:
    return fact["at_ms"], fact["sequence"]


def _fact_count(facts: list[Mapping[str, Any]], kind: str) -> int:
    return sum(value["kind"] == kind for value in facts)


def _audio_after_teardown_count(facts: list[Mapping[str, Any]]) -> int:
    teardown_times = [
        value["at_ms"]
        for value in facts
        if value["kind"] in {"teardown_complete", "teardown_failed"}
    ]
    if not teardown_times:
        return 0
    return sum(
        value["kind"] == "audio_received" and value["at_ms"] > min(teardown_times)
        for value in facts
    )


def _wire_error_code(facts: list[Mapping[str, Any]]) -> str | None:
    codes = (
        ("audio_after_terminal", "audio_after_terminal"),
        ("abnormal_close", "provider_closed"),
        ("goaway", "provider_goaway"),
        ("malformed", "malformed_message"),
        ("teardown_failed", "teardown_failure"),
    )
    return next((code for kind, code in codes if _fact_count(facts, kind)), None)


def _reject_forbidden_capsule_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or key in FORBIDDEN_CAPSULE_KEYS:
                raise MeasurementError("audit capsule field is forbidden")
            _reject_forbidden_capsule_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_forbidden_capsule_keys(child)


def _validate_wire_facts(raw: object) -> None:
    if not isinstance(raw, list) or len(raw) > 10_000:
        raise MeasurementError("audit capsule wire facts are invalid")
    previous_fact_at_ms = -1
    for index, raw_fact in enumerate(raw):
        fact = _strict_mapping(
            raw_fact,
            {
                "kind",
                "at_ms",
                "response_ordinal",
                "activity_ordinal",
                "sequence",
                "epoch",
                "audio_bytes",
            },
            label="audit capsule wire fact",
        )
        if fact["kind"] not in WIRE_FACT_KINDS:
            raise MeasurementError("audit capsule wire fact kind is invalid")
        at_ms = _bounded_int(fact["at_ms"], label="wire fact time", maximum=7_200_000)
        if at_ms < previous_fact_at_ms:
            raise MeasurementError("audit capsule wire time moved backward")
        previous_fact_at_ms = at_ms
        _optional_bounded_int(
            fact["response_ordinal"],
            label="response ordinal",
            maximum=10_000,
        )
        _optional_bounded_int(
            fact["activity_ordinal"],
            label="activity ordinal",
            maximum=255,
        )
        sequence = _bounded_int(fact["sequence"], label="wire sequence", maximum=100_000)
        if sequence != index:
            raise MeasurementError("audit capsule wire fact sequence is invalid")
        epoch = _bounded_int(fact["epoch"], label="wire epoch", maximum=1_000)
        if epoch < 1:
            raise MeasurementError("audit capsule wire epoch is invalid")
        audio_bytes = _bounded_int(
            fact["audio_bytes"], label="wire audio bytes", maximum=1024 * 1024
        )
        if fact["kind"] in {"audio_received", "caller_audio_sent"}:
            if audio_bytes <= 0:
                raise MeasurementError("audio wire fact byte count is invalid")
        elif audio_bytes:
            raise MeasurementError("non-audio wire fact byte count is invalid")
        if fact["kind"] in {
            "audio_received",
            "response_open",
            "response_terminal",
        } and fact["response_ordinal"] is None:
            raise MeasurementError("response wire fact ordinal is missing")


def _validate_event_activity_ordinals(
    raw: object,
    *,
    events: tuple[CallerTurnEvent, ...],
    wire_facts: list[Mapping[str, Any]],
) -> None:
    if not isinstance(raw, list) or len(raw) != len(events):
        raise MeasurementError("session event ownership cardinality is invalid")
    sent_audio = tuple(
        fact for fact in wire_facts if fact["kind"] == "caller_audio_sent"
    )
    for event, value in zip(events, raw, strict=True):
        activity_ordinal = _bounded_int(
            value,
            label="event activity ordinal",
            maximum=255,
        )
        if event.kind is CallerTurnEventKind.RECONNECT_STARTED:
            continue
        eligible = tuple(
            fact
            for fact in sent_audio
            if fact["epoch"] == event.epoch and fact["at_ms"] <= event.at_ms
        )
        if not eligible:
            raise MeasurementError("session event lacks causal activity ownership")
        latest = max(eligible, key=lambda fact: (fact["at_ms"], fact["sequence"]))
        if latest["activity_ordinal"] != activity_ordinal:
            raise MeasurementError("session event ownership is not causal")


def _validate_session_connection_topology(
    *,
    events: tuple[CallerTurnEvent, ...],
    event_activity_ordinals: list[int],
    wire_facts: list[Mapping[str, Any]],
    activities: tuple[Mapping[str, Any], ...],
    provider_request_count: int,
) -> None:
    opens = _validate_connection_open_topology(
        wire_facts,
        provider_request_count=provider_request_count,
    )
    opened_epochs = tuple(fact["epoch"] for fact in opens)
    expected_epochs = {activity["expected_epoch"] for activity in activities}
    if expected_epochs != set(opened_epochs):
        raise MeasurementError("audit capsule session connection topology is invalid")

    activity_epochs = {
        activity["activity_ordinal"]: activity["expected_epoch"]
        for activity in activities
    }
    first_audio_by_epoch: dict[int, Mapping[str, Any]] = {}
    for fact in wire_facts:
        if fact["kind"] == "caller_audio_sent":
            first_audio_by_epoch.setdefault(fact["epoch"], fact)
    if set(first_audio_by_epoch) != set(opened_epochs):
        raise MeasurementError("audit capsule session connection topology is invalid")

    current_epoch = opened_epochs[0]
    open_by_epoch = {fact["epoch"]: fact for fact in opens}
    for event, owner in zip(events, event_activity_ordinals, strict=True):
        if event.kind is CallerTurnEventKind.RECONNECT_STARTED:
            if event.epoch != current_epoch + 1:
                raise MeasurementError(
                    "audit capsule session connection topology is invalid"
                )
            opened = open_by_epoch.get(event.epoch)
            first_audio = first_audio_by_epoch.get(event.epoch)
            if (
                opened is None
                or first_audio is None
                or event.at_ms != opened["at_ms"]
                or owner != first_audio["activity_ordinal"]
                or activity_epochs.get(owner) != event.epoch
            ):
                raise MeasurementError(
                    "audit capsule session connection topology is invalid"
                )
            current_epoch = event.epoch
        elif event.epoch != current_epoch or activity_epochs.get(owner) != event.epoch:
            raise MeasurementError("audit capsule session connection topology is invalid")
    if current_epoch != opened_epochs[-1]:
        raise MeasurementError("audit capsule session connection topology is invalid")


def _validate_connection_open_topology(
    wire_facts: list[Mapping[str, Any]],
    *,
    provider_request_count: int,
) -> tuple[Mapping[str, Any], ...]:
    opens = tuple(fact for fact in wire_facts if fact["kind"] == "connection_open")
    if len(opens) != provider_request_count or provider_request_count < 1:
        raise MeasurementError("audit capsule provider request topology is invalid")
    expected_epochs = tuple(range(1, provider_request_count + 1))
    if tuple(fact["epoch"] for fact in opens) != expected_epochs:
        raise MeasurementError("audit capsule connection-open topology is invalid")

    current_epoch = 0
    for fact in wire_facts:
        if fact["kind"] == "connection_open":
            if fact["epoch"] != current_epoch + 1:
                raise MeasurementError("audit capsule connection-open topology is invalid")
            current_epoch = fact["epoch"]
        elif fact["epoch"] != current_epoch:
            raise MeasurementError("audit capsule connection-open topology is invalid")
    if current_epoch != provider_request_count:
        raise MeasurementError("audit capsule connection-open topology is invalid")
    _validate_connection_completion_facts(wire_facts, expected_epochs=expected_epochs)
    return opens


def _validate_connection_completion_facts(
    wire_facts: list[Mapping[str, Any]],
    *,
    expected_epochs: tuple[int, ...],
) -> None:
    provider_receipt_kinds = {
        "abnormal_close",
        "audio_after_terminal",
        "audio_received",
        "false_activity",
        "goaway",
        "interrupted",
        "malformed",
        "response_open",
        "response_terminal",
        "tool_call_cancelled",
        "tool_call_open",
        "usage_received",
    }
    for epoch in expected_epochs:
        completions = [
            fact
            for fact in wire_facts
            if fact["epoch"] == epoch
            and fact["kind"] in {"stream_closed", "observation_complete"}
        ]
        if len(completions) != 1:
            raise MeasurementError("audit capsule observation endpoint is invalid")
        completion = completions[0]
        if any(
            fact is not completion
            and fact["kind"] not in {"teardown_complete", "teardown_failed"}
            and (fact["at_ms"], fact["sequence"])
            > (completion["at_ms"], completion["sequence"])
            for fact in wire_facts
            if fact["epoch"] == epoch
        ):
            raise MeasurementError("audit capsule completion endpoint is not final")
        receipts = [
            fact
            for fact in wire_facts
            if fact["epoch"] == epoch and fact["kind"] in provider_receipt_kinds
        ]
        if completion["kind"] == "stream_closed":
            continue
        if not receipts:
            raise MeasurementError("audit capsule observation endpoint is invalid")
        latest_receipt_ms = max(fact["at_ms"] for fact in receipts)
        if completion["at_ms"] - latest_receipt_ms < 3_000:
            raise MeasurementError("audit capsule observation window is incomplete")


def _validate_response_wire_state(raw: list[Mapping[str, Any]]) -> None:
    active: tuple[int, int | None, int] | None = None
    opened: set[int] = set()
    closed: set[tuple[int, int | None, int]] = set()
    latest_sent_activity: dict[int, int] = {}
    for fact in raw:
        kind = fact["kind"]
        response_ordinal = fact["response_ordinal"]
        identity = (response_ordinal, fact["activity_ordinal"], fact["epoch"])
        if kind == "caller_audio_sent" and fact["activity_ordinal"] is not None:
            latest_sent_activity[fact["epoch"]] = fact["activity_ordinal"]
        if kind == "response_open":
            if response_ordinal in opened or active is not None:
                raise MeasurementError("capsule response-open facts are invalid")
            if (
                fact["activity_ordinal"] is not None
                and latest_sent_activity.get(fact["epoch"])
                != fact["activity_ordinal"]
            ):
                raise MeasurementError("capsule response ownership is not causal")
            opened.add(response_ordinal)
            active = identity
        elif kind == "audio_received":
            if active == identity or identity in closed:
                continue
            raise MeasurementError("capsule response audio is not causally attributable")
        elif kind == "response_terminal":
            if active != identity:
                raise MeasurementError("capsule response-terminal facts are invalid")
            closed.add(identity)
            active = None


def _event_dict(event: CallerTurnEvent) -> dict[str, Any]:
    return {
        "kind": event.kind.value,
        "at_ms": event.at_ms,
        "sequence": event.sequence,
        "epoch": event.epoch,
        "text": event.text,
    }


def _strict_mapping(
    raw: object,
    fields: set[str],
    *,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise MeasurementError(f"{label} fields are invalid")
    return raw


def _validate_policy(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in POLICIES_MS:
        raise MeasurementError("quiescence policy is invalid")
    return value


def _validate_split(value: object) -> str:
    if not isinstance(value, str) or value not in VALID_SPLITS:
        raise MeasurementError("split is invalid")
    return value


def _validate_language(value: object) -> str:
    if not isinstance(value, str) or value not in VALID_LANGUAGES:
        raise MeasurementError("language is invalid")
    return value


def _safe_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise MeasurementError(f"{label} is invalid")
    return value


def _optional_safe_id(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _safe_id(value, label=label)


def _safe_id_tuple(value: object, *, label: str, maximum: int) -> tuple[str, ...]:
    if (
        not isinstance(value, tuple)
        or not 1 <= len(value) <= maximum
        or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in value)
    ):
        raise MeasurementError(f"{label} are invalid")
    if len(set(value)) != len(value):
        raise MeasurementError(f"{label} must be unique")
    return value


def _string_tuple(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise MeasurementError(f"{label} must be an array")
    return _safe_id_tuple(tuple(value), label=label, maximum=16)


def _bounded_int(value: object, *, label: str, maximum: int = MAX_COUNTER) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MeasurementError(f"{label} must be an integer")
    if not 0 <= value <= maximum:
        raise MeasurementError(f"{label} is outside its fixed bound")
    return value


def _optional_bounded_int(value: object, *, label: str, maximum: int) -> int | None:
    if value is None:
        return None
    return _bounded_int(value, label=label, maximum=maximum)


def _fraction_to_micros(numerator: int, denominator: int) -> int:
    return (numerator * 1_000_000 + denominator // 2) // denominator
