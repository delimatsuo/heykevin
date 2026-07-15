"""Exact, shared Gate 0B allocation contract tests."""

from dataclasses import replace

import pytest

from app.services.qualification_allocation import (
    CODE_SWITCH_TAGS,
    PRIMARY_CONDITIONS,
    REQUIRED_CRITICAL_KINDS,
    REQUIRED_STRESS_TAGS,
    AllocationActivity,
    AllocationError,
    NoSpeechAllocation,
    validate_gate0b_allocation,
)


LANGUAGES = ("ar", "en", "es", "fr", "hi", "ht", "pt", "zh")


def _valid_split(
    split: str = "development",
) -> tuple[tuple[AllocationActivity, ...], tuple[NoSpeechAllocation, ...]]:
    base = 0 if split == "development" else 128
    activities: list[AllocationActivity] = []
    stress_tags = tuple(sorted(REQUIRED_STRESS_TAGS))
    critical_kinds = tuple(sorted(REQUIRED_CRITICAL_KINDS))
    for language_index, language in enumerate(LANGUAGES):
        for within_language in range(16):
            ordinal = base + language_index * 16 + within_language
            tags = ["standard"]
            if within_language >= 12:
                tags = [
                    tag
                    for tag_index, tag in enumerate(stress_tags)
                    if tag_index % 4 == within_language - 12
                ]
            if language != "en" and within_language == 10:
                tags.append(CODE_SWITCH_TAGS[0])
            if language != "en" and within_language == 11:
                tags.append(CODE_SWITCH_TAGS[1])
            applicable = {
                "number_dictation": "digits",
                "correction": "correction",
                CODE_SWITCH_TAGS[0]: "english_to_language",
                CODE_SWITCH_TAGS[1]: "language_to_english",
            }
            span_kinds = {critical_kinds[within_language % len(critical_kinds)]}
            span_kinds.update(applicable[tag] for tag in tags if tag in applicable)
            activities.append(
                AllocationActivity(
                    ordinal=ordinal,
                    split=split,
                    language=language,
                    condition=PRIMARY_CONDITIONS[within_language // 4],
                    scenario_tags=tuple(tags),
                    critical_span_kinds=tuple(sorted(span_kinds)),
                )
            )
    windows = tuple(
        NoSpeechAllocation(
            ordinal=base // 4 + index,
            split=split,
            condition="silence" if index < 16 else "background_noise",
        )
        for index in range(32)
    )
    return tuple(activities), windows


def test_exact_allocation_accepts_every_required_cell() -> None:
    activities, windows = _valid_split()

    validate_gate0b_allocation(activities, windows, split="development")


@pytest.mark.parametrize("tag", sorted(REQUIRED_STRESS_TAGS))
def test_exact_allocation_rejects_each_missing_stress_cell(tag: str) -> None:
    activities, windows = _valid_split()
    mutated = tuple(
        replace(activity, scenario_tags=tuple(value for value in activity.scenario_tags if value != tag))
        for activity in activities
    )

    with pytest.raises(AllocationError, match="stress coverage"):
        validate_gate0b_allocation(mutated, windows, split="development")


@pytest.mark.parametrize("tag", CODE_SWITCH_TAGS)
def test_exact_allocation_rejects_each_code_switch_direction(tag: str) -> None:
    activities, windows = _valid_split()
    mutated = tuple(
        replace(activity, scenario_tags=tuple(value for value in activity.scenario_tags if value != tag))
        for activity in activities
    )

    with pytest.raises(AllocationError, match="code-switch"):
        validate_gate0b_allocation(mutated, windows, split="development")


def test_exact_allocation_rejects_language_condition_correlation() -> None:
    activities, windows = _valid_split()
    mutated = tuple(
        replace(activity, condition=PRIMARY_CONDITIONS[LANGUAGES.index(activity.language) % 4])
        for activity in activities
    )

    with pytest.raises(AllocationError, match="condition"):
        validate_gate0b_allocation(mutated, windows, split="development")


def test_exact_allocation_rejects_silence_substituted_for_noise_stratum() -> None:
    activities, windows = _valid_split()
    mutated = tuple(replace(window, condition="silence") for window in windows)

    with pytest.raises(AllocationError, match="no-speech"):
        validate_gate0b_allocation(activities, mutated, split="development")


def test_exact_allocation_rejects_missing_applicable_critical_span() -> None:
    activities, windows = _valid_split()
    target = next(
        activity for activity in activities if "number_dictation" in activity.scenario_tags
    )
    mutated = tuple(
        replace(activity, critical_span_kinds=("negation",))
        if activity.ordinal == target.ordinal
        else activity
        for activity in activities
    )

    with pytest.raises(AllocationError, match="critical span"):
        validate_gate0b_allocation(mutated, windows, split="development")
