"""Kevin - AI Call Screening Assistant.

FastAPI application entry point.
"""

import signal
import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.utils.logging import setup_logging, get_logger
from app.webhooks.twilio_incoming import router as twilio_router
from app.webhooks.telegram_callback import router as telegram_router
from app.webhooks.media_stream import router as media_stream_router
from app.api.contacts import router as contacts_router
from app.api.calls import router as calls_router
from app.api.knowledge import router as knowledge_router
from app.api.settings import router as settings_router
from app.api.voip import router as voip_router

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
    allow_origins=[],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# Webhook routes
app.include_router(twilio_router)
app.include_router(telegram_router)
app.include_router(media_stream_router)
app.include_router(contacts_router)
app.include_router(calls_router)
app.include_router(knowledge_router)
app.include_router(settings_router)
app.include_router(voip_router)


@app.get("/health")
async def health():
    """Health check — returns minimal info only."""
    return {"status": "ok"}


@app.on_event("startup")
async def startup():
    logger.info(
        "Kevin starting up",
        extra={
            "environment": settings.environment,
            "twilio_number": settings.twilio_phone_number,
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
