"""OAuth 2.0 PKCE (RFC 7636) helpers for Jobber integration."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from typing import Any

_PKCE_VERIFIER_REGEX = re.compile(r"^[A-Za-z0-9._~-]{43,128}\Z")
PKCE_METHOD_S256 = "S256"


class PkceError(Exception):
    """Generic value-free exception for PKCE generation or validation failures."""


def generate_pkce_code_verifier() -> str:
    """Generate a high-entropy cryptographic PKCE code verifier (86 unreserved ASCII chars)."""
    return secrets.token_urlsafe(64)


def validate_pkce_code_verifier(verifier: Any) -> str:
    """Validate that verifier is an exact str of 43-128 unreserved URL-safe ASCII characters.

    Fails closed on non-str types, bools, byte strings, whitespace, or invalid lengths/characters.
    Returns the validated verifier string unchanged.
    """
    if not isinstance(verifier, str) or type(verifier) is not str:
        raise PkceError("Invalid PKCE code verifier type")
    if not _PKCE_VERIFIER_REGEX.fullmatch(verifier):
        raise PkceError("Invalid PKCE code verifier format")
    return verifier


def derive_pkce_code_challenge(verifier: Any) -> str:
    """Derive the S256 PKCE code challenge from a validated code verifier.

    challenge = base64url(SHA256(ASCII(verifier))) with no trailing '=' padding.
    """
    valid_verifier = validate_pkce_code_verifier(verifier)
    digest = hashlib.sha256(valid_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
