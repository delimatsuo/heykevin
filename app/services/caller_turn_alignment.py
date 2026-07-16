"""Pure multilingual attribution and transcription-fidelity primitives."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
import unicodedata

from app.services.caller_turns import CallerTurnEvent, CallerTurnEventKind


ALIGNMENT_SCHEMA_ID = "gate_0b_alignment_v2"
SUPPORTED_LANGUAGES = frozenset({"ar", "en", "es", "fr", "hi", "ht", "pt", "zh"})
MAX_FRAGMENT_COUNT = 128
MAX_TEXT_CODEPOINTS = 4_000
MAX_TEXT_UTF8_BYTES = 16_000
MAX_REFERENCE_COUNT = 10
MAX_CRITICAL_SPANS = 16
MAX_CRITICAL_SPAN_CODEPOINTS = 512
MAX_EDIT_MATRIX_CELLS = 1_000_000


class FragmentMode(str, Enum):
    DELTA = "delta"
    CUMULATIVE = "cumulative"


class AlignmentStatus(str, Enum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    UNASSIGNED = "unassigned"


class CriticalSpanKind(str, Enum):
    DIGITS = "digits"
    NEGATION = "negation"
    CORRECTION = "correction"
    IDENTITY_CONFUSABLE = "identity_confusable"
    ENGLISH_TO_LANGUAGE = "english_to_language"
    LANGUAGE_TO_ENGLISH = "language_to_english"


@dataclass(frozen=True, slots=True)
class NormalizedText:
    text: str
    characters: tuple[str, ...]
    words: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class EditCounts:
    insertions: int = 0
    deletions: int = 0
    substitutions: int = 0

    @property
    def distance(self) -> int:
        return self.insertions + self.deletions + self.substitutions

    def with_insertion(self) -> "EditCounts":
        return EditCounts(self.insertions + 1, self.deletions, self.substitutions)

    def with_deletion(self) -> "EditCounts":
        return EditCounts(self.insertions, self.deletions + 1, self.substitutions)

    def with_substitution(self) -> "EditCounts":
        return EditCounts(self.insertions, self.deletions, self.substitutions + 1)


@dataclass(frozen=True, slots=True)
class CriticalSpan:
    kind: CriticalSpanKind
    text: str
    language: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CriticalSpanKind):
            raise TypeError("critical span kind is invalid")
        _validate_source_text(
            self.text,
            label="critical span",
            max_codepoints=MAX_CRITICAL_SPAN_CODEPOINTS,
            max_utf8_bytes=MAX_CRITICAL_SPAN_CODEPOINTS * 4,
        )
        if self.language is not None:
            _validate_language(self.language)


@dataclass(frozen=True, slots=True)
class ActivityReference:
    activity_ordinal: int
    language: str
    text: str
    critical_spans: tuple[CriticalSpan, ...] = ()

    def __post_init__(self) -> None:
        _validate_ordinal(self.activity_ordinal)
        _validate_language(self.language)
        _validate_source_text(
            self.text,
            label="reference text",
            max_codepoints=MAX_TEXT_CODEPOINTS,
            max_utf8_bytes=MAX_TEXT_UTF8_BYTES,
        )
        normalized_reference = normalize_text(self.text, self.language)
        if not normalized_reference.characters:
            raise ValueError("reference text must contain normalized characters")
        if (
            not isinstance(self.critical_spans, tuple)
            or len(self.critical_spans) > MAX_CRITICAL_SPANS
            or any(not isinstance(span, CriticalSpan) for span in self.critical_spans)
        ):
            raise ValueError("critical spans violate the fixed bound")
        for span in self.critical_spans:
            normalized_span = normalize_text(span.text, span.language or self.language)
            if not normalized_span.characters:
                raise ValueError("critical span must contain normalized characters")
            reference_units, span_units = _comparable_alignment_units(
                normalized_reference,
                normalized_span,
            )
            if _count_sequence_occurrences(
                reference_units,
                span_units,
            ) != 1:
                raise ValueError("critical span must map uniquely within its reference")


@dataclass(frozen=True, slots=True)
class AlignmentPolicy:
    fragment_mode: FragmentMode
    schema_id: str = ALIGNMENT_SCHEMA_ID
    max_assignment_cer: Fraction = Fraction(1, 3)
    max_fidelity_cer: Fraction = Fraction(1, 10)
    max_fidelity_wer: Fraction = Fraction(3, 20)
    min_ambiguity_margin: Fraction = Fraction(1, 20)
    max_fragments: int = MAX_FRAGMENT_COUNT
    max_text_codepoints: int = MAX_TEXT_CODEPOINTS
    max_text_utf8_bytes: int = MAX_TEXT_UTF8_BYTES
    max_references: int = MAX_REFERENCE_COUNT

    def __post_init__(self) -> None:
        if not isinstance(self.fragment_mode, FragmentMode):
            raise TypeError("fragment mode is invalid")
        if self.schema_id != ALIGNMENT_SCHEMA_ID:
            raise ValueError("alignment schema is not supported")
        for label, value in (
            ("assignment CER", self.max_assignment_cer),
            ("fidelity CER", self.max_fidelity_cer),
            ("fidelity WER", self.max_fidelity_wer),
            ("ambiguity margin", self.min_ambiguity_margin),
        ):
            if not isinstance(value, Fraction):
                raise TypeError(f"{label} must be an exact fraction")
            if not 0 <= value <= 1:
                raise ValueError(f"{label} is outside its fixed bound")
        if self.max_fidelity_cer > self.max_assignment_cer:
            raise ValueError("fidelity CER cannot exceed assignment CER")
        _validate_limit(self.max_fragments, maximum=MAX_FRAGMENT_COUNT, label="fragment")
        _validate_limit(
            self.max_text_codepoints,
            maximum=MAX_TEXT_CODEPOINTS,
            label="codepoint",
        )
        _validate_limit(
            self.max_text_utf8_bytes,
            maximum=MAX_TEXT_UTF8_BYTES,
            label="UTF-8 byte",
        )
        _validate_limit(
            self.max_references,
            maximum=MAX_REFERENCE_COUNT,
            label="reference",
        )


@dataclass(frozen=True, slots=True)
class CriticalSpanOutcome:
    span_ordinal: int
    kind: CriticalSpanKind
    exact: bool


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    schema_id: str
    status: AlignmentStatus
    activity_ordinal: int | None
    nearest_activity_ordinal: int
    character_edits: EditCounts
    cer: Fraction
    word_edits: EditCounts | None
    wer: Fraction | None
    ambiguity_margin: Fraction | None
    critical_spans: tuple[CriticalSpanOutcome, ...]
    fidelity_passed: bool
    eligible: bool


@dataclass(frozen=True, slots=True)
class _Candidate:
    reference: ActivityReference
    character_edits: EditCounts
    cer: Fraction
    word_edits: EditCounts | None
    wer: Fraction | None
    critical_spans: tuple[CriticalSpanOutcome, ...]
    fidelity_passed: bool


@dataclass(frozen=True, slots=True)
class _CriticalSpanInterval:
    outcome_mask: int
    reference_start: int
    hypothesis_start: int
    length: int


def normalize_text(text: str, language: str) -> NormalizedText:
    """Normalize one bounded string with fixed Unicode and segmentation rules."""
    _validate_language(language)
    normalized = _validate_source_text(
        text,
        label="text",
        max_codepoints=MAX_TEXT_CODEPOINTS,
        max_utf8_bytes=MAX_TEXT_UTF8_BYTES,
    ).casefold()
    if (
        len(normalized) > MAX_TEXT_CODEPOINTS
        or len(normalized.encode("utf-8")) > MAX_TEXT_UTF8_BYTES
    ):
        raise ValueError("text exceeds the text bound after case folding")

    output: list[str] = []
    for character in normalized:
        if language == "ar" and _is_arabic_formatting_mark(character):
            continue
        try:
            output.append(str(unicodedata.decimal(character)))
            continue
        except (TypeError, ValueError):
            pass
        category = unicodedata.category(character)
        if character.isspace() or category.startswith(("P", "S")):
            output.append(" ")
        else:
            output.append(character)

    collapsed = " ".join("".join(output).split())
    if language == "zh":
        collapsed = collapsed.replace(" ", "")
        words = None
    else:
        words = tuple(collapsed.split())
    characters = tuple(character for character in collapsed if not character.isspace())
    return NormalizedText(text=collapsed, characters=characters, words=words)


def reconstruct_fragments(
    fragments: tuple[str, ...],
    *,
    policy: AlignmentPolicy,
) -> str:
    """Reconstruct provider fragments under one explicit immutable policy."""
    if not isinstance(policy, AlignmentPolicy):
        raise TypeError("policy must be an AlignmentPolicy")
    if not isinstance(fragments, tuple):
        raise TypeError("fragments must be a tuple")
    if len(fragments) > policy.max_fragments:
        raise ValueError("fragment count exceeds the fixed bound")

    normalized_fragments = tuple(
        _validate_source_text(
            fragment,
            label="fragment",
            max_codepoints=policy.max_text_codepoints,
            max_utf8_bytes=policy.max_text_utf8_bytes,
        )
        for fragment in fragments
    )
    if policy.fragment_mode is FragmentMode.DELTA:
        reconstructed = "".join(normalized_fragments)
    else:
        previous = ""
        for fragment in normalized_fragments:
            if not fragment.startswith(previous):
                raise ValueError("cumulative fragment does not extend its predecessor")
            previous = fragment
        reconstructed = previous

    _validate_source_text(
        reconstructed,
        label="reconstructed text",
        max_codepoints=policy.max_text_codepoints,
        max_utf8_bytes=policy.max_text_utf8_bytes,
    )
    return reconstructed


def compute_edit_counts(
    reference: tuple[str, ...],
    hypothesis: tuple[str, ...],
) -> EditCounts:
    """Return deterministic Levenshtein operations with fixed tie ordering."""
    for label, sequence in (("reference", reference), ("hypothesis", hypothesis)):
        if not isinstance(sequence, tuple):
            raise TypeError(f"{label} sequence must be a tuple")
        if len(sequence) > MAX_TEXT_CODEPOINTS:
            raise ValueError(f"{label} sequence exceeds the edit bound")
        if any(not isinstance(token, str) or not token for token in sequence):
            raise ValueError(f"{label} sequence contains an invalid token")

    prefix_length = 0
    common_length = min(len(reference), len(hypothesis))
    while (
        prefix_length < common_length
        and reference[prefix_length] == hypothesis[prefix_length]
    ):
        prefix_length += 1
    reference_end = len(reference)
    hypothesis_end = len(hypothesis)
    while (
        reference_end > prefix_length
        and hypothesis_end > prefix_length
        and reference[reference_end - 1] == hypothesis[hypothesis_end - 1]
    ):
        reference_end -= 1
        hypothesis_end -= 1
    reference = reference[prefix_length:reference_end]
    hypothesis = hypothesis[prefix_length:hypothesis_end]
    if (len(reference) + 1) * (len(hypothesis) + 1) > MAX_EDIT_MATRIX_CELLS:
        raise ValueError("edit matrix exceeds the fixed resource bound")

    previous = [EditCounts(insertions=index) for index in range(len(hypothesis) + 1)]
    for reference_index, reference_token in enumerate(reference, start=1):
        current = [EditCounts(deletions=reference_index)]
        for hypothesis_index, hypothesis_token in enumerate(hypothesis, start=1):
            if reference_token == hypothesis_token:
                current.append(previous[hypothesis_index - 1])
                continue
            candidates = (
                previous[hypothesis_index - 1].with_substitution(),
                previous[hypothesis_index].with_deletion(),
                current[hypothesis_index - 1].with_insertion(),
            )
            current.append(min(candidates, key=lambda counts: counts.distance))
        previous = current
    return previous[-1]


def align_fragments(
    fragments: tuple[str, ...],
    *,
    references: tuple[ActivityReference, ...],
    policy: AlignmentPolicy,
) -> AlignmentResult:
    """Assign reconstructed fragments to one reference and score fidelity."""
    if not isinstance(policy, AlignmentPolicy):
        raise TypeError("policy must be an AlignmentPolicy")
    if not isinstance(references, tuple):
        raise TypeError("references must be a tuple")
    if not 1 <= len(references) <= policy.max_references:
        raise ValueError("reference count exceeds the fixed bound")
    if any(not isinstance(reference, ActivityReference) for reference in references):
        raise TypeError("references must contain ActivityReference values")
    ordinals = [reference.activity_ordinal for reference in references]
    if len(ordinals) != len(set(ordinals)):
        raise ValueError("reference activity ordinals must be unique")

    reconstructed = reconstruct_fragments(fragments, policy=policy)
    candidates = tuple(
        _score_candidate(reconstructed, reference, policy=policy)
        for reference in references
    )
    ranked = sorted(
        candidates,
        key=lambda candidate: (candidate.cer, candidate.reference.activity_ordinal),
    )
    best = ranked[0]
    margin = ranked[1].cer - best.cer if len(ranked) > 1 else None

    if best.cer > policy.max_assignment_cer:
        status = AlignmentStatus.UNASSIGNED
    elif margin is not None and margin < policy.min_ambiguity_margin:
        status = AlignmentStatus.AMBIGUOUS
    else:
        status = AlignmentStatus.MATCHED
    activity_ordinal = (
        best.reference.activity_ordinal if status is AlignmentStatus.MATCHED else None
    )
    eligible = status is AlignmentStatus.MATCHED and best.fidelity_passed
    return AlignmentResult(
        schema_id=ALIGNMENT_SCHEMA_ID,
        status=status,
        activity_ordinal=activity_ordinal,
        nearest_activity_ordinal=best.reference.activity_ordinal,
        character_edits=best.character_edits,
        cer=best.cer,
        word_edits=best.word_edits,
        wer=best.wer,
        ambiguity_margin=margin,
        critical_spans=best.critical_spans,
        fidelity_passed=best.fidelity_passed,
        eligible=eligible,
    )


def align_caller_turn_events(
    events: tuple[CallerTurnEvent, ...],
    *,
    references: tuple[ActivityReference, ...],
    policy: AlignmentPolicy,
) -> AlignmentResult:
    """Score a receipt-ordered event tuple without mutating assembler input."""
    if not isinstance(events, tuple):
        raise TypeError("events must be a tuple")
    if any(not isinstance(event, CallerTurnEvent) for event in events):
        raise TypeError("events must contain CallerTurnEvent values")
    fragments = tuple(
        event.text
        for event in events
        if event.kind is CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT
    )
    return align_fragments(fragments, references=references, policy=policy)


def _score_candidate(
    transcript: str,
    reference: ActivityReference,
    *,
    policy: AlignmentPolicy,
) -> _Candidate:
    normalized_reference = normalize_text(reference.text, reference.language)
    normalized_transcript = normalize_text(transcript, reference.language)
    character_edits = compute_edit_counts(
        normalized_reference.characters,
        normalized_transcript.characters,
    )
    cer = Fraction(character_edits.distance, len(normalized_reference.characters))

    if normalized_reference.words is None or normalized_transcript.words is None:
        word_edits = None
        wer = None
    else:
        word_edits = compute_edit_counts(
            normalized_reference.words,
            normalized_transcript.words,
        )
        wer = Fraction(word_edits.distance, len(normalized_reference.words))
    normalized_spans = tuple(
        normalize_text(span.text, span.language or reference.language)
        for span in reference.critical_spans
    )
    critical_exactness = _critical_span_exactness(
        normalized_reference,
        normalized_transcript,
        normalized_spans,
        character_distance=character_edits.distance,
        word_distance=None if word_edits is None else word_edits.distance,
    )
    critical_outcomes = tuple(
        CriticalSpanOutcome(
            span_ordinal=index,
            kind=span.kind,
            exact=critical_exactness[index],
        )
        for index, span in enumerate(reference.critical_spans)
    )
    fidelity_passed = (
        cer <= policy.max_fidelity_cer
        and (wer is None or wer <= policy.max_fidelity_wer)
        and all(outcome.exact for outcome in critical_outcomes)
    )
    return _Candidate(
        reference=reference,
        character_edits=character_edits,
        cer=cer,
        word_edits=word_edits,
        wer=wer,
        critical_spans=critical_outcomes,
        fidelity_passed=fidelity_passed,
    )


def _critical_span_exactness(
    reference: NormalizedText,
    hypothesis: NormalizedText,
    spans: tuple[NormalizedText, ...],
    *,
    character_distance: int,
    word_distance: int | None,
) -> tuple[bool, ...]:
    word_intervals: list[_CriticalSpanInterval] = []
    character_intervals: list[_CriticalSpanInterval] = []
    exactness = [False] * len(spans)

    for index, span in enumerate(spans):
        reference_units, hypothesis_units, span_units = _comparable_alignment_units(
            reference,
            hypothesis,
            span,
        )
        reference_positions = _sequence_occurrence_indices(reference_units, span_units)
        hypothesis_positions = _sequence_occurrence_indices(hypothesis_units, span_units)
        if len(reference_positions) != 1 or len(hypothesis_positions) != 1:
            continue
        interval = _CriticalSpanInterval(
            outcome_mask=1 << index,
            reference_start=reference_positions[0],
            hypothesis_start=hypothesis_positions[0],
            length=len(span_units),
        )
        if _uses_word_alignment(reference, hypothesis, span):
            word_intervals.append(interval)
        else:
            character_intervals.append(interval)

    preserved_mask = 0
    if word_intervals:
        if word_distance is None or reference.words is None or hypothesis.words is None:
            raise RuntimeError("word alignment distance is unavailable")
        preserved_mask |= _preserved_interval_mask(
            reference.words,
            hypothesis.words,
            tuple(word_intervals),
            total_distance=word_distance,
        )
    if character_intervals:
        preserved_mask |= _preserved_interval_mask(
            reference.characters,
            hypothesis.characters,
            tuple(character_intervals),
            total_distance=character_distance,
        )
    for index in range(len(exactness)):
        exactness[index] = bool(preserved_mask & (1 << index))
    return tuple(exactness)


def _preserved_interval_mask(
    reference: tuple[str, ...],
    hypothesis: tuple[str, ...],
    intervals: tuple[_CriticalSpanInterval, ...],
    *,
    total_distance: int,
) -> int:
    all_intervals_mask = sum(interval.outcome_mask for interval in intervals)
    hypothesis_length = len(hypothesis)
    unreachable = total_distance + 1
    previous_costs = [unreachable] * (hypothesis_length + 1)
    previous_masks = [0] * (hypothesis_length + 1)
    previous_costs[0] = 0
    previous_masks[0] = all_intervals_mask

    for hypothesis_index in range(1, min(hypothesis_length, total_distance) + 1):
        previous_costs[hypothesis_index] = hypothesis_index
        previous_masks[hypothesis_index] = _mask_after_alignment_edge(
            previous_masks[hypothesis_index - 1],
            intervals,
            source=(0, hypothesis_index - 1),
            destination=(0, hypothesis_index),
        )

    for reference_index in range(1, len(reference) + 1):
        current_costs = [unreachable] * (hypothesis_length + 1)
        current_masks = [0] * (hypothesis_length + 1)
        if reference_index <= total_distance:
            current_costs[0] = reference_index
            current_masks[0] = _mask_after_alignment_edge(
                previous_masks[0],
                intervals,
                source=(reference_index - 1, 0),
                destination=(reference_index, 0),
            )

        band_start = max(1, reference_index - total_distance)
        band_end = min(hypothesis_length, reference_index + total_distance)
        for hypothesis_index in range(band_start, band_end + 1):
            candidates = (
                (
                    previous_costs[hypothesis_index - 1]
                    + (reference[reference_index - 1] != hypothesis[hypothesis_index - 1]),
                    previous_masks[hypothesis_index - 1],
                    (reference_index - 1, hypothesis_index - 1),
                ),
                (
                    previous_costs[hypothesis_index] + 1,
                    previous_masks[hypothesis_index],
                    (reference_index - 1, hypothesis_index),
                ),
                (
                    current_costs[hypothesis_index - 1] + 1,
                    current_masks[hypothesis_index - 1],
                    (reference_index, hypothesis_index - 1),
                ),
            )
            best_cost = min(cost for cost, _, _ in candidates)
            if best_cost > total_distance:
                continue
            best_masks = tuple(
                _mask_after_alignment_edge(
                    mask,
                    intervals,
                    source=source,
                    destination=(reference_index, hypothesis_index),
                )
                for cost, mask, source in candidates
                if cost == best_cost
            )
            preserved_mask = best_masks[0]
            for candidate_mask in best_masks[1:]:
                preserved_mask &= candidate_mask
            current_costs[hypothesis_index] = best_cost
            current_masks[hypothesis_index] = preserved_mask
        previous_costs = current_costs
        previous_masks = current_masks

    if previous_costs[hypothesis_length] != total_distance:
        raise RuntimeError("critical-span alignment distance is inconsistent")
    return previous_masks[hypothesis_length]


def _mask_after_alignment_edge(
    preserved_mask: int,
    intervals: tuple[_CriticalSpanInterval, ...],
    *,
    source: tuple[int, int],
    destination: tuple[int, int],
) -> int:
    for interval in intervals:
        if preserved_mask & interval.outcome_mask and not _edge_preserves_interval(
            interval,
            source=source,
            destination=destination,
        ):
            preserved_mask &= ~interval.outcome_mask
    return preserved_mask


def _edge_preserves_interval(
    interval: _CriticalSpanInterval,
    *,
    source: tuple[int, int],
    destination: tuple[int, int],
) -> bool:
    reference_index, hypothesis_index = source
    destination_reference, destination_hypothesis = destination
    reference_end = interval.reference_start + interval.length
    hypothesis_end = interval.hypothesis_start + interval.length

    if (
        reference_index <= interval.reference_start
        and hypothesis_index <= interval.hypothesis_start
    ):
        if (
            destination_reference <= interval.reference_start
            and destination_hypothesis <= interval.hypothesis_start
        ):
            return True
        return (
            reference_index == interval.reference_start
            and hypothesis_index == interval.hypothesis_start
            and destination_reference == reference_index + 1
            and destination_hypothesis == hypothesis_index + 1
        )

    progress = reference_index - interval.reference_start
    if 0 < progress < interval.length and hypothesis_index == interval.hypothesis_start + progress:
        return (
            destination_reference == reference_index + 1
            and destination_hypothesis == hypothesis_index + 1
        )
    return reference_index >= reference_end and hypothesis_index >= hypothesis_end


def _comparable_alignment_units(
    *values: NormalizedText,
) -> tuple[tuple[str, ...], ...]:
    if _uses_word_alignment(*values):
        return tuple(value.words for value in values)  # type: ignore[return-value]
    return tuple(value.characters for value in values)


def _uses_word_alignment(*values: NormalizedText) -> bool:
    return all(value.words is not None for value in values)


def _sequence_occurrence_indices(
    container: tuple[str, ...],
    target: tuple[str, ...],
) -> tuple[int, ...]:
    if not target:
        return ()
    return tuple(
        index
        for index in range(len(container) - len(target) + 1)
        if container[index : index + len(target)] == target
    )


def _validate_language(language: object) -> str:
    if not isinstance(language, str) or language not in SUPPORTED_LANGUAGES:
        raise ValueError("language is not a supported language")
    return language


def _validate_ordinal(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("activity ordinal must be an integer")
    if not 0 <= value <= 10_000:
        raise ValueError("activity ordinal is outside its fixed bound")
    return value


def _validate_limit(value: object, *, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} limit must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"{label} limit is outside its fixed bound")
    return value


def _validate_source_text(
    text: object,
    *,
    label: str,
    max_codepoints: int,
    max_utf8_bytes: int,
) -> str:
    if not isinstance(text, str):
        raise TypeError(f"{label} must be a string")
    normalized = unicodedata.normalize("NFKC", text)
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError(f"{label} contains a prohibited Unicode character")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid Unicode") from exc
    if len(normalized) > max_codepoints or len(encoded) > max_utf8_bytes:
        raise ValueError(f"{label} exceeds the text bound")
    return normalized


def _is_arabic_formatting_mark(character: str) -> bool:
    codepoint = ord(character)
    return character == "ـ" or (
        0x0610 <= codepoint <= 0x061A
        or 0x064B <= codepoint <= 0x065F
        or codepoint == 0x0670
        or 0x06D6 <= codepoint <= 0x06ED
    )


def _contains_sequence(container: tuple[str, ...], target: tuple[str, ...]) -> bool:
    return _count_sequence_occurrences(container, target) > 0


def _count_sequence_occurrences(
    container: tuple[str, ...],
    target: tuple[str, ...],
) -> int:
    if not target or len(target) > len(container):
        return 0
    return sum(
        container[index : index + len(target)] == target
        for index in range(len(container) - len(target) + 1)
    )
