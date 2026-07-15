"""Shared fail-closed allocation rules for offline Gate 0B evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable


VALID_SPLITS = ("development", "holdout")
VALID_LANGUAGES = ("ar", "en", "es", "fr", "hi", "ht", "pt", "zh")
PRIMARY_CONDITIONS = (
    "clean",
    "twilio_codec_only",
    "acoustic_impairment",
    "interaction_stress",
)
REQUIRED_STRESS_TAGS = frozenset(
    {
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
)
CODE_SWITCH_TAGS = (
    "code_switch_english_to_language",
    "code_switch_language_to_english",
)
REQUIRED_CRITICAL_KINDS = frozenset(
    {
        "digits",
        "negation",
        "correction",
        "identity_confusable",
        "english_to_language",
        "language_to_english",
    }
)
NO_SPEECH_CONDITIONS = ("silence", "background_noise")

_APPLICABLE_CRITICAL_SPANS = {
    "number_dictation": "digits",
    "correction": "correction",
    CODE_SWITCH_TAGS[0]: "english_to_language",
    CODE_SWITCH_TAGS[1]: "language_to_english",
}


class AllocationError(ValueError):
    """Raised when a Gate 0B schedule or evidence population is incomplete."""


@dataclass(frozen=True, slots=True)
class AllocationActivity:
    ordinal: int
    split: str
    language: str
    condition: str
    scenario_tags: tuple[str, ...]
    critical_span_kinds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NoSpeechAllocation:
    ordinal: int
    split: str
    condition: str


def validate_gate0b_allocation(
    activities: Iterable[AllocationActivity],
    no_speech_windows: Iterable[NoSpeechAllocation],
    *,
    split: str,
) -> None:
    """Require the complete preregistered population for one sealed split."""
    if split not in VALID_SPLITS:
        raise AllocationError("allocation split is invalid")
    activity_values = tuple(activities)
    window_values = tuple(no_speech_windows)
    if any(not isinstance(value, AllocationActivity) for value in activity_values):
        raise TypeError("activities must contain AllocationActivity values")
    if any(not isinstance(value, NoSpeechAllocation) for value in window_values):
        raise TypeError("no-speech windows must contain NoSpeechAllocation values")

    activity_base = 0 if split == "development" else 128
    expected_activity_ordinals = set(range(activity_base, activity_base + 128))
    ordinals = [value.ordinal for value in activity_values]
    if (
        len(activity_values) != 128
        or len(ordinals) != len(set(ordinals))
        or set(ordinals) != expected_activity_ordinals
        or any(value.split != split for value in activity_values)
    ):
        raise AllocationError("activity allocation cardinality is invalid")

    language_condition_counts = Counter(
        (value.language, value.condition) for value in activity_values
    )
    if set(value.language for value in activity_values) != set(VALID_LANGUAGES):
        raise AllocationError("activity language allocation is invalid")
    if set(value.condition for value in activity_values) != set(PRIMARY_CONDITIONS):
        raise AllocationError("activity condition allocation is invalid")
    if any(
        language_condition_counts[(language, condition)] != 4
        for language in VALID_LANGUAGES
        for condition in PRIMARY_CONDITIONS
    ):
        raise AllocationError("each language must contain four activities per condition")

    stress_counts: Counter[str] = Counter()
    stress_languages: defaultdict[str, set[str]] = defaultdict(set)
    code_switch_counts: Counter[tuple[str, str]] = Counter()
    observed_critical_kinds: set[str] = set()
    recognized_tags = REQUIRED_STRESS_TAGS | set(CODE_SWITCH_TAGS) | {"standard"}
    for activity in activity_values:
        tags = activity.scenario_tags
        critical_kinds = activity.critical_span_kinds
        if (
            not isinstance(tags, tuple)
            or not tags
            or len(tags) > 16
            or len(tags) != len(set(tags))
            or any(tag not in recognized_tags for tag in tags)
        ):
            raise AllocationError("activity scenario allocation is invalid")
        if (
            not isinstance(critical_kinds, tuple)
            or not critical_kinds
            or len(critical_kinds) != len(set(critical_kinds))
            or any(kind not in REQUIRED_CRITICAL_KINDS for kind in critical_kinds)
        ):
            raise AllocationError("activity critical span allocation is invalid")
        observed_critical_kinds.update(critical_kinds)
        for tag in set(tags) & REQUIRED_STRESS_TAGS:
            stress_counts[tag] += 1
            stress_languages[tag].add(activity.language)
        for tag in set(tags) & set(CODE_SWITCH_TAGS):
            if activity.language == "en":
                raise AllocationError("English activities cannot satisfy code-switch coverage")
            code_switch_counts[(activity.language, tag)] += 1
        for tag, required_kind in _APPLICABLE_CRITICAL_SPANS.items():
            if tag in tags and required_kind not in critical_kinds:
                raise AllocationError("applicable critical span coverage is incomplete")

    if any(
        stress_counts[tag] < 8 or len(stress_languages[tag]) < 4
        for tag in REQUIRED_STRESS_TAGS
    ):
        raise AllocationError("stress coverage is incomplete")
    if any(
        code_switch_counts[(language, tag)] < 1
        for language in VALID_LANGUAGES
        if language != "en"
        for tag in CODE_SWITCH_TAGS
    ):
        raise AllocationError("code-switch direction coverage is incomplete")
    if observed_critical_kinds != REQUIRED_CRITICAL_KINDS:
        raise AllocationError("critical span coverage is incomplete")

    window_base = 0 if split == "development" else 32
    expected_window_ordinals = set(range(window_base, window_base + 32))
    window_ordinals = [value.ordinal for value in window_values]
    window_conditions = Counter(value.condition for value in window_values)
    if (
        len(window_values) != 32
        or len(window_ordinals) != len(set(window_ordinals))
        or set(window_ordinals) != expected_window_ordinals
        or any(value.split != split for value in window_values)
        or window_conditions != Counter({"silence": 16, "background_noise": 16})
    ):
        raise AllocationError("no-speech allocation is invalid")
