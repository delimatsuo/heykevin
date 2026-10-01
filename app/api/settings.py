"""User settings API — per-contractor settings stored in Firestore."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator
from typing import Optional

from app.middleware.auth import verify_api_token, require_contractor_access
from app.db.firestore_client import get_firestore_client
from app.services.country_policy import (
    RECOGNIZED_COUNTRIES,
    SUPPORTED_COUNTRIES,
    is_recognized_country,
    resolve_service_binding,
    validate_phone_and_region,
    CountryPhoneMismatchError,
    InvalidPhoneError,
)
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
            if upper in RECOGNIZED_COUNTRIES:
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
    service_binding = None
    try:
        db = get_firestore_client()
        root_doc = db.collection("contractors").document(contractor_id).get()
        if root_doc.exists:
            root_data = root_doc.to_dict() or {}
            service_binding = resolve_service_binding(root_data)
            if service_binding and service_binding.get("country_code"):
                country_code = service_binding["country_code"]
            else:
                raw_cc = root_data.get("country_code")
                if isinstance(raw_cc, str) and is_recognized_country(raw_cc.strip().upper()):
                    country_code = raw_cc.strip().upper()
    except Exception as e:
        logger.error(f"Contractor root read failed for {contractor_id}: {e}", exc_info=True)

    # Merge with defaults so new fields are always present
    result = {**_DEFAULT_SETTINGS, **stored}
    # country_code and service_binding are root-authoritative
    result["country_code"] = country_code
    result["service_binding"] = service_binding
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

    db = get_firestore_client()
    root_doc_exists = False
    root_data = {}
    root_read_error = False
    try:
        root_doc = db.collection("contractors").document(contractor_id).get()
        if root_doc.exists:
            root_doc_exists = True
            root_data = root_doc.to_dict() or {}
    except Exception as e:
        root_read_error = True
        logger.error(f"Contractor root read before settings update failed for {contractor_id}: {e}", exc_info=True)

    # country_code lives on the main contractor document
    if "country_code" in updates:
        if root_read_error:
            raise HTTPException(status_code=503, detail="Failed to read contractor profile")
        if not root_doc_exists:
            raise HTTPException(status_code=404, detail="Contractor not found")

        cc = updates.pop("country_code")
        if cc and is_recognized_country(cc.upper()):
            req_cc = cc.upper()
            existing_number = root_data.get("twilio_number", "")
            if existing_number:
                binding = resolve_service_binding(root_data)
                assigned_country = binding.get("country_code") if binding else None
                locked_country = assigned_country or root_data.get("country_code")
                if locked_country and req_cc != locked_country:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "country_locked_to_number",
                            "message": "Cannot change country for an account with an assigned phone number",
                            "country_code": locked_country,
                        },
                    )
                elif not locked_country and req_cc != root_data.get("country_code"):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "country_locked_to_number",
                            "message": "Cannot change country for an account with an assigned phone number",
                            "country_code": root_data.get("country_code") or req_cc,
                        },
                    )
            else:
                owner_phone = (root_data.get("owner_phone") or "").strip()
                if owner_phone:
                    try:
                        _, derived_region = validate_phone_and_region(owner_phone, default_country=req_cc)
                        if derived_region and derived_region != req_cc:
                            raise HTTPException(
                                status_code=400,
                                detail={
                                    "code": "country_phone_mismatch",
                                    "message": "Phone number does not match specified country",
                                    "country_code": req_cc,
                                    "phone_region": derived_region,
                                },
                            )
                    except CountryPhoneMismatchError as e:
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "code": "country_phone_mismatch",
                                "message": "Phone number does not match specified country",
                                "country_code": e.country_code,
                                "phone_region": e.phone_region,
                            },
                        )
                    except InvalidPhoneError:
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "code": "invalid_owner_phone",
                                "message": "Invalid owner phone number",
                            },
                        )
            root_updates["country_code"] = req_cc
        elif cc:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported country code: {cc}",
            )

    # voice_engine lives on the main contractor document (not settings subcollection)
    if "voice_engine" in updates:
        ve = updates.pop("voice_engine")
        if ve in ("elevenlabs", "gemini"):
            root_updates["voice_engine"] = ve

    if root_updates or updates:
        try:
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
