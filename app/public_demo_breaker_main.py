"""IAM-private entry point holding the public demo breaker's parent authority."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.services.public_demo_breaker import (
    breaker_settings,
    suppress_breaker_transport_logs,
    trip_twilio_parent_breaker,
    validate_public_demo_breaker_runtime,
)
from app.utils.logging import get_logger, setup_logging

setup_logging(breaker_settings.log_level)
suppress_breaker_transport_logs()
logger = get_logger(__name__)

_EXPECTED_PAYLOAD_KEYS = frozenset(
    {
        "action",
        "caller_service_account",
        "child_account_sid",
        "current_value",
        "date_fired",
        "request_key",
        "trigger_value",
        "usage_trigger_sid",
        "version",
    }
)
_MAX_BODY_BYTES = 4096
_MAX_CALLBACK_AGE_SECONDS = 900


def _callback_age_is_fresh(callback_age: float) -> bool:
    """Allow limited clock skew but reject the exact 15-minute replay boundary."""

    return -60 <= callback_age < _MAX_CALLBACK_AGE_SECONDS


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    validate_public_demo_breaker_runtime()
    logger.info("public_demo_breaker event=starting")
    yield
    logger.info("public_demo_breaker event=stopping")


app = FastAPI(
    title="Kevin Public Demo Breaker",
    description="Private fail-closed circuit breaker",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


def _has_cloud_run_authorization(request: Request) -> bool:
    authorization = request.headers.get("Authorization", "")
    try:
        scheme, token = authorization.split(" ", 1)
    except ValueError:
        return False
    # Cloud Run IAM is the authoritative, external principal check. This local
    # bearer-shape check is defense in depth only; it does not authenticate the
    # caller independently of IAM.
    return scheme.lower() == "bearer" and token.count(".") == 2 and len(token) >= 16


def _valid_request_signature(body: bytes, received: str) -> bool:
    secret = breaker_settings.public_demo_breaker_hmac_secret.strip()
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(received, expected)


def _payload_is_exact_and_fresh(payload: object) -> bool:
    if not isinstance(payload, dict) or set(payload) != _EXPECTED_PAYLOAD_KEYS:
        return False
    try:
        fired_at = datetime.fromisoformat(str(payload["date_fired"]))
        if fired_at.tzinfo is None:
            return False
        fired_at = fired_at.astimezone(UTC)
        callback_age = (datetime.now(UTC) - fired_at).total_seconds()
        trigger_value = Decimal(str(payload["trigger_value"]))
        current_value = Decimal(str(payload["current_value"]))
        configured_limit = Decimal(
            str(breaker_settings.public_demo_twilio_daily_spend_limit_usd)
        )
    except (InvalidOperation, TypeError, ValueError):
        return False
    request_key = str(payload["request_key"])
    return (
        type(payload["version"]) is int
        and payload["version"] == 1
        and payload["action"] == "suspend_public_demo"
        and payload["caller_service_account"]
        == breaker_settings.public_demo_breaker_caller_service_account.strip()
        and payload["child_account_sid"]
        == breaker_settings.public_demo_breaker_twilio_child_account_sid.strip()
        and payload["usage_trigger_sid"]
        == breaker_settings.public_demo_twilio_usage_trigger_sid.strip()
        and trigger_value.is_finite()
        and current_value.is_finite()
        and trigger_value == configured_limit
        and current_value >= configured_limit
        and len(request_key) == 64
        and all(character in "0123456789abcdef" for character in request_key)
        and _callback_age_is_fresh(callback_age)
    )


@app.post("/v1/public-demo/suspend")
async def suspend_public_demo(request: Request) -> JSONResponse:
    """Accept only an IAM-authenticated, signed request for the exact child."""

    if not _has_cloud_run_authorization(request):
        logger.warning("public_demo_breaker event=request_rejected reason=identity")
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES or not _valid_request_signature(
        body,
        request.headers.get("X-Kevin-Breaker-Signature", ""),
    ):
        logger.warning("public_demo_breaker event=request_rejected reason=signature")
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("public_demo_breaker event=request_rejected reason=payload")
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    if not _payload_is_exact_and_fresh(payload):
        logger.warning("public_demo_breaker event=request_rejected reason=binding")
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})

    if not await trip_twilio_parent_breaker():
        return JSONResponse(status_code=503, content={"detail": "Retry later"})
    return JSONResponse(status_code=200, content={"status": "suspended"})


@app.exception_handler(Exception)
async def unhandled_breaker_exception(_request: Request, error: Exception) -> JSONResponse:
    logger.error(
        "public_demo_breaker event=unhandled_exception exception_type=%s",
        type(error).__name__,
    )
    return JSONResponse(status_code=503, content={"detail": "Retry later"})
