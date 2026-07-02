"""Turn-taking decisions for live voice calls.

This module keeps STT endpoint signals out of the LLM path until the caller's
speech looks like a complete turn. It is intentionally deterministic and small:
audio/STT events provide timing, while this controller adds dialogue-state and
semantic-shape checks before committing a turn.
"""

from dataclasses import dataclass
import re
from typing import Literal


TurnSignal = Literal["speech_final", "utterance_end", "buffer_cap", "deferred_timeout", "force"]
ExpectedAnswer = Literal["name", "location", "yes_no", "choice", "open", "unknown"]


@dataclass(frozen=True)
class TurnDecision:
    should_commit: bool
    text: str
    reason: str
    expected_answer: ExpectedAnswer
    signal: TurnSignal


class TurnTakingController:
    """Decide whether buffered STT text is ready for the receptionist LLM."""

    _CONTINUATION_WORDS = {
        "a", "an", "and", "are", "around", "at", "because", "but", "by",
        "for", "from", "if", "in", "is", "near", "of", "or", "that",
        "the", "to", "with", "you",
    }
    _DISCOURSE_TRAILING_PHRASES = {
        "i mean",
        "i guess",
        "like",
        "well",
    }
    _AUXILIARY_QUESTION_PREFIXES = (
        "can you",
        "could you",
        "do you",
        "does it",
        "where do",
        "would you",
    )
    _QUESTION_PREFIXES = (
        *_AUXILIARY_QUESTION_PREFIXES,
        "is that",
        "what are",
        "what is",
        "what's",
    )
    _FILLER_PREFIXES = {"k", "okay", "ok", "uh", "um", "hmm"}

    def __init__(self):
        self._last_agent_text = ""

    def record_agent_text(self, text: str):
        self._last_agent_text = text or ""

    def decide(self, segments: list[str], *, signal: TurnSignal, force: bool = False) -> TurnDecision:
        text = self._combine(segments)
        expected_answer = self._expected_answer()

        if force or signal in {"buffer_cap", "deferred_timeout", "force"}:
            return TurnDecision(True, text, "forced", expected_answer, signal)

        normalized = self._normalize(text)
        if not normalized:
            return TurnDecision(False, text, "empty", expected_answer, signal)

        if self._is_function_word_fragment(normalized):
            return TurnDecision(False, text, "function_word_fragment", expected_answer, signal)

        if self._is_discourse_fragment(normalized):
            return TurnDecision(False, text, "discourse_fragment", expected_answer, signal)

        if self._is_incomplete_question(normalized, text):
            return TurnDecision(False, text, "incomplete_question", expected_answer, signal)

        if self._ends_with_continuation_word(normalized):
            return TurnDecision(False, text, "trailing_continuation_word", expected_answer, signal)

        if self._is_expected_short_answer(normalized, expected_answer):
            return TurnDecision(True, text, "expected_short_answer", expected_answer, signal)

        if self._has_terminal_punctuation(text):
            return TurnDecision(True, text, "terminal_punctuation", expected_answer, signal)

        if len(normalized.split()) >= 4:
            return TurnDecision(True, text, "long_enough", expected_answer, signal)

        return TurnDecision(False, text, "short_unresolved", expected_answer, signal)

    @staticmethod
    def _combine(segments: list[str]) -> str:
        return " ".join(segment.strip() for segment in segments if segment and segment.strip()).strip()

    @classmethod
    def _normalize(cls, text: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9' ]", " ", text or "").lower()
        normalized = re.sub(r"\s+", " ", normalized).strip()
        words = normalized.split()
        while words and words[0] in cls._FILLER_PREFIXES:
            words = words[1:]
        return " ".join(words)

    @staticmethod
    def _has_terminal_punctuation(text: str) -> bool:
        return bool(re.search(r"[.?!]\s*$", text or ""))

    def _expected_answer(self) -> ExpectedAnswer:
        prompt = self._normalize(self._last_agent_text)
        if not prompt:
            return "unknown"
        if "name" in prompt or "who am i speaking with" in prompt:
            return "name"
        if (
            "city" in prompt
            or "town" in prompt
            or "area" in prompt
            or "where are you" in prompt
            or "where you are" in prompt
        ):
            return "location"
        if " or " in f" {prompt} ":
            return "choice"
        if (
            "is the number" in prompt
            or "correct" in prompt
            or prompt.startswith("is ")
            or prompt.startswith("are ")
            or prompt.startswith("do ")
        ):
            return "yes_no"
        if "what" in prompt or "how can i help" in prompt:
            return "open"
        return "unknown"

    def _is_expected_short_answer(self, normalized: str, expected_answer: ExpectedAnswer) -> bool:
        words = normalized.split()
        if not words or len(words) > 5:
            return False
        if self._is_function_word_fragment(normalized) or self._ends_with_continuation_word(normalized):
            return False
        if expected_answer in {"name", "location", "choice"}:
            return True
        if expected_answer == "yes_no" and words[0] in {"yes", "yeah", "yep", "correct", "right", "no", "nope"}:
            return True
        return False

    def _is_function_word_fragment(self, normalized: str) -> bool:
        words = normalized.split()
        return len(words) == 1 and words[0] in self._CONTINUATION_WORDS

    def _is_discourse_fragment(self, normalized: str) -> bool:
        return normalized in self._DISCOURSE_TRAILING_PHRASES

    def _is_incomplete_question(self, normalized: str, raw_text: str) -> bool:
        words = normalized.split()
        if self._has_terminal_punctuation(raw_text):
            return False
        if any(normalized.startswith(prefix) for prefix in self._AUXILIARY_QUESTION_PREFIXES):
            return len(words) <= 3
        return len(words) <= 4 and any(normalized.startswith(prefix) for prefix in self._QUESTION_PREFIXES)

    def _ends_with_continuation_word(self, normalized: str) -> bool:
        words = normalized.split()
        return bool(words and words[-1] in self._CONTINUATION_WORDS)
