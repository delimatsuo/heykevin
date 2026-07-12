"""Deterministic urgency classification shared by live voice pipelines."""

import re


URGENCY_KEYWORDS = frozenset(
    {
        "accident",
        "breaker tripped",
        "burning smell",
        "burst pipe",
        "carbon monoxide",
        "electric panel",
        "electrical fire",
        "electrical panel",
        "emergency",
        "fire",
        "flood",
        "flooding",
        "gas leak",
        "hospital",
        "no water",
        "pipe burst",
        "sewage",
        "smell burning",
        "smoke",
        "sparking",
        "tripped breaker",
        "water everywhere",
    }
)

_SIGNAL_PATTERN_TEXT = {
    phrase: re.escape(phrase)
    for phrase in URGENCY_KEYWORDS
}
_SIGNAL_PATTERN_TEXT["water everywhere"] = r"water(?:\s+is)?\s+everywhere"

_PHRASE_PATTERNS = tuple(
    (
        phrase,
        re.compile(rf"(?<!\w){_SIGNAL_PATTERN_TEXT[phrase]}(?!\w)"),
    )
    for phrase in sorted(URGENCY_KEYWORDS)
)

_NEGATION_PREFIX = re.compile(
    r"(?:"
    r"\bno\s+longer\b"
    r"|\b(?:no|not|never|without)\b"
    r"|\b(?:is|are|was|were|do|does|did|have|has|had|can|could)\s+not\b"
    r"|\b(?:isn't|aren't|wasn't|weren't|don't|doesn't|didn't|haven't|hasn't|hadn't|can't|couldn't)\b"
    r")"
    r"(?:\s+(?:a|active|an|any|currently|evidence|indication|indications|known|of|ongoing|reported|sign|signs|still|the|whatsoever))*"
    r"\s*$"
)
_NEGATED_PERCEPTION_PREFIX = re.compile(
    r"(?:"
    r"\b(?:do|does|did|can|could)\s+not\b"
    r"|\b(?:don't|doesn't|didn't|can't|couldn't|haven't|hasn't|hadn't|never)\b"
    r")"
    r"\s+(?:been|detect(?:ed)?|experience(?:d)?|had|have|hear(?:d)?|notice(?:d)?|see|seen|smell(?:ed)?)"
    r"(?:\s+(?:a|an|any|evidence|of|sign|signs|the))*"
    r"\s*$"
)
_COORDINATED_NEGATION_PREFIX = re.compile(
    r"\b(?:no|not|without)\b(?:\s+[a-z0-9'-]+){1,5}\s+(?:and|or)\s*$"
)
_RESOLVED_PREFIX = re.compile(
    r"\b(?:cleared|former|previous|resolved)"
    r"(?:\s+(?:a|an|the))*\s*$"
)
_RESOLVED_SUFFIX = re.compile(
    r"^\s*(?:,?\s*(?:that|which)\s+)?"
    r"(?:"
    r"(?:(?:has|had)\s+(?:been\s+)?|(?:is|was|were)\s+)?"
    r"(?:cleared|contained|ended|fixed|resolved|stopped)"
    r"|(?:is|was|were)\s+over(?=\s*(?:[.!?,;:]|$))"
    r"|(?:is|was|were)\s+under\s+control"
    r")\b"
)
_REACTIVATED_SUFFIX = re.compile(
    r"\b(?:but|however|then)\b.{0,64}"
    r"\b(?:again|back|continued|continues|ongoing|recurred|returned|resumed|restarted|spreading|started)\b"
)


def _is_suppressed(text: str, start: int, end: int) -> bool:
    prefix = text[max(0, start - 96):start]
    suffix = text[end:end + 64]
    resolved_match = _RESOLVED_SUFFIX.search(suffix)
    return bool(
        _NEGATION_PREFIX.search(prefix)
        or _NEGATED_PERCEPTION_PREFIX.search(prefix)
        or _COORDINATED_NEGATION_PREFIX.search(prefix)
        or _RESOLVED_PREFIX.search(prefix)
        or (resolved_match and not _REACTIVATED_SUFFIX.search(suffix))
    )


def find_urgent_signal(text: str) -> str | None:
    """Return the first active urgency phrase, excluding explicit negations.

    This intentionally handles only nearby, explicit negation and resolution.
    Ambiguous safety statements remain urgent so the classifier fails toward
    escalation instead of silently suppressing a possible emergency.
    """
    normalized = " ".join(text.lower().replace("\u2019", "'").split())
    if not normalized:
        return None

    candidates = []
    for phrase, pattern in _PHRASE_PATTERNS:
        for match in pattern.finditer(normalized):
            candidates.append(
                (match.start(), -(match.end() - match.start()), phrase, match.end())
            )

    suppressed_spans: list[tuple[int, int]] = []
    for start, _negative_length, phrase, end in sorted(candidates):
        if any(
            blocked_start <= start and end <= blocked_end
            for blocked_start, blocked_end in suppressed_spans
        ):
            continue
        if _is_suppressed(normalized, start, end):
            suppressed_spans.append((start, end))
            continue
        return phrase
    return None
