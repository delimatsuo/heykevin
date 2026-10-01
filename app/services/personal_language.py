"""Language normalization and resolution helpers for personal and voice pipelines.

Pure bounded helpers with no network or heavy dependencies.
"""
from __future__ import annotations

from typing import Any

_PORTUGUESE_VARIANTS = frozenset({
    "pt",
    "pt-br",
    "pt_br",
    "pt-pt",
    "pt_pt",
    "portuguese",
    "português",
    "portugues",
})


def normalize_language(lang: Any) -> str:
    """Resolve user_language to a canonical BCP-47 tag or 'en'.

    New iOS app sends 'pt-BR'; older app sends 'pt'.
    Both resolve to 'pt-BR'. Unrecognized or empty values default safely to 'en'.
    """
    if not isinstance(lang, str):
        return "en"
    cleaned = lang.strip().lower()
    if not cleaned:
        return "en"

    if cleaned in _PORTUGUESE_VARIANTS:
        return "pt-BR"
    if cleaned in {"en", "en-us", "en_us", "en-gb", "en_gb", "en-ca", "en_ca", "english"}:
        return "en"
    if cleaned in {"es", "es-es", "es_es", "es-mx", "es_mx", "spanish", "español", "espanol"}:
        return "es"
    if cleaned in {"fr", "fr-fr", "fr_fr", "fr-ca", "fr_ca", "french", "français", "francais"}:
        return "fr"
    if cleaned in {"de", "de-de", "de_de", "german", "deutsch"}:
        return "de"
    if cleaned in {"it", "it-it", "it_it", "italian", "italiano"}:
        return "it"

    # For any simple 2-letter ISO code
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned

    return "en"


def is_portuguese(lang: Any) -> bool:
    """Return True if the specified language represents Brazilian Portuguese."""
    return normalize_language(lang) == "pt-BR"


def resolve_gemini_voice_key(lang: Any) -> str:
    """Map canonical or client language code to existing Gemini voice key in GEMINI_VOICES."""
    normalized = normalize_language(lang)
    if normalized == "pt-BR" or normalized.startswith("pt"):
        return "pt"
    if len(normalized) >= 2:
        prefix = normalized[:2].lower()
        if prefix in {"en", "pt", "de", "fr", "it", "es"}:
            return prefix
    return "en"
