"""Apple Sign-In identity-token verification.

This module verifies the JWT identity token returned by Sign in with Apple. It
fetches and caches Apple's public JWKS from
``https://appleid.apple.com/auth/keys`` (1-hour TTL) and performs full RS256
signature, issuer, audience, expiry, and ``sub`` validation. Used by the
unauthenticated bootstrap endpoints (``POST /api/contractors``,
``POST /api/contractors/lookup-by-apple-id``, and the temporary build-23
legacy ``GET /api/contractors/lookup-by-apple-id``) to prevent account
takeover via forged Apple user IDs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from jose import jwk as jose_jwk
from jose import jwt as jose_jwt
from jose.exceptions import JWTError
from jose.utils import base64url_decode

from app.utils.logging import get_logger

logger = get_logger(__name__)

APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ISSUER = "https://appleid.apple.com"
DEFAULT_AUDIENCE = "com.kevin.callscreen"
JWKS_CACHE_TTL_SECONDS = 3600  # 1 hour
LEEWAY_SECONDS = 60


class AppleAuthError(Exception):
    """Raised when an Apple identity token fails verification.

    The message is intentionally generic — callers should surface a 401 to the
    client without leaking which step failed.
    """


@dataclass
class AppleIdentity:
    """Verified Apple Sign-In identity claims."""

    sub: str
    aud: str
    iss: str
    email: Optional[str] = None
    email_verified: Optional[bool] = None
    is_private_email: Optional[bool] = None
    raw_claims: Optional[dict[str, Any]] = None


# Module-level cache. Stored as a simple tuple so we can swap atomically.
# (jwks_dict, fetched_at_unix_seconds)
_jwks_cache: tuple[Optional[dict[str, Any]], float] = (None, 0.0)


def _cache_is_fresh(now: float) -> bool:
    cached, fetched_at = _jwks_cache
    return cached is not None and (now - fetched_at) < JWKS_CACHE_TTL_SECONDS


async def _fetch_jwks(force_refresh: bool = False) -> dict[str, Any]:
    """Fetch Apple's JWKS, using a 1-hour module-level cache."""
    global _jwks_cache
    now = time.time()
    if not force_refresh and _cache_is_fresh(now):
        cached, _ = _jwks_cache
        assert cached is not None
        return cached

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(APPLE_JWKS_URL)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as e:
        # If we have a stale cache, prefer it over hard failure — but only if
        # this isn't an explicit refresh (kid not found path).
        cached, _ = _jwks_cache
        if cached is not None and not force_refresh:
            logger.warning(f"Apple JWKS fetch failed, using stale cache: {e}")
            return cached
        raise AppleAuthError("token verification unavailable") from e

    if not isinstance(data, dict) or "keys" not in data:
        raise AppleAuthError("token verification unavailable")

    _jwks_cache = (data, now)
    return data


def _find_jwk(jwks: dict[str, Any], kid: str) -> Optional[dict[str, Any]]:
    for key in jwks.get("keys", []):
        if isinstance(key, dict) and key.get("kid") == kid:
            return key
    return None


async def verify_apple_identity_token(
    identity_token: str,
    expected_user_id: str,
    expected_audience: str = DEFAULT_AUDIENCE,
) -> AppleIdentity:
    """Verify an Apple Sign-In identity token (JWT).

    Validates:
        * RS256 signature against Apple's JWKS (fetched + cached for 1h)
        * iss == https://appleid.apple.com
        * aud == ``expected_audience``
        * exp not expired (with 60s leeway)
        * sub == ``expected_user_id``

    Returns the parsed :class:`AppleIdentity` on success.
    Raises :class:`AppleAuthError` (with a generic message) on any failure.
    """
    if not identity_token or not isinstance(identity_token, str):
        raise AppleAuthError("identity token required")
    if not expected_user_id:
        raise AppleAuthError("identity token required")

    # Pull the unverified header so we can pick the right JWK.
    try:
        header = jose_jwt.get_unverified_header(identity_token)
    except JWTError as e:
        raise AppleAuthError("invalid identity token") from e

    kid = header.get("kid") if isinstance(header, dict) else None
    alg = header.get("alg") if isinstance(header, dict) else None
    if not kid or alg != "RS256":
        raise AppleAuthError("invalid identity token")

    jwks = await _fetch_jwks()
    jwk_data = _find_jwk(jwks, kid)
    if jwk_data is None:
        # Apple may have rotated keys since our last fetch — refresh once.
        jwks = await _fetch_jwks(force_refresh=True)
        jwk_data = _find_jwk(jwks, kid)
    if jwk_data is None:
        raise AppleAuthError("invalid identity token")

    try:
        public_key = jose_jwk.construct(jwk_data, algorithm="RS256")
    except (JWTError, Exception) as e:  # pragma: no cover - defensive
        raise AppleAuthError("invalid identity token") from e

    # Manually verify the signature so we can also do precise claim checks.
    try:
        message, encoded_signature = identity_token.rsplit(".", 1)
        decoded_sig = base64url_decode(encoded_signature.encode("utf-8"))
    except (ValueError, Exception) as e:
        raise AppleAuthError("invalid identity token") from e

    if not public_key.verify(message.encode("utf-8"), decoded_sig):
        raise AppleAuthError("invalid identity token")

    # Decode claims (signature already verified above, so use options to skip
    # re-verification but keep claim validation sane).
    try:
        claims = jose_jwt.get_unverified_claims(identity_token)
    except JWTError as e:
        raise AppleAuthError("invalid identity token") from e

    if not isinstance(claims, dict):
        raise AppleAuthError("invalid identity token")

    iss = claims.get("iss")
    if iss != APPLE_ISSUER:
        raise AppleAuthError("invalid identity token")

    aud = claims.get("aud")
    # Apple sometimes returns aud as a list; accept either.
    if isinstance(aud, list):
        if expected_audience not in aud:
            raise AppleAuthError("invalid identity token")
        aud_value = expected_audience
    else:
        if aud != expected_audience:
            raise AppleAuthError("invalid identity token")
        aud_value = aud

    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        raise AppleAuthError("invalid identity token")
    now = time.time()
    if now - LEEWAY_SECONDS > exp:
        raise AppleAuthError("invalid identity token")

    sub = claims.get("sub")
    if not sub or sub != expected_user_id:
        raise AppleAuthError("invalid identity token")

    email_verified_raw = claims.get("email_verified")
    if isinstance(email_verified_raw, str):
        email_verified: Optional[bool] = email_verified_raw.lower() == "true"
    elif isinstance(email_verified_raw, bool):
        email_verified = email_verified_raw
    else:
        email_verified = None

    is_private_email_raw = claims.get("is_private_email")
    if isinstance(is_private_email_raw, str):
        is_private_email: Optional[bool] = is_private_email_raw.lower() == "true"
    elif isinstance(is_private_email_raw, bool):
        is_private_email = is_private_email_raw
    else:
        is_private_email = None

    return AppleIdentity(
        sub=sub,
        aud=aud_value,
        iss=iss,
        email=claims.get("email"),
        email_verified=email_verified,
        is_private_email=is_private_email,
        raw_claims=claims,
    )


def _reset_cache_for_tests() -> None:
    """Test-only helper to clear the JWKS cache between cases."""
    global _jwks_cache
    _jwks_cache = (None, 0.0)
