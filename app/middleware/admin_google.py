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

# Verified tokens, so the page's steady polling doesn't re-fetch Google's
# certificates on every request. Entries expire with the token itself.
_CACHE_LIMIT = 128
_verified: dict[str, tuple[str, float]] = {}  # sha256(token) -> (email, exp)


def admin_emails() -> frozenset[str]:
    return frozenset(
        email.strip().lower() for email in settings.admin_emails.split(",") if email.strip()
    )


def google_sign_in_enabled() -> bool:
    return bool(settings.admin_google_client_id.strip()) and bool(admin_emails())


def _verify_claims(token: str) -> dict:
    """Google's checks: signature, audience, issuer, expiry. Raises if any fail."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(
        token,
        google_requests.Request(),
        settings.admin_google_client_id.strip(),
        clock_skew_in_seconds=10,
    )


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
        if len(_verified) >= _CACHE_LIMIT:
            _verified.clear()
        _verified[key] = (email, float(claims.get("exp") or 0))

    if email not in admin_emails():
        logger.warning("Admin Google sign-in from an account not on ADMIN_EMAILS")
        return None
    return email
