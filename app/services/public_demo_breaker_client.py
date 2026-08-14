"""Narrow authenticated client for the IAM-private public-demo breaker."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from decimal import Decimal

import httpx

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/identity"
)
PUBLIC_DEMO_BREAKER_TOKEN_TIMEOUT_SECONDS = 2.0
PUBLIC_DEMO_BREAKER_REQUEST_TIMEOUT_SECONDS = 12.0


def _canonical_breaker_body(
    *,
    child_account_sid: str,
    usage_trigger_sid: str,
    trigger_value: Decimal,
    current_value: Decimal,
    date_fired: datetime,
    idempotency_token: str,
    shared_secret: str,
    caller_service_account: str,
) -> bytes:
    request_key = hmac.new(
        shared_secret.encode("utf-8"),
        f"usage-trigger:{idempotency_token}".encode(),
        hashlib.sha256,
    ).hexdigest()
    payload = {
        "action": "suspend_public_demo",
        "caller_service_account": caller_service_account,
        "child_account_sid": child_account_sid,
        "current_value": str(current_value),
        "date_fired": date_fired.isoformat(),
        "request_key": request_key,
        "trigger_value": str(trigger_value),
        "usage_trigger_sid": usage_trigger_sid,
        "version": 1,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _breaker_signature(body: bytes, shared_secret: str) -> str:
    digest = hmac.new(
        shared_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


async def _fetch_cloud_run_id_token(audience: str) -> str:
    timeout = httpx.Timeout(PUBLIC_DEMO_BREAKER_TOKEN_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.get(
            _METADATA_IDENTITY_URL,
            params={"audience": audience, "format": "full"},
            headers={"Metadata-Flavor": "Google"},
        )
    response.raise_for_status()
    token = response.text.strip()
    if not token:
        raise RuntimeError("Cloud Run identity endpoint returned an empty token")
    return token


async def trip_public_demo_breaker(
    *,
    child_account_sid: str,
    usage_trigger_sid: str,
    trigger_value: Decimal,
    current_value: Decimal,
    date_fired: datetime,
    idempotency_token: str,
) -> bool:
    """Request only the exact suspension action; return false on any uncertainty."""

    breaker_url = settings.public_demo_breaker_url.strip().rstrip("/")
    audience = settings.public_demo_breaker_audience.strip().rstrip("/")
    shared_secret = settings.public_demo_breaker_hmac_secret.strip()
    caller_identity = settings.public_demo_breaker_caller_service_account.strip()
    if (
        not breaker_url.startswith("https://")
        or audience != breaker_url
        or len(shared_secret) < 32
        or not caller_identity
    ):
        logger.error("public_demo event=breaker_request_failed reason=unsafe_config")
        return False

    body = _canonical_breaker_body(
        child_account_sid=child_account_sid,
        usage_trigger_sid=usage_trigger_sid,
        trigger_value=trigger_value,
        current_value=current_value,
        date_fired=date_fired,
        idempotency_token=idempotency_token,
        shared_secret=shared_secret,
        caller_service_account=caller_identity,
    )
    try:
        token = await _fetch_cloud_run_id_token(audience)
        timeout = httpx.Timeout(
            PUBLIC_DEMO_BREAKER_REQUEST_TIMEOUT_SECONDS,
            connect=PUBLIC_DEMO_BREAKER_TOKEN_TIMEOUT_SECONDS,
        )
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                f"{breaker_url}/v1/public-demo/suspend",
                content=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "X-Kevin-Breaker-Signature": _breaker_signature(
                        body,
                        shared_secret,
                    ),
                },
            )
        if response.status_code != 200:
            logger.error(
                "public_demo event=breaker_request_failed reason=downstream_status"
            )
            return False
        return response.json() == {"status": "suspended"}
    except Exception as error:  # noqa: BLE001 - callback must remain retryable
        logger.error(
            "public_demo event=breaker_request_failed reason=transport "
            "exception_type=%s",
            type(error).__name__,
        )
        return False
