"""Independent, payload-safe measurement primitives for Gate 0B evidence."""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
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
    CriticalSpan,
    CriticalSpanKind,
    align_caller_turn_events,
    align_fragments,
    normalize_text,
    reconstruct_fragments,
)
from app.services.caller_turns import (
    CallerTurnAssembler,
    CallerTurnEvent,
    CallerTurnEventKind,
)
from app.services.qualification_identity import canonical_json_bytes


ACTIVITY_PRIMITIVE_SCHEMA_ID = "gate_0b_activity_primitive_v1"
NO_SPEECH_PRIMITIVE_SCHEMA_ID = "gate_0b_no_speech_primitive_v1"
AUDIT_CAPSULE_SCHEMA_ID = "gate_0b_audit_capsule_v1"
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
MAX_COUNTER = 100_000
MAX_TEXT_LENGTH = 16_000
MAX_CAPSULE_BYTES = 16 * 1024 * 1024
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
        "audio_received",
        "caller_activity_end",
        "caller_activity_start",
        "cancelled",
        "false_activity",
        "interrupted",
        "response_gap",
        "response_open",
        "response_terminal",
        "response_timeout",
        "runaway_output",
        "teardown",
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
    reconstructed = reconstruct_fragments(fragments, policy=alignment_policy)
    normalized_reference = normalize_text(expected_reference.text, measurement.language)
    normalized_hypothesis = normalize_text(reconstructed, measurement.language)

    assembler = CallerTurnAssembler(
        active_epoch=measurement.expected_epoch,
        quiescence_ms=measurement.policy_ms,
    )
    turns = []
    late_fragment_count = 0
    for event in measurement.events:
        emitted = assembler.ingest(event)
        if emitted and event.kind is CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT:
            late_fragment_count += 1
        turns.extend(emitted)
    turns.extend(assembler.advance_time(measurement.advance_to_ms))
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
        contamination_count=int(
            alignment.activity_ordinal is not None
            and alignment.activity_ordinal != measurement.activity_ordinal
        ),
        duplicate_count=max(0, len(turns) - 1),
        cross_epoch_acceptance_count=sum(
            turn.epoch != measurement.expected_epoch for turn in turns
        ),
        late_fragment_mutation_count=late_fragment_count,
        stale_count=assembler.stale_event_count,
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
            "activities",
            "no_speech_windows",
        },
        label="audit capsule",
    )
    if data["schema_id"] != AUDIT_CAPSULE_SCHEMA_ID:
        raise MeasurementError("audit capsule schema is invalid")
    _safe_id(data["campaign_id"], label="campaign ID")
    _validate_policy(data["policy_ms"])
    activities = data["activities"]
    windows = data["no_speech_windows"]
    if not isinstance(activities, list) or len(activities) > 256:
        raise MeasurementError("audit capsule activities are invalid")
    if not isinstance(windows, list) or len(windows) > 64:
        raise MeasurementError("audit capsule no-speech windows are invalid")
    for raw_activity in activities:
        activity = _strict_mapping(
            raw_activity,
            {
                "activity_ordinal",
                "split",
                "language",
                "condition",
                "scenario_tags",
                "reference_text",
                "critical_spans",
                "events",
                "expected_lifecycle_status",
                "expected_epoch",
                "advance_to_ms",
                "wire_facts",
            },
            label="audit capsule activity",
        )
        ordinal = _bounded_int(
            activity["activity_ordinal"],
            label="activity ordinal",
            maximum=255,
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
        events = activity["events"]
        if not isinstance(events, list) or len(events) > 10_000:
            raise MeasurementError("audit capsule events are invalid")
        for event in events:
            CallerTurnEvent.from_dict(event)
        if activity["expected_lifecycle_status"] not in VALID_LIFECYCLE_STATUSES:
            raise MeasurementError("audit capsule lifecycle status is invalid")
        _bounded_int(activity["expected_epoch"], label="expected epoch", maximum=1_000)
        _bounded_int(activity["advance_to_ms"], label="advance time", maximum=7_200_000)
        _validate_wire_facts(activity["wire_facts"])
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
    return json.loads(canonical_json_bytes(data))


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
    for raw_fact in raw:
        fact = _strict_mapping(
            raw_fact,
            {"kind", "at_ms", "response_ordinal", "activity_ordinal"},
            label="audit capsule wire fact",
        )
        if fact["kind"] not in WIRE_FACT_KINDS:
            raise MeasurementError("audit capsule wire fact kind is invalid")
        _bounded_int(fact["at_ms"], label="wire fact time", maximum=7_200_000)
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
