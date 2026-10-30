"""Google sign-in for the admin page.

The admin page's "Sign in with Google" button returns a Google ID token, which
the page sends as its bearer token. This module verifies it (Google's
signature, audience = ADMIN_GOOGLE_CLIENT_ID, issuer, expiry, verified email)
and admits only the accounts listed in ADMIN_EMAILS. Off unless both are set.

An admin Google session reaches only what the admin page uses
(ADMIN_PAGE_PATH_PREFIXES); scripts keep the global bearer token.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Optional

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

ADMIN_PAGE_PATH_PREFIXES = (
    "/api/admin/",
    "/api/integrations/jobber/lead-capture",
)

# Verified tokens, so the page's steady polling doesn't re-verify every
# request. Entries expire with the token itself.
_CACHE_LIMIT = 128
_verified: dict[str, tuple[str, float]] = {}  # sha256(token) -> (email, exp)

# Google's signing certificates, cached for as long as Google says they're
# good, so a flood of forged tokens costs a local signature check, not a
# certificate fetch on the thread pool that also routes calls.
_GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
_GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
_CERTS_TIMEOUT_SECONDS = 5
_CERTS_DEFAULT_TTL_SECONDS = 3600
# A token signed by a key we haven't seen may mean Google rotated early; refetch
# for that at most this often.
_CERTS_FORCED_REFRESH_SECONDS = 300
_certs: dict = {"keys": None, "expires": 0.0, "fetched": 0.0}
_session = None


def admin_emails() -> frozenset[str]:
    return frozenset(
        email.strip().lower() for email in settings.admin_emails.split(",") if email.strip()
    )


def google_sign_in_enabled() -> bool:
    return bool(settings.admin_google_client_id.strip()) and bool(admin_emails())


def _max_age(cache_control: str) -> int:
    for part in cache_control.split(","):
        name, _, value = part.strip().partition("=")
        if name == "max-age" and value.isdigit():
            return int(value)
    return _CERTS_DEFAULT_TTL_SECONDS


def _google_certs(force: bool = False) -> dict:
    """Google's ID-token signing certificates, from cache while they're fresh."""
    global _session
    now = time.time()
    if not force and _certs["keys"] and _certs["expires"] > now:
        return _certs["keys"]
    import requests

    if _session is None:
        _session = requests.Session()
    response = _session.get(_GOOGLE_CERTS_URL, timeout=_CERTS_TIMEOUT_SECONDS)
    response.raise_for_status()
    _certs.update(
        keys=response.json(),
        expires=now + _max_age(response.headers.get("Cache-Control", "")),
        fetched=now,
    )
    return _certs["keys"]


def _verify_claims(token: str) -> dict:
    """Google's checks: signature, audience, issuer, expiry. Raises if any fail.

    The same checks as google.oauth2.id_token.verify_oauth2_token, against
    cached certificates instead of a fetch per token.
    """
    from google.auth import jwt

    audience = settings.admin_google_client_id.strip()
    certs = _google_certs()
    if jwt.decode_header(token).get("kid") not in certs and (
        time.time() - _certs["fetched"] > _CERTS_FORCED_REFRESH_SECONDS
    ):
        certs = _google_certs(force=True)
    claims = jwt.decode(token, certs=certs, audience=audience, clock_skew_in_seconds=10)
    if claims.get("iss") not in _GOOGLE_ISSUERS:
        raise ValueError("ID token not issued by Google")
    return claims


async def admin_email_for_token(token: str, path: str) -> Optional[str]:
    """The allowlisted admin a Google ID token proves for this path, else None."""
    if not google_sign_in_enabled() or token.count(".") != 2:
        return None
    if not path.startswith(ADMIN_PAGE_PATH_PREFIXES):
        return None

    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    cached = _verified.get(key)
    if cached and cached[1] > now:
        email = cached[0]
    else:
        try:
            claims = await asyncio.get_running_loop().run_in_executor(None, _verify_claims, token)
        except Exception as e:  # noqa: BLE001 — any failure means "not an admin"
            logger.warning("Admin Google sign-in rejected: %s", type(e).__name__)
            return None
        if claims.get("email_verified") is not True:
            return None
        email = str(claims.get("email") or "").strip().lower()
        # Google owns Gmail addresses and Workspace accounts (they carry `hd`).
        # A consumer Google account made with some other address only proved
        # that mailbox once, at sign-up, so it can't vouch for it now.
        if not (email.endswith("@gmail.com") or claims.get("hd")):
            logger.warning("Admin Google sign-in refused: not a Gmail or Workspace account")
            return None
        if len(_verified) >= _CACHE_LIMIT:
            _verified.clear()
        _verified[key] = (email, float(claims.get("exp") or 0))

    if email not in admin_emails():
        logger.warning("Admin Google sign-in from an account not on ADMIN_EMAILS")
        return None
    return email
