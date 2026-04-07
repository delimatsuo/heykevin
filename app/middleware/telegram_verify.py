"""Telegram webhook secret token verification."""

from fastapi import Request, HTTPException

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


async def verify_telegram_secret(request: Request):
    """Validate that a webhook request came from Telegram.

    Checks the X-Telegram-Bot-Api-Secret-Token header against our configured secret.
    Used as a FastAPI dependency on Telegram webhook routes.
    """
    if not settings.telegram_webhook_secret:
        # No secret configured — skip validation (dev mode)
        return

    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if token != settings.telegram_webhook_secret:
        logger.warning("Invalid Telegram webhook secret")
        raise HTTPException(status_code=403, detail="Invalid Telegram secret")
