"""OAuth integration endpoints for Jobber and Google Calendar."""

import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.db.admin_audit import write_admin_audit_event
from app.middleware.auth import require_contractor_access, verify_api_token
from app.services.integration_tokens import (
    has_usable_token,
    safe_decrypt_integration_token,
    validate_token_expires_at,
    validate_token_expires_in,
    validate_token_string,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

JOBBER_AUTH_URL = "https://api.getjobber.com/api/oauth/authorize"
JOBBER_TOKEN_URL = "https://api.getjobber.com/api/oauth/token"
JOBBER_REDIRECT_URI = f"{settings.cloud_run_url}/api/integrations/jobber/callback"

GOOGLE_CALENDAR_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_REDIRECT_URI = f"{settings.cloud_run_url}/api/integrations/google-calendar/callback"
GOOGLE_REDIRECT_URI = GOOGLE_CALENDAR_REDIRECT_URI
GOOGLE_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
]
GOOGLE_CALENDAR_SCOPE = " ".join(GOOGLE_CALENDAR_SCOPES)


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
        .hint {{
            font-size: 14px;
            opacity: 0.7;
            margin-top: 8px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="check">&#10003;</div>
        <h1>{service_name} Connected</h1>
        <p>Your {service_name} account is now linked to Hey Kevin.</p>
        <p class="hint">You can close this page and go back to the app.</p>
    </div>
</body>
</html>"""


def _get_firestore():
    """Lazy import to avoid circular deps."""
    from app.db.firestore_client import get_firestore_client
    return get_firestore_client()


def _require_admin(request: Request):
    """Raise 403 if the caller is not using the global admin token."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


class JobberLeadCaptureUpdate(BaseModel):
    enabled: bool
    reason: str = Field(default="", max_length=500)


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
async def jobber_callback(code: str = Query(...), state: str = Query(...), request: Request = None):
    """Exchange authorization code for access + refresh tokens."""
    from app.services.integration_token_mutations import (
        connect_provider_cas,
        consume_oauth_state,
    )
    from app.services.integration_tokens import (
        MAX_KEY_VERSION,
        IntegrationTokenConfigError,
        IntegrationTokenDecryptionError,
        determine_write_format,
        is_encryption_configured,
    )

    if settings.integration_token_encrypted_writes_enabled and not is_encryption_configured():
        logger.error("Jobber OAuth callback aborted: integration token encryption is not configured")
        raise HTTPException(
            status_code=500,
            detail="Integration token encryption is not configured",
        ) from None

    db = _get_firestore()

    # Atomically validate and consume state (one-time use, deletes even if expired/malformed)
    state_data = await consume_oauth_state(
        db=db,
        collection_name="jobber_oauth_states",
        state=state,
    )
    raw_cid = state_data.get("contractor_id")
    try:
        contractor_id = validate_token_string(raw_cid, name="contractor_id")
        assert contractor_id is not None
    except Exception:
        logger.error("Jobber OAuth callback invalid contractor_id in state")
        raise HTTPException(status_code=400, detail="Invalid contractor in OAuth state") from None

    # Pre-exchange contractor precondition check
    contractor_ref = db.collection("contractors").document(contractor_id)
    contractor_doc = contractor_ref.get()
    if not getattr(contractor_doc, "exists", False):
        raise HTTPException(status_code=404, detail="Contractor not found") from None
    contractor_data = contractor_doc.to_dict() or {}
    if contractor_data.get("active") is not True:
        raise HTTPException(status_code=403, detail="Contractor account is inactive") from None

    raw_generation = contractor_data.get("jobber_generation")
    if raw_generation is None:
        observed_generation = 0
    elif type(raw_generation) is int and type(raw_generation) is not bool and 0 <= raw_generation <= MAX_KEY_VERSION:
        observed_generation = raw_generation
    else:
        logger.error("Jobber OAuth callback aborted: invalid stored generation")
        raise HTTPException(status_code=409, detail="Invalid contractor generation") from None

    observed_access_raw = contractor_data.get("jobber_access_token")
    observed_refresh_raw = contractor_data.get("jobber_refresh_token")

    # Validate that eventual write format is possible BEFORE exchanging the authorization code
    try:
        determine_write_format(
            contractor_id=contractor_id,
            provider="jobber",
            stored_access=observed_access_raw,
            stored_refresh=observed_refresh_raw,
            envelope_required=contractor_data.get("jobber_token_envelope_required"),
        )
    except (IntegrationTokenConfigError, IntegrationTokenDecryptionError) as exc:
        logger.error("Jobber OAuth callback aborted: integration token encryption unconfigured or historical key missing: %s", exc)
        raise HTTPException(status_code=500, detail="Integration token encryption configuration unavailable") from None
    except Exception as exc:
        logger.error("Jobber OAuth callback aborted: invalid stored credential state: %s", exc)
        raise HTTPException(status_code=409, detail="Contractor credentials in conflicted or malformed state") from None

    # Exchange code for tokens
    try:
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
    except Exception:
        logger.error(
            "Jobber token exchange failed: provider=jobber operation=token_exchange result=error"
        )
        raise HTTPException(status_code=502, detail="Failed to exchange code with Jobber") from None

    if resp.status_code != 200:
        logger.error(
            "Jobber token exchange failed: provider=jobber operation=token_exchange status_code=%s",
            resp.status_code,
        )
        raise HTTPException(status_code=502, detail="Failed to exchange code with Jobber") from None

    try:
        tokens = resp.json()
    except Exception:
        logger.error(
            "Jobber token response invalid: provider=jobber operation=token_exchange result=invalid_json"
        )
        raise HTTPException(status_code=502, detail="Invalid response from Jobber") from None

    if type(tokens) is not dict:
        logger.error(
            "Jobber token response invalid: provider=jobber operation=token_exchange result=invalid_type"
        )
        raise HTTPException(status_code=502, detail="Invalid response from Jobber") from None

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        logger.error(
            "Jobber token exchange returned no access token: provider=jobber operation=token_exchange"
        )
        raise HTTPException(status_code=502, detail="No access token in Jobber response") from None

    try:
        validate_token_string(access_token, name="access_token")
    except Exception:
        logger.error("Jobber token exchange returned malformed access token")
        raise HTTPException(status_code=502, detail="Malformed access token in Jobber response") from None

    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        logger.error(
            "Jobber token exchange returned no refresh token: provider=jobber operation=token_exchange"
        )
        raise HTTPException(status_code=502, detail="No refresh token in Jobber response") from None

    try:
        validate_token_string(refresh_token, name="refresh_token")
    except Exception:
        logger.error("Jobber token exchange returned malformed refresh token")
        raise HTTPException(status_code=502, detail="Malformed refresh token in Jobber response") from None

    expires_in = tokens.get("expires_in")
    expires_at = tokens.get("expires_at")
    try:
        if expires_in is not None:
            validate_token_expires_in(expires_in)
        if expires_at is not None:
            validate_token_expires_at(expires_at)
    except Exception:
        logger.error("Jobber token exchange returned invalid expiry values")
        raise HTTPException(status_code=502, detail="Invalid token expiry in Jobber response") from None

    try:
        updates, new_gen, audit_id = await connect_provider_cas(
            contractor_id=contractor_id,
            provider="jobber",
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            expires_at=expires_at,
            observed_generation=observed_generation,
            observed_access_raw=observed_access_raw,
            observed_refresh_raw=observed_refresh_raw,
            db=db,
        )
    except Exception:
        logger.error(
            "Jobber token persistence failed: provider=jobber operation=persist result=error"
        )
        raise HTTPException(status_code=500, detail="Failed to securely persist Jobber integration") from None

    logger.info("Jobber connected for contractor %s (generation=%s)", contractor_id[:8] or "unknown", new_gen)

    return HTMLResponse(_success_page("Jobber"))


# ── Status ───────────────────────────────────────────────────────────

@router.get("/jobber/status", dependencies=[Depends(verify_api_token)])
async def jobber_status(contractor_id: str = Query(...), request: Request = None):
    """Check whether a contractor has Jobber connected."""
    require_contractor_access(request, contractor_id)
    db = _get_firestore()
    doc = db.collection("contractors").document(contractor_id).get()
    if not getattr(doc, "exists", False):
        raise HTTPException(status_code=404, detail="Contractor not found")

    data = doc.to_dict() or {}
    connected = has_usable_token(data, "jobber", "access", contractor_id=contractor_id)

    return {
        "connected": connected,
        "connected_at": data.get("jobber_connected_at"),
        "lead_capture_enabled": connected and data.get("jobber_lead_capture_enabled") is True,
    }


# ── Lead capture toggle ──────────────────────────────────────────────

@router.post("/jobber/lead-capture", dependencies=[Depends(verify_api_token)])
async def jobber_update_lead_capture(
    body: JobberLeadCaptureUpdate,
    contractor_id: str = Query(...),
    request: Request = None,
):
    """Enable or disable Jobber Request lead capture for a connected contractor."""
    _require_admin(request)
    db = _get_firestore()
    doc_ref = db.collection("contractors").document(contractor_id)
    doc = doc_ref.get()
    if not getattr(doc, "exists", False):
        raise HTTPException(status_code=404, detail="Contractor not found")

    data = doc.to_dict() or {}
    connected = has_usable_token(data, "jobber", "access", contractor_id=contractor_id)

    if body.enabled and not connected:
        raise HTTPException(status_code=409, detail="Connect Jobber before enabling lead capture")

    previous_enabled = data.get("jobber_lead_capture_enabled") is True
    now = time.time()
    updates = {
        "jobber_lead_capture_enabled": body.enabled,
        "jobber_lead_capture_updated_at": now,
    }
    doc_ref.update(updates)
    await write_admin_audit_event(
        request=request,
        action="jobber_lead_capture_update",
        target_type="contractor",
        target_id=contractor_id,
        reason=body.reason or "admin toggled Jobber lead capture",
        before={"jobber_lead_capture_enabled": previous_enabled},
        after={"jobber_lead_capture_enabled": body.enabled},
        metadata={"jobber_connected": connected},
    )

    logger.info(
        "Jobber lead capture updated: contractor_id=%s enabled=%s",
        contractor_id[:8] or "unknown",
        body.enabled,
    )
    return {
        "status": "ok",
        "contractor_id": contractor_id,
        "connected": connected,
        "lead_capture_enabled": body.enabled,
        "updated_at": now,
    }


# ── Disconnect ───────────────────────────────────────────────────────

@router.post("/jobber/disconnect", dependencies=[Depends(verify_api_token)])
async def jobber_disconnect(contractor_id: str = Query(...), request: Request = None):
    """Revoke Jobber tokens and atomically remove from contractor doc."""
    from app.db.integration_lifecycle_audit import AUDIT_COLLECTION
    from app.services.integration_token_mutations import disconnect_provider_cas

    require_contractor_access(request, contractor_id)

    db = _get_firestore()

    # Durable CAS disconnect: advances generation, deletes fields, records audit event
    tombstone_gen, access_token, audit_id = await disconnect_provider_cas(
        contractor_id=contractor_id,
        provider="jobber",
        db=db,
    )

    # Best-effort revoke with Jobber (failure never prevents durable deletion)
    revocation_status = "pending"
    if not access_token:
        revocation_status = "not_attempted_unavailable_token"
    else:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.getjobber.com/api/oauth/revoke",
                    data={
                        "token": access_token,
                        "client_id": settings.jobber_client_id,
                        "client_secret": settings.jobber_client_secret,
                    },
                    timeout=5.0,
                )
            if resp.status_code == 200:
                revocation_status = "revoked_provider_confirmed"
            else:
                revocation_status = "revocation_rejected_provider"
        except Exception:
            revocation_status = "revocation_network_error"

    # Async patch revocation status to audit document and exact-postverify
    try:
        db.collection(AUDIT_COLLECTION).document(audit_id).update({
            "revocation_status": revocation_status,
            "revocation_completed_at": time.time(),
        })
        audit_snap = db.collection(AUDIT_COLLECTION).document(audit_id).get()
        if not getattr(audit_snap, "exists", False) or (audit_snap.to_dict() or {}).get("revocation_status") != revocation_status:
            logger.warning("Audit document revocation status postcondition mismatch: provider=jobber")
    except Exception:
        pass

    return {
        "status": "disconnected",
        "contractor_id": contractor_id,
        "generation": tombstone_gen,
        "revocation_status": revocation_status,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Google Calendar Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/google-calendar/connect", dependencies=[Depends(verify_api_token)])
async def google_calendar_connect(contractor_id: str = Query(...), request: Request = None):
    """Generate Google Calendar OAuth consent URL and store state in Firestore."""
    require_contractor_access(request, contractor_id)
    if not settings.google_calendar_client_id:
        raise HTTPException(
            status_code=500,
            detail="Google Calendar integration is not configured",
        )

    db = _get_firestore()

    # Generate a random state token, store in Firestore with 10-min TTL
    state = secrets.token_urlsafe(32)
    db.collection("google_oauth_states").document(state).set({
        "contractor_id": contractor_id,
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    })

    params = {
        "client_id": settings.google_calendar_client_id,
        "redirect_uri": GOOGLE_CALENDAR_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{GOOGLE_CALENDAR_AUTH_URL}?{urlencode(params)}"
    return {"authorize_url": url}


@router.get("/google-calendar/callback")
async def google_calendar_callback(code: str = Query(...), state: str = Query(...), request: Request = None):
    """Exchange authorization code for access + refresh tokens."""
    from app.services.integration_token_mutations import (
        connect_provider_cas,
        consume_oauth_state,
    )
    from app.services.integration_tokens import (
        MAX_KEY_VERSION,
        IntegrationTokenConfigError,
        IntegrationTokenDecryptionError,
        determine_write_format,
        is_encryption_configured,
    )

    if settings.integration_token_encrypted_writes_enabled and not is_encryption_configured():
        logger.error("Google Calendar OAuth callback aborted: integration token encryption is not configured")
        raise HTTPException(
            status_code=500,
            detail="Integration token encryption is not configured",
        ) from None

    db = _get_firestore()

    # Atomically validate and consume state (one-time use, deletes even if expired/malformed)
    state_data = await consume_oauth_state(
        db=db,
        collection_name="google_oauth_states",
        state=state,
    )
    raw_cid = state_data.get("contractor_id")
    try:
        contractor_id = validate_token_string(raw_cid, name="contractor_id")
        assert contractor_id is not None
    except Exception:
        logger.error("Google Calendar OAuth callback invalid contractor_id in state")
        raise HTTPException(status_code=400, detail="Invalid contractor in OAuth state") from None

    # Pre-exchange contractor precondition check
    contractor_ref = db.collection("contractors").document(contractor_id)
    contractor_doc = contractor_ref.get()
    if not getattr(contractor_doc, "exists", False):
        raise HTTPException(status_code=404, detail="Contractor not found") from None
    contractor_data = contractor_doc.to_dict() or {}
    if contractor_data.get("active") is not True:
        raise HTTPException(status_code=403, detail="Contractor account is inactive") from None

    raw_generation = contractor_data.get("google_calendar_generation")
    if raw_generation is None:
        observed_generation = 0
    elif type(raw_generation) is int and type(raw_generation) is not bool and 0 <= raw_generation <= MAX_KEY_VERSION:
        observed_generation = raw_generation
    else:
        logger.error("Google Calendar OAuth callback aborted: invalid stored generation")
        raise HTTPException(status_code=409, detail="Invalid contractor generation") from None

    observed_access_raw = contractor_data.get("google_calendar_access_token")
    observed_refresh_raw = contractor_data.get("google_calendar_refresh_token")

    # Validate that eventual write format is possible BEFORE exchanging the authorization code
    try:
        determine_write_format(
            contractor_id=contractor_id,
            provider="google_calendar",
            stored_access=observed_access_raw,
            stored_refresh=observed_refresh_raw,
            envelope_required=contractor_data.get("google_calendar_token_envelope_required"),
        )
    except (IntegrationTokenConfigError, IntegrationTokenDecryptionError) as exc:
        logger.error("Google Calendar OAuth callback aborted: integration token encryption unconfigured or historical key missing: %s", exc)
        raise HTTPException(status_code=500, detail="Integration token encryption configuration unavailable") from None
    except Exception as exc:
        logger.error("Google Calendar OAuth callback aborted: invalid stored credential state: %s", exc)
        raise HTTPException(status_code=409, detail="Contractor credentials in conflicted or malformed state") from None

    try:
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
    except Exception:
        logger.error(
            "Google token exchange failed: provider=google_calendar operation=token_exchange result=error"
        )
        raise HTTPException(status_code=502, detail="Failed to exchange code with Google") from None

    if resp.status_code != 200:
        logger.error(
            "Google token exchange failed: provider=google_calendar operation=token_exchange status_code=%s",
            resp.status_code,
        )
        raise HTTPException(status_code=502, detail="Failed to exchange code with Google") from None

    try:
        tokens = resp.json()
    except Exception:
        logger.error(
            "Google token response invalid: provider=google_calendar operation=token_exchange result=invalid_json"
        )
        raise HTTPException(status_code=502, detail="Invalid response from Google") from None

    if type(tokens) is not dict:
        logger.error(
            "Google token response invalid: provider=google_calendar operation=token_exchange result=invalid_type"
        )
        raise HTTPException(status_code=502, detail="Invalid response from Google") from None

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        logger.error(
            "Google token exchange returned no access token: provider=google_calendar operation=token_exchange"
        )
        raise HTTPException(status_code=502, detail="No access token in Google response") from None

    try:
        validate_token_string(access_token, name="access_token")
    except Exception:
        logger.error("Google token exchange returned malformed access token")
        raise HTTPException(status_code=502, detail="Malformed access token in Google response") from None

    if "refresh_token" in tokens:
        new_refresh_token = tokens["refresh_token"]
        try:
            validate_token_string(new_refresh_token, name="refresh_token")
            effective_refresh_token = new_refresh_token
        except Exception:
            logger.error("Google token exchange returned malformed refresh_token")
            raise HTTPException(status_code=502, detail="Malformed refresh token in Google response") from None
    else:
        # Fallback to existing stored refresh token only when refresh_token key is ABSENT
        existing_refresh_token = (
            safe_decrypt_integration_token(
                observed_refresh_raw,
                contractor_id=contractor_id,
                provider="google_calendar",
                token_kind="refresh",
            )
            if observed_refresh_raw
            else None
        )
        if not existing_refresh_token:
            logger.error(
                "Google token exchange returned no durable refresh credential: provider=google_calendar operation=token_exchange"
            )
            raise HTTPException(status_code=502, detail="Google did not provide offline access") from None
        effective_refresh_token = existing_refresh_token

    expires_in = tokens.get("expires_in")
    if expires_in is not None:
        try:
            validate_token_expires_in(expires_in)
        except Exception:
            logger.error("Google token exchange returned invalid expires_in")
            raise HTTPException(status_code=502, detail="Invalid token expiry in Google response") from None

    try:
        updates, new_gen, audit_id = await connect_provider_cas(
            contractor_id=contractor_id,
            provider="google_calendar",
            access_token=access_token,
            refresh_token=effective_refresh_token,
            expires_in=expires_in,
            scope=tokens.get("scope") or GOOGLE_CALENDAR_SCOPE,
            observed_generation=observed_generation,
            observed_access_raw=observed_access_raw,
            observed_refresh_raw=observed_refresh_raw,
            db=db,
        )
    except Exception:
        logger.error(
            "Google Calendar token persistence failed: provider=google_calendar operation=persist result=error"
        )
        raise HTTPException(status_code=500, detail="Failed to securely persist Google Calendar integration") from None

    logger.info("Google Calendar connected for contractor %s (generation=%s)", contractor_id[:8] or "unknown", new_gen)

    return HTMLResponse(_success_page("Google Calendar"))


# ── Status ───────────────────────────────────────────────────────────

@router.get("/google-calendar/status", dependencies=[Depends(verify_api_token)])
async def google_calendar_status(contractor_id: str = Query(...), request: Request = None):
    """Check whether a contractor has Google Calendar connected."""
    require_contractor_access(request, contractor_id)
    db = _get_firestore()
    doc = db.collection("contractors").document(contractor_id).get()
    if not getattr(doc, "exists", False):
        raise HTTPException(status_code=404, detail="Contractor not found")

    data = doc.to_dict() or {}
    connected = has_usable_token(data, "google_calendar", "access", contractor_id=contractor_id)

    return {
        "connected": connected,
        "connected_at": data.get("google_calendar_connected_at"),
    }


# ── Disconnect ───────────────────────────────────────────────────────

@router.post("/google-calendar/disconnect", dependencies=[Depends(verify_api_token)])
async def google_calendar_disconnect(contractor_id: str = Query(...), request: Request = None):
    """Revoke Google tokens and atomically remove from contractor doc."""
    from app.db.integration_lifecycle_audit import AUDIT_COLLECTION
    from app.services.integration_token_mutations import disconnect_provider_cas

    require_contractor_access(request, contractor_id)

    db = _get_firestore()

    # Durable CAS disconnect: advances generation, deletes fields, records audit event
    tombstone_gen, access_token, audit_id = await disconnect_provider_cas(
        contractor_id=contractor_id,
        provider="google_calendar",
        db=db,
    )

    # Best-effort revoke with Google (failure never prevents durable deletion)
    revocation_status = "pending"
    if not access_token:
        revocation_status = "not_attempted_unavailable_token"
    else:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": access_token},
                    timeout=5.0,
                )
            if resp.status_code in (200, 204):
                revocation_status = "succeeded"
            else:
                revocation_status = "provider_rejected"
        except Exception:
            revocation_status = "transport_error"

    # Async patch revocation status to audit document and exact-postverify
    try:
        db.collection(AUDIT_COLLECTION).document(audit_id).update({
            "revocation_status": revocation_status,
            "revocation_completed_at": time.time(),
        })
        audit_snap = db.collection(AUDIT_COLLECTION).document(audit_id).get()
        if not getattr(audit_snap, "exists", False) or (audit_snap.to_dict() or {}).get("revocation_status") != revocation_status:
            logger.warning("Audit document revocation status postcondition mismatch: provider=google_calendar")
    except Exception:
        pass

    logger.info("Google Calendar disconnected for contractor %s (generation=%s)", contractor_id[:8] or "unknown", tombstone_gen)
    return {"status": "disconnected"}
