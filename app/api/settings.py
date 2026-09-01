"""User settings API — per-contractor settings stored in Firestore."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, field_validator
from typing import Optional

from app.middleware.auth import verify_api_token, require_contractor_access
from app.db.firestore_client import get_firestore_client
from app.db.contractors import SUPPORTED_COUNTRIES
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
    country_code: Optional[str] = None

    @field_validator("country_code")
    @classmethod
    def validate_country_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str):
            trimmed = v.strip()
            if not trimmed:
                return None
            upper = trimmed.upper()
            if upper in SUPPORTED_COUNTRIES:
                return upper
            raise ValueError(f"Unsupported country code: {v}")
        raise ValueError(f"Unsupported country code: {v}")


def _settings_ref(contractor_id: str):
    """Return the Firestore document reference for a contractor's settings."""
    db = get_firestore_client()
    return (
        db.collection("contractors")
        .document(contractor_id)
        .collection("settings")
        .document("preferences")
    )


async def _get_settings(contractor_id: str) -> dict:
    """Load settings from Firestore, falling back to defaults."""
    stored = {}
    try:
        doc = _settings_ref(contractor_id).get()
        if doc.exists:
            stored = doc.to_dict() or {}
    except Exception as e:
        logger.error(f"Settings read failed for {contractor_id}: {e}", exc_info=True)

    country_code = "US"
    try:
        db = get_firestore_client()
        root_doc = db.collection("contractors").document(contractor_id).get()
        if root_doc.exists:
            root_data = root_doc.to_dict() or {}
            raw_cc = root_data.get("country_code")
            if isinstance(raw_cc, str) and raw_cc.strip().upper() in SUPPORTED_COUNTRIES:
                country_code = raw_cc.strip().upper()
    except Exception as e:
        logger.error(f"Contractor root read failed for {contractor_id}: {e}", exc_info=True)

    # Merge with defaults so new fields are always present
    result = {**_DEFAULT_SETTINGS, **stored}
    # country_code is root-authoritative; stored preferences must not override it
    result["country_code"] = country_code
    return result


@router.get("")
async def api_get_settings(
    request: Request, contractor_id: str = Query(..., description="Contractor ID")
):
    """Get current settings for a contractor."""
    require_contractor_access(request, contractor_id)
    return await _get_settings(contractor_id)


@router.put("")
async def api_update_settings(
    request: Request,
    body: SettingsUpdate,
    contractor_id: str = Query(..., description="Contractor ID"),
):
    """Update settings for a contractor."""
    require_contractor_access(request, contractor_id)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    root_updates = {}

    # country_code lives on the main contractor document
    if "country_code" in updates:
        cc = updates.pop("country_code")
        if cc and cc.upper() in SUPPORTED_COUNTRIES:
            root_updates["country_code"] = cc.upper()
        elif cc:
            return {"error": f"Unsupported country code: {cc}"}

    # voice_engine lives on the main contractor document (not settings subcollection)
    if "voice_engine" in updates:
        ve = updates.pop("voice_engine")
        if ve in ("elevenlabs", "gemini"):
            root_updates["voice_engine"] = ve

    if root_updates or updates:
        try:
            db = get_firestore_client()
            root_ref = db.collection("contractors").document(contractor_id)
            if root_updates and updates:
                batch = db.batch()
                batch.update(root_ref, root_updates)
                batch.set(
                    root_ref.collection("settings").document("preferences"),
                    updates,
                    merge=True,
                )
                batch.commit()
            elif root_updates:
                root_ref.update(root_updates)
            else:
                root_ref.collection("settings").document("preferences").set(updates, merge=True)
        except Exception as e:
            logger.error(f"Settings write failed for {contractor_id}: {e}", exc_info=True)
            if not updates and set(root_updates) == {"country_code"}:
                return {"error": "Failed to save country_code"}
            if not updates and set(root_updates) == {"voice_engine"}:
                return {"error": "Failed to save voice_engine"}
            return {"error": "Failed to save settings"}
    logger.info(
        f"Settings updated for {contractor_id}: {list(root_updates.keys()) + list(updates.keys())}"
    )
    return await _get_settings(contractor_id)
