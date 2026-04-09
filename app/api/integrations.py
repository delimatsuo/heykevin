"""OAuth integration endpoints for Jobber and Google Calendar."""

import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request, Depends
from fastapi.responses import HTMLResponse

import httpx

from app.config import settings
from app.middleware.auth import verify_api_token, require_contractor_access
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

JOBBER_AUTH_URL = "https://api.getjobber.com/api/oauth/authorize"
JOBBER_TOKEN_URL = "https://api.getjobber.com/api/oauth/token"

# Redirect URI the backend receives after Jobber OAuth consent
JOBBER_REDIRECT_URI = f"{settings.cloud_run_url}/api/integrations/jobber/callback"


def _success_page(service_name: str) -> str:
    """Return a styled HTML success page after OAuth connection."""
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Connected - Hey Kevin</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
        }}
        .card {{
            background: rgba(255,255,255,0.15);
            backdrop-filter: blur(10px);
            border-radius: 24px;
            padding: 48px 32px;
            text-align: center;
            max-width: 360px;
            width: 90%;
        }}
        .check {{
            width: 72px;
            height: 72px;
            background: rgba(255,255,255,0.2);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 24px;
            font-size: 36px;
        }}
        h1 {{ font-size: 24px; margin-bottom: 12px; }}
        p {{ font-size: 16px; opacity: 0.85; line-height: 1.5; margin-bottom: 32px; }}
        .btn {{
            display: inline-block;
            background: white;
            color: #764ba2;
            padding: 14px 32px;
            border-radius: 12px;
            text-decoration: none;
            font-weight: 600;
            font-size: 16px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="check">&#10003;</div>
        <h1>{service_name} Connected</h1>
        <p>Your {service_name} account is now linked to Hey Kevin. You can close this page and return to the app.</p>
        <a href="javascript:window.close()" class="btn">Close</a>
    </div>
</body>
</html>"""


def _get_firestore():
    """Lazy import to avoid circular deps."""
    from app.db.firestore_client import get_firestore_client
    return get_firestore_client()


# ── Connect (start OAuth flow) ──────────────────────────────────────

@router.get("/jobber/connect", dependencies=[Depends(verify_api_token)])
async def jobber_connect(contractor_id: str = Query(...), request: Request = None):
    """Generate a Jobber OAuth authorize URL for the contractor."""
    require_contractor_access(request, contractor_id)
    if not settings.jobber_client_id:
        raise HTTPException(status_code=501, detail="Jobber integration not configured")

    state = secrets.token_urlsafe(32)

    # Store state → contractor mapping in Firestore with 10-min TTL
    db = _get_firestore()
    db.collection("jobber_oauth_states").document(state).set({
        "contractor_id": contractor_id,
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    })

    authorize_url = JOBBER_AUTH_URL + "?" + urlencode({
        "client_id": settings.jobber_client_id,
        "redirect_uri": JOBBER_REDIRECT_URI,
        "response_type": "code",
        "state": state,
    })

    return {"authorize_url": authorize_url}


# ── Callback (exchange code for tokens) ─────────────────────────────

@router.get("/jobber/callback")
async def jobber_callback(code: str = Query(...), state: str = Query(...)):
    """Exchange authorization code for access + refresh tokens."""
    db = _get_firestore()

    # Validate state
    state_ref = db.collection("jobber_oauth_states").document(state)
    state_doc = state_ref.get()
    if not state_doc.exists:
        raise HTTPException(status_code=400, detail="Invalid or expired state parameter")

    state_data = state_doc.to_dict()
    if time.time() > state_data.get("expires_at", 0):
        state_ref.delete()
        raise HTTPException(status_code=400, detail="State parameter expired")

    contractor_id = state_data["contractor_id"]
    state_ref.delete()  # one-time use

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            JOBBER_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.jobber_client_id,
                "client_secret": settings.jobber_client_secret,
                "redirect_uri": JOBBER_REDIRECT_URI,
            },
            timeout=10.0,
        )

    if resp.status_code != 200:
        logger.error(f"Jobber token exchange failed: {resp.status_code} {resp.text[:200]}")
        raise HTTPException(status_code=502, detail="Failed to exchange code with Jobber")

    tokens = resp.json()
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")

    if not access_token:
        raise HTTPException(status_code=502, detail="No access token in Jobber response")

    # Store tokens on contractor doc (encryption via KMS to be added later)
    db.collection("contractors").document(contractor_id).update({
        "jobber_access_token": access_token,
        "jobber_refresh_token": refresh_token,
        "jobber_connected_at": time.time(),
    })

    logger.info(f"Jobber connected for contractor {contractor_id}")

    return HTMLResponse(_success_page("Jobber"))


# ── Status ───────────────────────────────────────────────────────────

@router.get("/jobber/status", dependencies=[Depends(verify_api_token)])
async def jobber_status(contractor_id: str = Query(...), request: Request = None):
    """Check whether a contractor has Jobber connected."""
    require_contractor_access(request, contractor_id)
    db = _get_firestore()
    doc = db.collection("contractors").document(contractor_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Contractor not found")

    data = doc.to_dict()
    connected = bool(data.get("jobber_access_token"))
    return {
        "connected": connected,
        "connected_at": data.get("jobber_connected_at"),
    }


# ── Disconnect ───────────────────────────────────────────────────────

@router.post("/jobber/disconnect", dependencies=[Depends(verify_api_token)])
async def jobber_disconnect(contractor_id: str = Query(...), request: Request = None):
    """Revoke Jobber tokens and remove from contractor doc."""
    require_contractor_access(request, contractor_id)
    db = _get_firestore()
    doc_ref = db.collection("contractors").document(contractor_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Contractor not found")

    data = doc.to_dict()
    access_token = data.get("jobber_access_token", "")

    # Best-effort revoke with Jobber
    if access_token:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://api.getjobber.com/api/oauth/revoke",
                    data={
                        "token": access_token,
                        "client_id": settings.jobber_client_id,
                        "client_secret": settings.jobber_client_secret,
                    },
                    timeout=5.0,
                )
        except Exception as e:
            logger.warning(f"Jobber token revoke failed (non-critical): {e}")

    # Remove tokens from Firestore
    from google.cloud.firestore_v1 import DELETE_FIELD
    doc_ref.update({
        "jobber_access_token": DELETE_FIELD,
        "jobber_refresh_token": DELETE_FIELD,
        "jobber_connected_at": DELETE_FIELD,
    })

    logger.info(f"Jobber disconnected for contractor {contractor_id}")
    return {"status": "disconnected"}


# ═══════════════════════════════════════════════════════════════════════
# Google Calendar (fallback for contractors without Jobber)
# ═══════════════════════════════════════════════════════════════════════

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
GOOGLE_REDIRECT_URI = f"{settings.cloud_run_url}/api/integrations/google-calendar/callback"


# ── Connect (start OAuth flow) ──────────────────────────────────────

@router.get("/google-calendar/connect", dependencies=[Depends(verify_api_token)])
async def google_calendar_connect(contractor_id: str = Query(...), request: Request = None):
    """Generate a Google OAuth authorize URL for the contractor."""
    require_contractor_access(request, contractor_id)
    if not settings.google_calendar_client_id:
        raise HTTPException(status_code=501, detail="Google Calendar integration not configured")

    state = secrets.token_urlsafe(32)

    db = _get_firestore()
    db.collection("google_oauth_states").document(state).set({
        "contractor_id": contractor_id,
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    })

    authorize_url = GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": settings.google_calendar_client_id,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })

    return {"authorize_url": authorize_url}


# ── Callback (exchange code for tokens) ─────────────────────────────

@router.get("/google-calendar/callback")
async def google_calendar_callback(code: str = Query(...), state: str = Query(...)):
    """Exchange authorization code for access + refresh tokens."""
    db = _get_firestore()

    state_ref = db.collection("google_oauth_states").document(state)
    state_doc = state_ref.get()
    if not state_doc.exists:
        raise HTTPException(status_code=400, detail="Invalid or expired state parameter")

    state_data = state_doc.to_dict()
    if time.time() > state_data.get("expires_at", 0):
        state_ref.delete()
        raise HTTPException(status_code=400, detail="State parameter expired")

    contractor_id = state_data["contractor_id"]
    state_ref.delete()  # one-time use

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.google_calendar_client_id,
                "client_secret": settings.google_calendar_client_secret,
                "redirect_uri": GOOGLE_REDIRECT_URI,
            },
            timeout=10.0,
        )

    if resp.status_code != 200:
        logger.error(f"Google token exchange failed: {resp.status_code} {resp.text[:200]}")
        raise HTTPException(status_code=502, detail="Failed to exchange code with Google")

    tokens = resp.json()
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")

    if not access_token:
        raise HTTPException(status_code=502, detail="No access token in Google response")

    db.collection("contractors").document(contractor_id).update({
        "google_calendar_access_token": access_token,
        "google_calendar_refresh_token": refresh_token,
        "google_calendar_connected_at": time.time(),
    })

    logger.info(f"Google Calendar connected for contractor {contractor_id}")

    return HTMLResponse(_success_page("Google Calendar"))


# ── Status ───────────────────────────────────────────────────────────

@router.get("/google-calendar/status", dependencies=[Depends(verify_api_token)])
async def google_calendar_status(contractor_id: str = Query(...), request: Request = None):
    """Check whether a contractor has Google Calendar connected."""
    require_contractor_access(request, contractor_id)
    db = _get_firestore()
    doc = db.collection("contractors").document(contractor_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Contractor not found")

    data = doc.to_dict()
    connected = bool(data.get("google_calendar_access_token"))
    return {
        "connected": connected,
        "connected_at": data.get("google_calendar_connected_at"),
    }


# ── Disconnect ───────────────────────────────────────────────────────

@router.post("/google-calendar/disconnect", dependencies=[Depends(verify_api_token)])
async def google_calendar_disconnect(contractor_id: str = Query(...), request: Request = None):
    """Revoke Google tokens and remove from contractor doc."""
    require_contractor_access(request, contractor_id)
    db = _get_firestore()
    doc_ref = db.collection("contractors").document(contractor_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Contractor not found")

    data = doc.to_dict()
    access_token = data.get("google_calendar_access_token", "")

    # Best-effort revoke with Google
    if access_token:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": access_token},
                    timeout=5.0,
                )
        except Exception as e:
            logger.warning(f"Google token revoke failed (non-critical): {e}")

    from google.cloud.firestore_v1 import DELETE_FIELD
    doc_ref.update({
        "google_calendar_access_token": DELETE_FIELD,
        "google_calendar_refresh_token": DELETE_FIELD,
        "google_calendar_connected_at": DELETE_FIELD,
    })

    logger.info(f"Google Calendar disconnected for contractor {contractor_id}")
    return {"status": "disconnected"}
