"""Deterministic urgency classification shared by live voice pipelines."""

import re


ENGLISH_URGENCY_KEYWORDS = frozenset(
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
SPANISH_URGENCY_KEYWORDS = frozenset(
    {
        "agua por todas partes",
        "chispas",
        "echando chispas",
        "emergencia",
        "fuego",
        "fuga de gas",
        "huele a gas",
        "hospital",
        "humo",
        "incendio",
        "inundacion",
        "inundaci\u00f3n",
        "monoxido de carbono",
        "mon\u00f3xido de carbono",
        "no hay agua",
        "olor a gas",
        "sin agua",
        "tuberia rota",
        "tuberia reventada",
        "tuber\u00eda rota",
        "tuber\u00eda reventada",
    }
)
URGENCY_KEYWORDS = ENGLISH_URGENCY_KEYWORDS | SPANISH_URGENCY_KEYWORDS

_SIGNAL_PATTERN_TEXT = {
    phrase: re.escape(phrase)
    for phrase in URGENCY_KEYWORDS
}
_SIGNAL_PATTERN_TEXT["water everywhere"] = r"water(?:\s+is)?\s+everywhere"
_SIGNAL_PATTERN_TEXT["no hay agua"] = r"no\s+hay\s+agua(?!\s+por\s+todas)"

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
_SPANISH_NEGATION_PREFIX = re.compile(
    r"(?:"
    r"\bya\s+no\b"
    r"|\bno\s+(?:es|esta|estamos|estan|estoy|est\u00e1|est\u00e1n|fue|hay|habia|hab\u00eda|huele|huelo|veo|vemos)\b"
    r"|\b(?:ningun|ninguna|ninguno|nunca|sin)\b"
    r")"
    r"(?:\s+(?:actual|actualmente|alguna|algun|de|del|el|evidencia|indicio|indicios|la|olor|se\u00f1al|se\u00f1ales|un|una|ya))*"
    r"\s*$"
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
    r"|(?:is|was|were)\s+(?:gone|out)(?:\s+now)?(?=\s*(?:[.!?,;:]|$))"
    r"|(?:is|was|were)\s+over(?=\s*(?:[.!?,;:]|$))"
    r"|(?:is|was|were)\s+under\s+control"
    r")\b"
)
_SPANISH_RESOLVED_SUFFIX = re.compile(
    r"^\s*(?:,?\s*(?:que|la\s+cual)\s+)?"
    r"(?:ya\s+)?(?:ha\s+sido\s+|fue\s+|esta\s+|est\u00e1\s+)?"
    r"(?:arreglad[ao]|controlad[ao]|par[o\u00f3]|reparad[ao]|resuelt[ao]|se\s+detuvo|termin[o\u00f3])\b"
)
_PRONOUN_RESOLVED_SUFFIX = re.compile(
    r"^\s*[,;]?\s*(?:but|however)\s+it\s+(?:is|was)\s+"
    r"(?:gone|out(?:\s+now)?|under\s+control)\b"
)
_REACTIVATED_SUFFIX = re.compile(
    r"\b(?:but|however|luego|pero|sin\s+embargo|then)\b.{0,64}"
    r"\b(?:"
    r"again|back|continued|continues|de\s+nuevo|ha\s+vuelto|ongoing|otra\s+vez|"
    r"continua|contin\u00faa|recurred|regres[o\u00f3]|returned|resumed|restarted|"
    r"se\s+est(?:a|\u00e1)\s+extendiendo|spreading|started|volvi[o\u00f3]"
    r")\b"
)
_CORRECTION_PREFIX = re.compile(
    r"(?:^|[,;:])\s*(?:but|however|pero|sin\s+embargo)\s+.{0,32}?"
    r"(?:"
    r"\bthere\s+(?:is|was)\s+(?:no|not)\b"
    r"|\b(?:no|ya\s+no)\s+(?:es|esta|est\u00e1|fue|hay)\b"
    r")"
)


def _has_same_signal_correction(suffix: str, phrase: str) -> bool:
    correction = _CORRECTION_PREFIX.search(suffix)
    if not correction:
        return False
    corrected_tail = suffix[correction.end():correction.end() + 48]
    fillers = (
        r"(?:\s+(?:a|active|actual|actualmente|algun|alguna|an|any|currently|known|"
        r"longer|ningun|ninguna|ongoing|the|un|una|ya))*\s+"
    )
    return bool(re.match(fillers + _SIGNAL_PATTERN_TEXT[phrase], corrected_tail))


def _is_suppressed(text: str, start: int, end: int, phrase: str) -> bool:
    prefix = text[max(0, start - 96):start]
    suffix = text[end:end + 64]
    resolved_match = (
        _RESOLVED_SUFFIX.search(suffix)
        or _SPANISH_RESOLVED_SUFFIX.search(suffix)
        or _PRONOUN_RESOLVED_SUFFIX.search(suffix)
        or _has_same_signal_correction(suffix, phrase)
    )
    return bool(
        _NEGATION_PREFIX.search(prefix)
        or _NEGATED_PERCEPTION_PREFIX.search(prefix)
        or _COORDINATED_NEGATION_PREFIX.search(prefix)
        or _SPANISH_NEGATION_PREFIX.search(prefix)
        or _RESOLVED_PREFIX.search(prefix)
        or (resolved_match and not _REACTIVATED_SUFFIX.search(suffix))
    )


def find_urgent_signal(text: str) -> str | None:
    """Return the first active urgency phrase, excluding explicit negations.

    This intentionally handles only nearby, explicit negation and resolution.
    Ambiguous safety statements remain urgent so the classifier fails toward
    escalation instead of silently suppressing a possible emergency.
    """
    normalized = " ".join(text.casefold().replace("\u2019", "'").split())
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
        if _is_suppressed(normalized, start, end, phrase):
            suppressed_spans.append((start, end))
            continue
        return phrase
    return None
