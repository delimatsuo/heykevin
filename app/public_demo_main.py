"""Minimal, isolated FastAPI entry point for Kevin's public phone demo."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.config import settings, validate_runtime_safety
from app.utils.error_handlers import twiml_response
from app.utils.logging import get_logger, setup_logging
from app.webhooks.public_demo import (
    _public_demo_unavailable_twiml,
    router,
    suppress_public_demo_sensitive_transport_logs,
)

setup_logging(settings.log_level)
suppress_public_demo_sensitive_transport_logs()
logger = get_logger(__name__)

PUBLIC_DEMO_INERT_USER_PHONE = "+12025550100"
PUBLIC_DEMO_INERT_TELEGRAM_BOT_TOKEN = "public-demo-disabled"


def _validate_public_demo_runtime() -> None:
    if (settings.environment or "").strip().lower() != "demo":
        raise RuntimeError("The public demo app requires ENVIRONMENT=demo")

    required = ["twilio_account_sid", "twilio_auth_token"]
    if settings.public_demo_enabled:
        required.append("gemini_api_key")
    missing = [name for name in required if not getattr(settings, name, None)]
    if missing:
        raise RuntimeError(f"Missing required demo config: {', '.join(missing)}")
    if settings.user_phone != PUBLIC_DEMO_INERT_USER_PHONE:
        raise RuntimeError(
            "USER_PHONE must be the reserved public-demo placeholder; never configure "
            "a real owner number on the demo service"
        )
    if settings.telegram_bot_token != PUBLIC_DEMO_INERT_TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN must be the inert public-demo placeholder"
        )

    validate_runtime_safety(public_demo_entrypoint=True)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _validate_public_demo_runtime()
    logger.info("Kevin public demo service starting")
    yield
    logger.info("Kevin public demo service stopping")


app = FastAPI(
    title="Kevin Public Demo",
    description="Fictional, no-side-effect AI receptionist demo",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "environment": settings.environment,
        "service": os.getenv("K_SERVICE", ""),
        "revision": os.getenv("K_REVISION", ""),
        "deploy_sha": os.getenv("DEPLOY_SHA", ""),
        "public_demo_enabled": bool(settings.public_demo_enabled),
    }


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> Response:
    logger.error(
        "public_demo event=unhandled_exception path=%s exception_type=%s",
        request.url.path,
        type(exc).__name__,
    )
    if request.url.path == "/webhooks/twilio/public-demo/usage-limit":
        # Usage Triggers retry on 5xx. Never disguise a circuit-breaker failure as
        # successful TwiML merely because it shares the Twilio webhook namespace.
        return JSONResponse(status_code=503, content={"detail": "Retry later"})
    if request.url.path.startswith("/webhooks/twilio/public-demo/"):
        return twiml_response(_public_demo_unavailable_twiml())
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
