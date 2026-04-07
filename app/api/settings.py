"""User settings API — per-contractor settings stored in Firestore."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from typing import Optional

from app.middleware.auth import verify_api_token, require_contractor_access
from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/settings", dependencies=[Depends(verify_api_token)])

# Default settings applied when a contractor has no stored settings yet
_DEFAULT_SETTINGS = {
    "greeting_name": "",
    "quiet_hours_enabled": False,
    "quiet_hours_start": "22:00",
    "quiet_hours_end": "07:00",
    "quiet_hours_tz": "America/Los_Angeles",
    "text_reply_message": "Can't talk right now. What's up?",
    "escalation_enabled": False,
}


class SettingsUpdate(BaseModel):
    greeting_name: Optional[str] = None
    quiet_hours_enabled: Optional[bool] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    quiet_hours_tz: Optional[str] = None
    text_reply_message: Optional[str] = None
    escalation_enabled: Optional[bool] = None
    voice_engine: Optional[str] = None


def _settings_ref(contractor_id: str):
    """Return the Firestore document reference for a contractor's settings."""
    db = get_firestore_client()
    return db.collection("contractors").document(contractor_id).collection("settings").document("preferences")


async def _get_settings(contractor_id: str) -> dict:
    """Load settings from Firestore, falling back to defaults."""
    try:
        doc = _settings_ref(contractor_id).get()
        if doc.exists:
            stored = doc.to_dict()
            # Merge with defaults so new fields are always present
            return {**_DEFAULT_SETTINGS, **stored}
    except Exception as e:
        logger.error(f"Settings read failed for {contractor_id}: {e}", exc_info=True)
    return dict(_DEFAULT_SETTINGS)


@router.get("")
async def api_get_settings(request: Request, contractor_id: str = Query(..., description="Contractor ID")):
    """Get current settings for a contractor."""
    require_contractor_access(request, contractor_id)
    return await _get_settings(contractor_id)


@router.put("")
async def api_update_settings(request: Request, body: SettingsUpdate, contractor_id: str = Query(..., description="Contractor ID")):
    """Update settings for a contractor."""
    require_contractor_access(request, contractor_id)
    updates = {k: v for k, v in body.dict().items() if v is not None}

    # voice_engine lives on the main contractor document (not settings subcollection)
    if "voice_engine" in updates:
        ve = updates.pop("voice_engine")
        if ve in ("elevenlabs", "gemini"):
            try:
                db = get_firestore_client()
                db.collection("contractors").document(contractor_id).update({"voice_engine": ve})
            except Exception as e:
                logger.error(f"voice_engine update failed for {contractor_id}: {e}", exc_info=True)
                return {"error": "Failed to save voice_engine"}

    if updates:
        try:
            _settings_ref(contractor_id).set(updates, merge=True)
        except Exception as e:
            logger.error(f"Settings write failed for {contractor_id}: {e}", exc_info=True)
            return {"error": "Failed to save settings"}
    logger.info(f"Settings updated for {contractor_id}: {list(updates.keys())}")
    return await _get_settings(contractor_id)
