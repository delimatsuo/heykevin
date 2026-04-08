"""Global error handlers — the last line of defense. Never drop a call."""

from fastapi import Request
from fastapi.responses import Response

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

def get_fallback_twiml() -> str:
    """Construct fallback TwiML at runtime to avoid leaking phone in module-level string."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Please hold while I connect you.</Say>
    <Dial>{settings.user_phone}</Dial>
</Response>"""


def twiml_response(twiml: str, status_code: int = 200) -> Response:
    """Return a TwiML response with the correct content type."""
    return Response(content=twiml.strip(), media_type="application/xml", status_code=status_code)


def fallback_twiml_response() -> Response:
    """Emergency fallback — just forward to the user's phone. No DB, no lookups."""
    logger.error("Fallback triggered — forwarding call directly to user")
    return twiml_response(get_fallback_twiml())
