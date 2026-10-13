"""Google-signed scheduler OIDC authentication, independent of contractor auth."""

import asyncio
import math
import re
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from app.config import settings, validate_runtime_safety

_TRANSPORT_TIMEOUT = 5.0
_EMAIL = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com\Z")


class BoundedGoogleRequest(GoogleRequest):
    def __call__(self, *args, **kwargs):
        kwargs["timeout"] = _TRANSPORT_TIMEOUT
        return super().__call__(*args, **kwargs)


def validate_worker_configuration():
    if settings.environment != "staging" or settings.background_worker_mode != "external":
        raise RuntimeError("Private worker requires staging external mode")
    validate_runtime_safety()
    audience = settings.background_worker_scheduler_audience
    parsed = urlsplit(audience)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or parsed.port not in {None, 443} or audience != audience.strip()):
        raise RuntimeError("Worker scheduler audience must be an exact HTTPS service URL")
    if not _EMAIL.fullmatch(settings.background_worker_scheduler_service_account_email):
        raise RuntimeError("Worker scheduler service account email is malformed")


def _verify(token: str):
    claims = id_token.verify_oauth2_token(
        token, BoundedGoogleRequest(), audience=settings.background_worker_scheduler_audience)
    # Check again explicitly: the verifier enforces these cryptographically; this
    # also rejects accidentally swapped/mocked verifier implementations.
    if (claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}
            or claims.get("aud") != settings.background_worker_scheduler_audience
            or claims.get("email") != settings.background_worker_scheduler_service_account_email
            or claims.get("email_verified") is not True
            or not isinstance(claims.get("sub"), str) or not claims["sub"].strip()
            or not isinstance(claims.get("exp"), (float, int))
            or isinstance(claims.get("exp"), bool) or not math.isfinite(claims["exp"])
            or claims["exp"] <= time.time()):
        raise ValueError("Invalid worker identity")


async def verify_worker_identity(request: Request):
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or not 0 < len(authorization[7:]) <= 16384:
        raise HTTPException(status_code=401, detail="Worker authentication required")
    try:
        await asyncio.to_thread(_verify, authorization[7:])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid worker identity") from None
