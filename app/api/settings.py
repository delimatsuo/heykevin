"""User settings API."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.middleware.auth import verify_api_token
from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/settings", dependencies=[Depends(verify_api_token)])

# In-memory settings store for MVP (single user)
# Phase 9+ would move this to Firestore
_user_settings = {
    "greeting_name": settings.user_name,
    "quiet_hours_enabled": False,
    "quiet_hours_start": "22:00",
    "quiet_hours_end": "07:00",
    "quiet_hours_tz": "America/Los_Angeles",
    "text_reply_message": f"Can't talk right now. What's up? - {settings.user_name}",
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


@router.get("")
async def api_get_settings():
    """Get current settings."""
    return _user_settings


@router.put("")
async def api_update_settings(body: SettingsUpdate):
    """Update settings."""
    updates = {k: v for k, v in body.dict().items() if v is not None}
    _user_settings.update(updates)
    logger.info(f"Settings updated: {list(updates.keys())}")
    return _user_settings
