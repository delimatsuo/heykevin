"""OAuth integration endpoints for Jobber and Google Calendar."""

import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.middleware.auth import require_contractor_access, verify_api_token
from app.services.calendar import (
    CANONICAL_GOOGLE_CALENDAR_SCOPE,
    validate_and_normalize_google_calendar_scope,
)
from app.services.integration_tokens import (
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
GOOGLE_CALENDAR_SCOPE = CANONICAL_GOOGLE_CALENDAR_SCOPE
GOOGLE_CALENDAR_SCOPES = GOOGLE_CALENDAR_SCOPE.split(" ")


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

    @field_validator("enabled", mode="before")
    @classmethod
    def validate_exact_bool(cls, v: Any) -> bool:
        if type(v) is not bool:
            raise ValueError("enabled must be an exact boolean (true or false)")
        return v


# ── Connect (start OAuth flow) ──────────────────────────────────────

@router.get("/jobber/connect", dependencies=[Depends(verify_api_token)])
async def jobber_connect(contractor_id: str = Query(...), request: Request = None):
    """Generate a Jobber OAuth authorize URL for the contractor."""
    from app.services.integration_token_mutations import create_oauth_state

    require_contractor_access(request, contractor_id)
    if not settings.jobber_client_id:
        raise HTTPException(status_code=501, detail="Jobber integration not configured")

    state = secrets.token_urlsafe(32)

    # Store state → contractor mapping bound to lifecycle epoch, generation, and credentials fingerprint
    db = _get_firestore()
    await create_oauth_state(
        db=db,
        collection_name="jobber_oauth_states",
        state=state,
        contractor_id=contractor_id,
        provider="jobber",
        ttl_seconds=600.0,
    )

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
        terminalize_provider_operation_intent_cas,
        terminalize_provider_reauthorization_attempt_cas,
        transition_provider_operation_intent_to_started_cas,
        transition_provider_reauthorization_attempt_to_started_cas,
    )
    from app.services.integration_tokens import (
        IntegrationTokenConfigError,
        IntegrationTokenDecryptionError,
        determine_write_format,
        is_encryption_configured,
    )

    if settings.integration_token_encrypted_writes_enabled and not is_encryption_configured():
        logger.error("Jobber OAuth callback aborted: provider=jobber operation=encryption_check result=unconfigured")
        raise HTTPException(
            status_code=500,
            detail="Integration token encryption is not configured",
        ) from None

    db = _get_firestore()

    # Atomically validate and consume state (one-time use, deletes even if expired/malformed)
    state_data, contractor_obs = await consume_oauth_state(
        db=db,
        collection_name="jobber_oauth_states",
        state=state,
    )
    contractor_id = contractor_obs["contractor_id"]
    observed_generation = contractor_obs["generation"]
    observed_epoch = contractor_obs["lifecycle_epoch"]
    observed_access_raw = contractor_obs["observed_access_raw"]
    observed_refresh_raw = contractor_obs["observed_refresh_raw"]
    claim_id = contractor_obs.get("claim_id")
    is_quarantined = contractor_obs.get("is_quarantined", False)

    # Validate that eventual write format is possible BEFORE exchanging the authorization code
    try:
        determine_write_format(
            contractor_id=contractor_id,
            provider="jobber",
            stored_access=observed_access_raw,
            stored_refresh=observed_refresh_raw,
            envelope_required=contractor_obs["envelope_required"],
        )
    except (IntegrationTokenConfigError, IntegrationTokenDecryptionError):
        if claim_id:
            if is_quarantined:
                await terminalize_provider_reauthorization_attempt_cas(contractor_id=contractor_id, provider="jobber", claim_id=claim_id, db=db)
            else:
                await terminalize_provider_operation_intent_cas(contractor_id=contractor_id, provider="jobber", claim_id=claim_id, kind="connect", db=db)
        logger.error("Jobber OAuth callback aborted: provider=jobber operation=write_format_check result=unconfigured")
        raise HTTPException(status_code=500, detail="Integration token encryption configuration unavailable") from None
    except Exception:
        if claim_id:
            if is_quarantined:
                await terminalize_provider_reauthorization_attempt_cas(contractor_id=contractor_id, provider="jobber", claim_id=claim_id, db=db)
            else:
                await terminalize_provider_operation_intent_cas(contractor_id=contractor_id, provider="jobber", claim_id=claim_id, kind="connect", db=db)
        logger.error("Jobber OAuth callback aborted: provider=jobber operation=write_format_check result=invalid_credential_state")
        raise HTTPException(status_code=409, detail="Contractor credentials in conflicted or malformed state") from None

    # Transition connect/reconnect intent to started immediately before provider token exchange
    if claim_id:
        try:
            if is_quarantined:
                await transition_provider_reauthorization_attempt_to_started_cas(
                    contractor_id=contractor_id,
                    provider="jobber",
                    claim_id=claim_id,
                    observed_generation=observed_generation,
                    observed_lifecycle_epoch=observed_epoch,
                    observed_access_raw=observed_access_raw,
                    observed_refresh_raw=observed_refresh_raw,
                    db=db,
                )
            else:
                await transition_provider_operation_intent_to_started_cas(
                    contractor_id=contractor_id,
                    provider="jobber",
                    claim_id=claim_id,
                    kind="connect",
                    observed_generation=observed_generation,
                    observed_lifecycle_epoch=observed_epoch,
                    observed_access_raw=observed_access_raw,
                    observed_refresh_raw=observed_refresh_raw,
                    db=db,
                )
        except Exception:
            logger.error("Jobber OAuth callback aborted: provider=jobber operation=transition_started result=lock_failed")
            raise HTTPException(status_code=409, detail="Failed to acquire live connect lock") from None

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
        # Provider ambiguity (timeout / connection / network failure): do NOT terminalize intent; retain claim-bound started intent
        logger.error(
            "Jobber token exchange failed: provider=jobber operation=token_exchange result=error"
        )
        raise HTTPException(status_code=502, detail="Failed to exchange code with Jobber") from None

    if resp.status_code != 200:
        if resp.status_code == 400 and claim_id:
            # 400 Bad Request is an explicit terminal rejection proving authorization code was invalid / not exchanged
            if is_quarantined:
                await terminalize_provider_reauthorization_attempt_cas(contractor_id=contractor_id, provider="jobber", claim_id=claim_id, db=db)
            else:
                await terminalize_provider_operation_intent_cas(contractor_id=contractor_id, provider="jobber", claim_id=claim_id, kind="connect", db=db)
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
    if type(access_token) is not str or len(access_token) == 0:
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
    if type(refresh_token) is not str or len(refresh_token) == 0:
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
            claim_id=claim_id,
            expires_in=expires_in,
            expires_at=expires_at,
            observed_generation=observed_generation,
            observed_lifecycle_epoch=observed_epoch,
            observed_access_raw=observed_access_raw,
            observed_refresh_raw=observed_refresh_raw,
            db=db,
        )
    except Exception:
        logger.error(
            "Jobber token persistence failed: provider=jobber operation=persist result=error"
        )
        raise HTTPException(status_code=500, detail="Failed to securely persist Jobber integration") from None

    logger.info("Jobber connected successfully: provider=jobber operation=connect result=success generation=%s", new_gen)

    return HTMLResponse(_success_page("Jobber"))


# ── Status ───────────────────────────────────────────────────────────

@router.get("/jobber/status", dependencies=[Depends(verify_api_token)])
async def jobber_status(contractor_id: str = Query(...), request: Request = None):
    """Check whether a contractor has Jobber connected."""
    from app.services.integration_token_mutations import (
        extract_safe_connected_at,
        is_durable_provider_connected,
    )

    require_contractor_access(request, contractor_id)
    db = _get_firestore()
    doc = db.collection("contractors").document(contractor_id).get()
    if not getattr(doc, "exists", False):
        raise HTTPException(status_code=404, detail="Contractor not found")

    data = doc.to_dict()
    if type(data) is not dict:
        raise HTTPException(status_code=500, detail="Contractor document is not an exact dict")
    connected = is_durable_provider_connected(data, "jobber", contractor_id=contractor_id)
    connected_at = extract_safe_connected_at(data, "jobber") if connected else None

    return {
        "connected": connected,
        "connected_at": connected_at,
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
    from app.services.integration_token_mutations import (
        IntegrationTokenCASConflict,
        IntegrationTokenContractorNotFound,
        update_jobber_lead_capture_cas,
    )

    db = _get_firestore()
    req_meta = {}
    if request is not None:
        from app.db.admin_audit import _client_ip_hash
        req_meta["ip_hash"] = _client_ip_hash(request)
        headers = getattr(request, "headers", {}) or {}
        if isinstance(headers, dict):
            req_meta["user_agent"] = headers.get("user-agent", "")

    try:
        result = await update_jobber_lead_capture_cas(
            contractor_id=contractor_id,
            enabled=body.enabled,
            actor="global_admin_token",
            reason=body.reason or "admin toggled Jobber lead capture",
            request_metadata=req_meta,
            db=db,
        )
    except IntegrationTokenContractorNotFound:
        raise HTTPException(status_code=404, detail="Contractor not found") from None
    except IntegrationTokenCASConflict:
        raise HTTPException(status_code=409, detail="Jobber lead capture update conflict") from None
    except HTTPException:
        raise
    except Exception:
        logger.error("Failed to update Jobber lead capture: provider=jobber operation=lead_capture result=error")
        raise HTTPException(status_code=500, detail="Failed to update Jobber lead capture") from None

    logger.info("Jobber lead capture updated: provider=jobber operation=lead_capture result=success")
    return {
        "status": "ok",
        "contractor_id": result.contractor_id,
        "connected": result.connected,
        "lead_capture_enabled": result.enabled,
        "updated_at": result.updated_at,
    }


# ── Disconnect ───────────────────────────────────────────────────────

@router.post("/jobber/disconnect", dependencies=[Depends(verify_api_token)])
async def jobber_disconnect(contractor_id: str = Query(...), request: Request = None):
    """Revoke Jobber tokens and atomically remove from contractor doc."""
    from app.services.integration_token_mutations import (
        IntegrationTokenCASConflict,
        IntegrationTokenContractorNotFound,
        IntegrationTokenError,
        disconnect_and_revoke_provider_orchestration,
    )

    require_contractor_access(request, contractor_id)

    try:
        db = _get_firestore()
        result = await disconnect_and_revoke_provider_orchestration(
            contractor_id=contractor_id,
            provider="jobber",
            db=db,
        )
    except IntegrationTokenContractorNotFound:
        logger.warning("Provider disconnect failed: provider=jobber operation=disconnect result=contractor_not_found")
        raise HTTPException(status_code=404, detail="Contractor not found") from None
    except (IntegrationTokenCASConflict, IntegrationTokenError):
        logger.warning("Provider disconnect failed: provider=jobber operation=disconnect result=conflict")
        raise HTTPException(status_code=409, detail="Integration transaction conflict") from None
    except Exception:
        logger.error("Provider disconnect failed: provider=jobber operation=disconnect result=internal_error")
        raise HTTPException(status_code=500, detail="Internal server error") from None

    logger.info("Provider disconnected: provider=jobber operation=disconnect result=success")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Google Calendar Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/google-calendar/connect", dependencies=[Depends(verify_api_token)])
async def google_calendar_connect(contractor_id: str = Query(...), request: Request = None):
    """Generate Google Calendar OAuth consent URL and store state in Firestore."""
    from app.services.integration_token_mutations import create_oauth_state

    require_contractor_access(request, contractor_id)
    if not settings.google_calendar_client_id:
        raise HTTPException(
            status_code=500,
            detail="Google Calendar integration is not configured",
        )

    db = _get_firestore()

    # Generate a random state token, store in Firestore bound to lifecycle epoch, generation, and credentials fingerprint
    state = secrets.token_urlsafe(32)
    await create_oauth_state(
        db=db,
        collection_name="google_oauth_states",
        state=state,
        contractor_id=contractor_id,
        provider="google_calendar",
        ttl_seconds=600.0,
    )

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
        terminalize_provider_operation_intent_cas,
        terminalize_provider_reauthorization_attempt_cas,
        transition_provider_operation_intent_to_started_cas,
        transition_provider_reauthorization_attempt_to_started_cas,
    )
    from app.services.integration_tokens import (
        IntegrationTokenConfigError,
        IntegrationTokenDecryptionError,
        determine_write_format,
        is_encryption_configured,
    )

    if settings.integration_token_encrypted_writes_enabled and not is_encryption_configured():
        logger.error("Google Calendar OAuth callback aborted: provider=google_calendar operation=encryption_check result=unconfigured")
        raise HTTPException(
            status_code=500,
            detail="Integration token encryption is not configured",
        ) from None

    db = _get_firestore()

    # Atomically validate and consume state (one-time use, deletes even if expired/malformed)
    state_data, contractor_obs = await consume_oauth_state(
        db=db,
        collection_name="google_oauth_states",
        state=state,
    )
    contractor_id = contractor_obs["contractor_id"]
    observed_generation = contractor_obs["generation"]
    observed_epoch = contractor_obs["lifecycle_epoch"]
    observed_access_raw = contractor_obs["observed_access_raw"]
    observed_refresh_raw = contractor_obs["observed_refresh_raw"]
    claim_id = contractor_obs.get("claim_id")
    is_quarantined = contractor_obs.get("is_quarantined", False)

    # Validate that eventual write format is possible BEFORE exchanging the authorization code
    try:
        determine_write_format(
            contractor_id=contractor_id,
            provider="google_calendar",
            stored_access=observed_access_raw,
            stored_refresh=observed_refresh_raw,
            envelope_required=contractor_obs["envelope_required"],
        )
    except (IntegrationTokenConfigError, IntegrationTokenDecryptionError):
        if claim_id:
            if is_quarantined:
                await terminalize_provider_reauthorization_attempt_cas(contractor_id=contractor_id, provider="google_calendar", claim_id=claim_id, db=db)
            else:
                await terminalize_provider_operation_intent_cas(contractor_id=contractor_id, provider="google_calendar", claim_id=claim_id, kind="connect", db=db)
        logger.error("Google Calendar OAuth callback aborted: provider=google_calendar operation=write_format_check result=unconfigured")
        raise HTTPException(status_code=500, detail="Integration token encryption configuration unavailable") from None
    except Exception:
        if claim_id:
            if is_quarantined:
                await terminalize_provider_reauthorization_attempt_cas(contractor_id=contractor_id, provider="google_calendar", claim_id=claim_id, db=db)
            else:
                await terminalize_provider_operation_intent_cas(contractor_id=contractor_id, provider="google_calendar", claim_id=claim_id, kind="connect", db=db)
        logger.error("Google Calendar OAuth callback aborted: provider=google_calendar operation=write_format_check result=invalid_credential_state")
        raise HTTPException(status_code=409, detail="Contractor credentials in conflicted or malformed state") from None

    # Transition connect/reconnect intent to started immediately before provider token exchange
    if claim_id:
        try:
            if is_quarantined:
                await transition_provider_reauthorization_attempt_to_started_cas(
                    contractor_id=contractor_id,
                    provider="google_calendar",
                    claim_id=claim_id,
                    observed_generation=observed_generation,
                    observed_lifecycle_epoch=observed_epoch,
                    observed_access_raw=observed_access_raw,
                    observed_refresh_raw=observed_refresh_raw,
                    db=db,
                )
            else:
                await transition_provider_operation_intent_to_started_cas(
                    contractor_id=contractor_id,
                    provider="google_calendar",
                    claim_id=claim_id,
                    kind="connect",
                    observed_generation=observed_generation,
                    observed_lifecycle_epoch=observed_epoch,
                    observed_access_raw=observed_access_raw,
                    observed_refresh_raw=observed_refresh_raw,
                    db=db,
                )
        except Exception:
            logger.error("Google Calendar OAuth callback aborted: provider=google_calendar operation=transition_started result=lock_failed")
            raise HTTPException(status_code=409, detail="Failed to acquire live connect lock") from None

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
        # Provider ambiguity (timeout / connection / network failure): do NOT terminalize intent; retain claim-bound started intent
        logger.error(
            "Google token exchange failed: provider=google_calendar operation=token_exchange result=error"
        )
        raise HTTPException(status_code=502, detail="Failed to exchange code with Google") from None

    if resp.status_code != 200:
        if resp.status_code == 400 and claim_id:
            # 400 Bad Request is an explicit terminal rejection proving authorization code was invalid / not exchanged
            if is_quarantined:
                await terminalize_provider_reauthorization_attempt_cas(contractor_id=contractor_id, provider="google_calendar", claim_id=claim_id, db=db)
            else:
                await terminalize_provider_operation_intent_cas(contractor_id=contractor_id, provider="google_calendar", claim_id=claim_id, kind="connect", db=db)
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
    if type(access_token) is not str or len(access_token) == 0:
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
        if type(new_refresh_token) is not str or len(new_refresh_token) == 0:
            logger.error("Google token exchange returned malformed refresh_token")
            raise HTTPException(status_code=502, detail="Malformed refresh token in Google response") from None
        try:
            validate_token_string(new_refresh_token, name="refresh_token")
            effective_refresh_token = new_refresh_token
        except Exception:
            logger.error("Google token exchange returned malformed refresh_token")
            raise HTTPException(status_code=502, detail="Malformed refresh token in Google response") from None
    else:
        if is_quarantined:
            logger.error(
                "Google Calendar OAuth callback failed: provider=google_calendar operation=quarantine_recovery result=missing_fresh_refresh_token"
            )
            raise HTTPException(status_code=502, detail="Missing fresh refresh token in Google response during quarantine recovery") from None
        # Fallback to existing stored refresh token only when refresh_token key is ABSENT on non-quarantined contractor
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

    if "scope" in tokens:
        scope_raw = tokens["scope"]
        valid_scope_ok, effective_scope = validate_and_normalize_google_calendar_scope(
            scope_raw,
            allow_none=False,
        )
        if not valid_scope_ok or effective_scope is None or type(effective_scope) is not str:
            logger.error("Google token exchange returned invalid or reduced scope")
            raise HTTPException(status_code=400, detail="Invalid or reduced scope in Google response")
    else:
        effective_scope = CANONICAL_GOOGLE_CALENDAR_SCOPE

    try:
        updates, new_gen, audit_id = await connect_provider_cas(
            contractor_id=contractor_id,
            provider="google_calendar",
            access_token=access_token,
            refresh_token=effective_refresh_token,
            claim_id=claim_id,
            expires_in=expires_in,
            scope=effective_scope,
            observed_generation=observed_generation,
            observed_lifecycle_epoch=observed_epoch,
            observed_access_raw=observed_access_raw,
            observed_refresh_raw=observed_refresh_raw,
            db=db,
        )
    except Exception:
        logger.error(
            "Google Calendar token persistence failed: provider=google_calendar operation=persist result=error"
        )
        raise HTTPException(status_code=500, detail="Failed to securely persist Google Calendar integration") from None

    logger.info("Google Calendar connected successfully: provider=google_calendar operation=connect result=success generation=%s", new_gen)

    return HTMLResponse(_success_page("Google Calendar"))


# ── Status ───────────────────────────────────────────────────────────

@router.get("/google-calendar/status", dependencies=[Depends(verify_api_token)])
async def google_calendar_status(contractor_id: str = Query(...), request: Request = None):
    """Check whether a contractor has Google Calendar connected."""
    from app.services.integration_token_mutations import (
        extract_safe_connected_at,
        is_durable_provider_connected,
    )

    require_contractor_access(request, contractor_id)
    db = _get_firestore()
    doc = db.collection("contractors").document(contractor_id).get()
    if not getattr(doc, "exists", False):
        raise HTTPException(status_code=404, detail="Contractor not found")

    data = doc.to_dict()
    if type(data) is not dict:
        raise HTTPException(status_code=500, detail="Contractor document is not an exact dict")
    connected = is_durable_provider_connected(data, "google_calendar", contractor_id=contractor_id)
    connected_at = extract_safe_connected_at(data, "google_calendar") if connected else None

    return {
        "connected": connected,
        "connected_at": connected_at,
    }


# ── Disconnect ───────────────────────────────────────────────────────

@router.post("/google-calendar/disconnect", dependencies=[Depends(verify_api_token)])
async def google_calendar_disconnect(contractor_id: str = Query(...), request: Request = None):
    """Revoke Google tokens and atomically remove from contractor doc."""
    from app.services.integration_token_mutations import (
        IntegrationTokenCASConflict,
        IntegrationTokenContractorNotFound,
        IntegrationTokenError,
        disconnect_and_revoke_provider_orchestration,
    )

    require_contractor_access(request, contractor_id)

    try:
        db = _get_firestore()
        result = await disconnect_and_revoke_provider_orchestration(
            contractor_id=contractor_id,
            provider="google_calendar",
            db=db,
        )
    except IntegrationTokenContractorNotFound:
        logger.warning("Provider disconnect failed: provider=google_calendar operation=disconnect result=contractor_not_found")
        raise HTTPException(status_code=404, detail="Contractor not found") from None
    except (IntegrationTokenCASConflict, IntegrationTokenError):
        logger.warning("Provider disconnect failed: provider=google_calendar operation=disconnect result=conflict")
        raise HTTPException(status_code=409, detail="Integration transaction conflict") from None
    except Exception:
        logger.error("Provider disconnect failed: provider=google_calendar operation=disconnect result=internal_error")
        raise HTTPException(status_code=500, detail="Internal server error") from None

    logger.info("Provider disconnected: provider=google_calendar operation=disconnect result=success")
    return result
