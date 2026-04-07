"""Bearer token authentication for management API endpoints."""

from typing import Optional

from fastapi import Request, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


async def verify_api_token(request: Request):
    """Validate Bearer token on /api/* routes.

    Used as a FastAPI dependency. If no API_BEARER_TOKEN is configured,
    auth is skipped (development mode).
    """
    if not settings.api_bearer_token:
        # No token configured — skip auth (dev mode only)
        return

    credentials: Optional[HTTPAuthorizationCredentials] = await _bearer_scheme(request)
    if not credentials or credentials.credentials != settings.api_bearer_token:
        logger.warning("Unauthorized API access attempt", extra={"path": request.url.path})
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
