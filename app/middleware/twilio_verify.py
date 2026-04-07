"""Twilio webhook signature validation middleware."""

from fastapi import Request, HTTPException
from twilio.request_validator import RequestValidator

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

_validator = RequestValidator(settings.twilio_auth_token)


async def verify_twilio_signature(request: Request):
    """Validate that a request actually came from Twilio.

    Raises HTTPException(403) if the signature is invalid.
    Used as a FastAPI dependency on Twilio webhook routes.
    """
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        logger.warning("Missing X-Twilio-Signature header")
        raise HTTPException(status_code=403, detail="Missing Twilio signature")

    # Reconstruct the full URL Twilio used to sign
    url = str(request.url)
    # Twilio signs against the original URL, which may use https even if behind a proxy
    if request.headers.get("X-Forwarded-Proto") == "https":
        url = url.replace("http://", "https://", 1)

    # Get form data (Twilio sends POST as application/x-www-form-urlencoded)
    form_data = await request.form()
    params = dict(form_data)

    if not _validator.validate(url, params, signature):
        logger.warning("Invalid Twilio signature", extra={"url": url})
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
