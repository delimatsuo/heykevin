"""Bearer token authentication for management API endpoints.

Supports two token types:
1. Global admin token (API_BEARER_TOKEN) — full access, backward compatible
2. Per-contractor tokens (kv_ct_{contractor_id}_{secret}) — scoped to one contractor

Per-contractor tokens are stored as SHA-256 hashes in the contractor's Firestore doc.
The middleware resolves the token to a contractor_id and attaches it to request.state.
"""

import hashlib
import hmac
from typing import Optional

from fastapi import Request, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)

# In-memory cache for contractor token lookups (cleared on deploy)
_token_cache: dict[str, str] = {}  # token_hash -> contractor_id


async def verify_api_token(request: Request):
    """Validate Bearer token on /api/* routes.

    Supports global admin token and per-contractor scoped tokens.
    Sets request.state.contractor_id if a contractor token is used.
    Sets request.state.is_admin if the global token is used.
    """
    # Webhooks use Twilio signature verification, not bearer tokens
    if request.url.path.startswith("/webhooks/"):
        return
    # Health check and docs are public
    if request.url.path in ("/health", "/docs", "/openapi.json"):
        return
    # Onboarding endpoints use Apple identity token auth, not bearer tokens
    if request.url.path in ("/api/contractors/lookup-by-apple-id", "/api/contractors"):
        if request.method in ("GET", "POST"):
            # Allow through — these endpoints validate Apple identity token internally
            credentials = await _bearer_scheme(request)
            if credentials and hmac.compare_digest(credentials.credentials, settings.api_bearer_token):
                request.state.contractor_id = ""
                request.state.is_admin = True
            else:
                request.state.contractor_id = ""
                request.state.is_admin = False
            return

    if not settings.api_bearer_token:
        raise HTTPException(status_code=503, detail="API authentication not configured")

    credentials: Optional[HTTPAuthorizationCredentials] = await _bearer_scheme(request)
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = credentials.credentials

    # Check global admin token first (constant-time comparison)
    if hmac.compare_digest(token, settings.api_bearer_token):
        request.state.contractor_id = ""  # Admin has access to all
        request.state.is_admin = True
        return

    # Check per-contractor token
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # Check cache first
    if token_hash in _token_cache:
        request.state.contractor_id = _token_cache[token_hash]
        request.state.is_admin = False
        return

    # Look up in Firestore
    try:
        from app.db.contractors import get_contractor_by_api_token
        contractor = await get_contractor_by_api_token(token_hash)
        if contractor:
            cid = contractor["contractor_id"]
            _token_cache[token_hash] = cid
            request.state.contractor_id = cid
            request.state.is_admin = False
            return
    except Exception as e:
        logger.error(f"Token lookup failed: {e}")

    logger.warning("Unauthorized API access attempt", extra={"path": request.url.path})
    raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


def require_contractor_access(request: Request, contractor_id: str):
    """Verify the authenticated contractor has access to the requested resource.

    Admin tokens (global) have access to all contractors.
    Contractor tokens can only access their own data.
    """
    # Admin has access to all
    if getattr(request.state, "is_admin", False):
        return
    # Contractor must match
    token_contractor_id = getattr(request.state, "contractor_id", "")
    if not token_contractor_id or token_contractor_id != contractor_id:
        raise HTTPException(status_code=403, detail="Access denied")


def generate_contractor_token(contractor_id: str) -> tuple[str, str]:
    """Generate a per-contractor API token. Returns (raw_token, token_hash)."""
    import secrets
    secret = secrets.token_urlsafe(32)
    raw_token = f"kv_ct_{contractor_id[:8]}_{secret}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, token_hash
