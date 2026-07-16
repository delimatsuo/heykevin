"""Deterministic multilingual attribution and fidelity tests for Gate 0B."""

import ast
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path

import pytest

from app.services.caller_turn_alignment import (
    ActivityReference,
    AlignmentPolicy,
    AlignmentStatus,
    CriticalSpan,
    CriticalSpanKind,
    FragmentMode,
    align_caller_turn_events,
    align_fragments,
    compute_edit_counts,
    normalize_text,
    reconstruct_fragments,
)
from app.services.caller_turns import (
    CallerTurnAssembler,
    CallerTurnEvent,
    CallerTurnEventKind,
)


def _policy(
    *,
    mode: FragmentMode = FragmentMode.DELTA,
    assignment_cer: Fraction = Fraction(1, 3),
    fidelity_cer: Fraction = Fraction(1, 10),
    ambiguity_margin: Fraction = Fraction(1, 20),
) -> AlignmentPolicy:
    return AlignmentPolicy(
        fragment_mode=mode,
        max_assignment_cer=assignment_cer,
        max_fidelity_cer=fidelity_cer,
        max_fidelity_wer=Fraction(3, 20),
        min_ambiguity_margin=ambiguity_margin,
    )


def test_normalization_is_unicode_aware_and_language_specific() -> None:
    portuguese = normalize_text("  CAFÉ,\u00a0Ｎº １２! ", "pt")
    arabic = normalize_text("الـسَّلام، عَلَيْكُمْ", "ar")
    chinese = normalize_text("你好， 世界！", "zh")

    assert portuguese.text == "café no 12"
    assert portuguese.characters == tuple("caféno12")
    assert portuguese.words == ("café", "no", "12")
    assert arabic.text == "السلام عليكم"
    assert arabic.words == ("السلام", "عليكم")
    assert chinese.text == "你好世界"
    assert chinese.characters == tuple("你好世界")
    assert chinese.words is None


def test_normalization_preserves_identity_like_confusables() -> None:
    latin = normalize_text("code O0", "en")
    greek = normalize_text("code Ο0", "en")

    assert latin.characters != greek.characters


def test_fragment_reconstruction_requires_explicit_delta_or_cumulative_semantics() -> None:
    assert reconstruct_fragments(("Bom", " dia"), policy=_policy()) == "Bom dia"
    assert reconstruct_fragments(
        ("Bom", "Bom dia"),
        policy=_policy(mode=FragmentMode.CUMULATIVE),
    ) == "Bom dia"

    with pytest.raises(ValueError, match="cumulative fragment"):
        reconstruct_fragments(
            ("Bom dia", "Boa tarde"),
            policy=_policy(mode=FragmentMode.CUMULATIVE),
        )


def test_edit_counts_are_deterministic_for_cer_and_wer_sequences() -> None:
    character_edits = compute_edit_counts(tuple("kitten"), tuple("sitting"))
    word_edits = compute_edit_counts(
        ("book", "service", "today"),
        ("book", "a", "service", "tomorrow"),
    )

    assert character_edits.substitutions == 2
    assert character_edits.insertions == 1
    assert character_edits.deletions == 0
    assert character_edits.distance == 3
    assert word_edits.substitutions == 1
    assert word_edits.insertions == 1
    assert word_edits.deletions == 0


def test_unique_reference_maps_with_separate_cer_and_wer() -> None:
    references = (
        ActivityReference(1, "en", "book an inspection Tuesday"),
        ActivityReference(2, "en", "cancel my appointment Friday"),
    )

    result = align_fragments(
        ("Book an inspection Tuesday.",),
        references=references,
        policy=_policy(),
    )

    assert result.status is AlignmentStatus.MATCHED
    assert result.activity_ordinal == 1
    assert result.nearest_activity_ordinal == 1
    assert result.cer == 0
    assert result.wer == 0
    assert result.fidelity_passed is True
    assert result.eligible is True


def test_mapping_rejects_ambiguous_and_below_threshold_candidates() -> None:
    ambiguous_references = (
        ActivityReference(1, "en", "book service Monday"),
        ActivityReference(2, "en", "book service Tuesday"),
    )
    ambiguous = align_fragments(
        ("book service",),
        references=ambiguous_references,
        policy=_policy(
            assignment_cer=Fraction(1, 2),
            ambiguity_margin=Fraction(1, 4),
        ),
    )
    unrelated = align_fragments(
        ("completely unrelated words",),
        references=(ActivityReference(3, "en", "schedule furnace repair"),),
        policy=_policy(assignment_cer=Fraction(1, 10)),
    )

    assert ambiguous.status is AlignmentStatus.AMBIGUOUS
    assert ambiguous.activity_ordinal is None
    assert ambiguous.eligible is False
    assert ambiguous.ambiguity_margin is not None
    assert ambiguous.ambiguity_margin < Fraction(1, 4)
    assert unrelated.status is AlignmentStatus.UNASSIGNED
    assert unrelated.activity_ordinal is None
    assert unrelated.nearest_activity_ordinal == 3
    assert unrelated.eligible is False


def test_corrupted_but_nearest_transcript_fails_critical_span_fidelity() -> None:
    reference = ActivityReference(
        7,
        "en",
        "code O0 not 1235 correction 1236",
        critical_spans=(
            CriticalSpan(CriticalSpanKind.IDENTITY_CONFUSABLE, "code O0"),
            CriticalSpan(CriticalSpanKind.NEGATION, "not"),
            CriticalSpan(CriticalSpanKind.DIGITS, "1235"),
            CriticalSpan(CriticalSpanKind.CORRECTION, "correction 1236"),
        ),
    )

    result = align_fragments(
        ("code 00 not 1238 correction 1236",),
        references=(reference,),
        policy=_policy(),
    )

    assert result.status is AlignmentStatus.MATCHED
    assert result.activity_ordinal == 7
    assert result.cer <= Fraction(1, 10)
    assert result.fidelity_passed is False
    assert result.eligible is False
    assert {outcome.kind for outcome in result.critical_spans if not outcome.exact} == {
        CriticalSpanKind.IDENTITY_CONFUSABLE,
        CriticalSpanKind.DIGITS,
    }


@pytest.mark.parametrize(
    "candidate",
    (
        (
            "please confirm with the customer that the scheduled technician not does "
            "need emergency access to the locked equipment room during the routine "
            "maintenance visit tomorrow morning"
        ),
        (
            "please confirm with the customer that the scheduled technician does need "
            "not emergency access to the locked equipment room during the routine "
            "maintenance visit tomorrow morning"
        ),
    ),
)
def test_adjacent_relocated_critical_span_does_not_pass_as_exact(candidate: str) -> None:
    reference = ActivityReference(
        8,
        "en",
        (
            "please confirm with the customer that the scheduled technician does not "
            "need emergency access to the locked equipment room during the routine "
            "maintenance visit tomorrow morning"
        ),
        critical_spans=(CriticalSpan(CriticalSpanKind.NEGATION, "not"),),
    )

    result = align_fragments((candidate,), references=(reference,), policy=_policy())

    assert result.cer <= Fraction(1, 10)
    assert result.wer is not None and result.wer <= Fraction(3, 20)
    assert result.critical_spans[0].exact is False
    assert result.fidelity_passed is False


def test_moved_critical_span_does_not_pass_as_exact() -> None:
    reference = ActivityReference(
        8,
        "en",
        (
            "please confirm with the customer that the scheduled technician does not "
            "need emergency access to the locked equipment room during the routine "
            "maintenance visit tomorrow morning"
        ),
        critical_spans=(CriticalSpan(CriticalSpanKind.NEGATION, "not"),),
    )

    result = align_fragments(
        (
            "not please confirm with the customer that the scheduled technician does "
            "need emergency access to the locked equipment room during the routine "
            "maintenance visit tomorrow morning",
        ),
        references=(reference,),
        policy=_policy(),
    )

    assert result.cer <= Fraction(1, 10)
    assert result.wer is not None and result.wer <= Fraction(1, 10)
    assert result.critical_spans[0].exact is False
    assert result.fidelity_passed is False


def test_duplicated_critical_span_does_not_pass_as_exact() -> None:
    reference = ActivityReference(
        8,
        "en",
        (
            "please confirm with the customer that the scheduled technician does not "
            "need emergency access to the locked equipment room during the routine "
            "maintenance visit tomorrow morning"
        ),
        critical_spans=(CriticalSpan(CriticalSpanKind.NEGATION, "not"),),
    )

    result = align_fragments(
        (
            "please confirm with the customer that the scheduled technician does not "
            "not need emergency access to the locked equipment room during the routine "
            "maintenance visit tomorrow morning",
        ),
        references=(reference,),
        policy=_policy(),
    )

    assert result.cer <= Fraction(1, 10)
    assert result.wer is not None and result.wer <= Fraction(1, 10)
    assert result.critical_spans[0].exact is False
    assert result.fidelity_passed is False


@pytest.mark.parametrize(
    "candidate",
    (
        "alpha alpha beta",
        "alpha beta beta",
    ),
)
def test_edit_crossing_critical_span_boundary_does_not_pass_as_exact(
    candidate: str,
) -> None:
    reference = ActivityReference(
        9,
        "en",
        "alpha beta",
        critical_spans=(
            CriticalSpan(CriticalSpanKind.CORRECTION, "alpha beta"),
        ),
    )

    result = align_fragments(
        (candidate,),
        references=(reference,),
        policy=_policy(assignment_cer=Fraction(1, 2)),
    )

    assert result.critical_spans[0].exact is False
    assert result.fidelity_passed is False


@pytest.mark.parametrize(
    ("candidate", "expected_exact"),
    (
        ("请确认 order 123 today 谢谢", True),
        ("请确认 order 124 today 谢谢", False),
    ),
)
def test_mixed_chinese_english_critical_span_exactness(
    candidate: str,
    expected_exact: bool,
) -> None:
    reference = ActivityReference(
        10,
        "zh",
        "请确认 order 123 today 谢谢",
        critical_spans=(
            CriticalSpan(
                CriticalSpanKind.ENGLISH_TO_LANGUAGE,
                "order 123",
                language="en",
            ),
        ),
    )

    result = align_fragments((candidate,), references=(reference,), policy=_policy())

    assert result.schema_id == "gate_0b_alignment_v2"
    assert result.critical_spans[0].exact is expected_exact
    assert result.fidelity_passed is expected_exact


@pytest.mark.parametrize(
    ("language", "reference_text", "candidate", "kind", "span"),
    (
        ("es", "I need ayuda ahora", "I need ayuda ahora", "english_to_language", "I need ayuda"),
        ("es", "necesito help now", "necesito help now", "language_to_english", "help now"),
    ),
)
def test_code_switch_directions_use_script_authored_critical_spans(
    language: str,
    reference_text: str,
    candidate: str,
    kind: str,
    span: str,
) -> None:
    reference = ActivityReference(
        9,
        language,
        reference_text,
        critical_spans=(CriticalSpan(CriticalSpanKind(kind), span),),
    )

    result = align_fragments((candidate,), references=(reference,), policy=_policy())

    assert result.fidelity_passed is True
    assert result.critical_spans[0].exact is True


def test_chinese_alignment_reports_cer_without_inventing_word_segmentation() -> None:
    result = align_fragments(
        ("需要修理暖气",),
        references=(ActivityReference(11, "zh", "需要修理暖气"),),
        policy=_policy(),
    )

    assert result.cer == 0
    assert result.word_edits is None
    assert result.wer is None
    assert result.fidelity_passed is True


def test_aligner_does_not_rewrite_or_reorder_assembler_events() -> None:
    events = (
        CallerTurnEvent(CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT, 10, 1, 1, "Book "),
        CallerTurnEvent(CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT, 20, 2, 1, "today"),
        CallerTurnEvent(CallerTurnEventKind.TURN_COMPLETE, 30, 3, 1),
    )
    before = tuple(asdict(event) for event in events)

    result = align_caller_turn_events(
        events,
        references=(ActivityReference(1, "en", "book today"),),
        policy=_policy(),
    )

    assembler = CallerTurnAssembler(active_epoch=1, quiescence_ms=100)
    assembled = ()
    for event in events:
        assembled += assembler.ingest(event)
    assembled += assembler.advance_time(130)

    assert result.eligible is True
    assert tuple(asdict(event) for event in events) == before
    assert assembled[0].transcript == "Book today"


def test_alignment_contract_rejects_unbounded_or_malformed_input() -> None:
    with pytest.raises(ValueError, match="supported language"):
        normalize_text("hello", "de")
    with pytest.raises(ValueError, match="fragment count"):
        reconstruct_fragments(tuple("x" for _ in range(129)), policy=_policy())
    with pytest.raises(ValueError, match="text bound"):
        reconstruct_fragments(("x" * 4_001,), policy=_policy())
    with pytest.raises(ValueError, match="text bound"):
        normalize_text("ß" * 4_000, "en")
    with pytest.raises(ValueError, match="edit matrix"):
        compute_edit_counts(tuple("a" * 2_000), tuple("b" * 2_000))
    with pytest.raises(ValueError, match="unique"):
        align_fragments(
            ("hello",),
            references=(
                ActivityReference(1, "en", "hello"),
                ActivityReference(1, "en", "goodbye"),
            ),
            policy=_policy(),
        )
    with pytest.raises(TypeError, match="tuple"):
        align_fragments(  # type: ignore[arg-type]
            ["hello"],
            references=(ActivityReference(1, "en", "hello"),),
            policy=_policy(),
        )
    with pytest.raises(ValueError, match="reference count"):
        align_fragments(
            ("hello",),
            references=tuple(
                ActivityReference(index, "en", f"reference {index}")
                for index in range(11)
            ),
            policy=_policy(),
        )
    with pytest.raises(ValueError, match="uniquely"):
        ActivityReference(
            12,
            "en",
            "not today and not tomorrow",
            critical_spans=(CriticalSpan(CriticalSpanKind.NEGATION, "not"),),
        )


def test_alignment_module_has_no_remote_or_learned_judge_dependency() -> None:
    path = Path("app/services/caller_turn_alignment.py")
    source = path.read_text()
    tree = ast.parse(source)
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )

    assert imports.isdisjoint(
        {"google", "httpx", "openai", "requests", "sentence_transformers", "socket"}
    )
    assert "embedding" not in source.lower()
