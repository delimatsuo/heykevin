"""Kevin - AI Call Screening Assistant.

FastAPI application entry point.
"""

import signal
import asyncio

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.middleware.auth import verify_api_token
from app.utils.logging import setup_logging, get_logger, redact_phone
from app.webhooks.twilio_incoming import router as twilio_router
from app.webhooks.media_stream import router as media_stream_router
from app.api.contacts import router as contacts_router
from app.api.calls import router as calls_router
from app.api.knowledge import router as knowledge_router
from app.api.settings import router as settings_router
from app.api.voip import router as voip_router
from app.api.contractors import router as contractors_router
from app.api.vcard import router as vcard_router
from app.api.estimates import router as estimates_router
from app.api.integrations import router as integrations_router
from app.api.forwarding import router as forwarding_router
from app.api.subscription import router as subscription_router
from app.webhooks.appstore import router as appstore_router

# Initialize logging
setup_logging(settings.log_level)
logger = get_logger(__name__)

# Graceful shutdown flag
_shutting_down = False

app = FastAPI(
    title="Kevin",
    description="AI-powered call screening assistant",
    version="0.1.0",
    docs_url="/docs" if settings.environment == "development" else None,
    redoc_url=None,
)

# CORS — restrictive by default
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://heykevin.one"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Webhook routes
app.include_router(twilio_router)
app.include_router(media_stream_router)
app.include_router(contacts_router)
app.include_router(calls_router)
app.include_router(knowledge_router)
app.include_router(settings_router)
app.include_router(voip_router)
app.include_router(contractors_router)
app.include_router(vcard_router)
app.include_router(estimates_router)
app.include_router(integrations_router)
app.include_router(forwarding_router)
app.include_router(subscription_router)
app.include_router(appstore_router)


@app.get("/health")
async def health():
    """Health check — returns minimal info only."""
    return {"status": "ok"}


if settings.environment == "development":

    @app.delete("/debug/twilio-number/{phone}", dependencies=[Depends(verify_api_token)])
    async def debug_release_number(phone: str):
        """Debug: release an orphaned Twilio number."""
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        numbers = client.incoming_phone_numbers.list(phone_number=f"+{phone}", limit=1)
        if numbers:
            numbers[0].delete()
            return {"status": "released", "number": phone}
        return {"status": "not_found"}

    @app.get("/debug/twilio-calls", dependencies=[Depends(verify_api_token)])
    async def debug_twilio_calls():
        """Debug: check recent Twilio call history."""
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        calls = client.calls.list(limit=10)
        result = []
        for c in calls:
            result.append({
                "sid": c.sid,
                "from": c.from_formatted,
                "to": c.to_formatted,
                "status": c.status,
                "direction": c.direction,
                "start_time": str(c.start_time),
                "duration": c.duration,
            })
        return {"calls": result}

    @app.get("/debug/twilio-numbers", dependencies=[Depends(verify_api_token)])
    async def debug_twilio_numbers():
        """Debug: check Twilio number webhook configuration."""
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        numbers = client.incoming_phone_numbers.list(limit=20)
        result = []
        for n in numbers:
            result.append({
                "number": n.phone_number,
                "voice_url": n.voice_url,
                "voice_method": n.voice_method,
                "voice_application_sid": n.voice_application_sid,
                "voice_fallback_url": n.voice_fallback_url,
                "sms_url": n.sms_url,
                "status_callback": n.status_callback,
            })
        return {"numbers": result}


async def _orphan_call_cleanup():
    """Periodically clean up stale entries from RTDB /active_calls.

    Runs every 5 minutes. Deletes any entry where state_updated_at
    is more than 2 hours old.
    """
    import time
    from app.db.cache import _init_firebase, ACTIVE_CALLS_PATH

    MAX_AGE = 7200  # 2 hours in seconds

    while True:
        await asyncio.sleep(300)  # every 5 minutes
        try:
            _init_firebase()
            from firebase_admin import db as rtdb

            loop = asyncio.get_event_loop()
            ref = rtdb.reference(ACTIVE_CALLS_PATH)
            all_calls = await loop.run_in_executor(None, ref.get)

            if not all_calls or not isinstance(all_calls, dict):
                continue

            now = time.time()
            cleaned = 0
            for call_sid, call_data in all_calls.items():
                if not isinstance(call_data, dict):
                    continue
                updated_at = call_data.get("state_updated_at", 0)
                if updated_at and now - updated_at > MAX_AGE:
                    child_ref = rtdb.reference(f"{ACTIVE_CALLS_PATH}/{call_sid}")
                    await loop.run_in_executor(None, child_ref.delete)
                    cleaned += 1

            if cleaned:
                logger.info(f"Orphan cleanup: removed {cleaned} stale active call(s)")

        except Exception as e:
            logger.warning(f"Orphan call cleanup error: {e}")


@app.on_event("startup")
async def startup():
    # Validate required config
    required = ['twilio_account_sid', 'twilio_auth_token', 'anthropic_api_key',
                'deepgram_api_key', 'elevenlabs_api_key', 'api_bearer_token']
    missing = [k for k in required if not getattr(settings, k, None)]
    if missing:
        raise RuntimeError(f"Missing required config: {', '.join(missing)}")

    # Warn loudly if vapi_webhook_secret is not set in production
    if not settings.vapi_webhook_secret and settings.environment != "development":
        logger.critical("SECURITY WARNING: vapi_webhook_secret is not set — Vapi webhook is unauthenticated")

    # Start orphan call cleanup background task
    asyncio.create_task(_orphan_call_cleanup())

    logger.info(
        "Kevin starting up",
        extra={
            "environment": settings.environment,
            "twilio_number": redact_phone(settings.twilio_phone_number),
        },
    )


@app.on_event("shutdown")
async def shutdown():
    logger.info("Kevin shutting down — finishing in-flight requests")


def _handle_sigterm(*args):
    """Handle SIGTERM gracefully — stop accepting new requests."""
    global _shutting_down
    _shutting_down = True
    logger.info("SIGTERM received — initiating graceful shutdown")


signal.signal(signal.SIGTERM, _handle_sigterm)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
