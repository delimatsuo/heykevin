"""Parent-authority Twilio breaker used only by the private breaker runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import re

from app.utils.logging import get_logger
from app.utils.pydantic_settings import DotenvProtectedBaseSettings

logger = get_logger(__name__)

_ACCOUNT_SID_RE = re.compile(r"AC[0-9a-fA-F]{32}\Z")
_API_KEY_SID_RE = re.compile(r"SK[0-9a-fA-F]{32}\Z")
_TRIGGER_SID_RE = re.compile(r"UT[0-9a-fA-F]{32}\Z")
_FORBIDDEN_BREAKER_ENV = (
    "TWILIO_AUTH_TOKEN",
    "GEMINI_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "ANTHROPIC_API_KEY",
    "USER_PHONE",
    "TELEGRAM_BOT_TOKEN",
)

PUBLIC_DEMO_BREAKER_TWILIO_HTTP_TIMEOUT_SECONDS = 2.0
PUBLIC_DEMO_BREAKER_SUSPEND_TIMEOUT_SECONDS = 3.0


class PublicDemoBreakerSettings(DotenvProtectedBaseSettings):
    """Minimal settings surface for the private, parent-authority runtime."""

    environment: str = ""
    log_level: str = "INFO"
    public_demo_breaker_audience: str = ""
    public_demo_breaker_hmac_secret: str = ""
    public_demo_breaker_caller_service_account: str = ""
    public_demo_twilio_usage_trigger_sid: str = ""
    public_demo_twilio_daily_spend_limit_usd: float = 0.0
    public_demo_breaker_twilio_parent_account_sid: str = ""
    public_demo_breaker_twilio_parent_main_api_key_sid: str = ""
    public_demo_breaker_twilio_parent_main_api_key_secret: str = ""
    public_demo_breaker_twilio_child_account_sid: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


def get_breaker_settings() -> PublicDemoBreakerSettings:
    return PublicDemoBreakerSettings()


breaker_settings = get_breaker_settings()


def validate_public_demo_breaker_runtime(
    configured: PublicDemoBreakerSettings = breaker_settings,
) -> None:
    errors: list[str] = []
    if configured.environment.strip().lower() != "demo-breaker":
        errors.append("ENVIRONMENT must be exactly demo-breaker")
    if configured.log_level.strip().upper() not in {"INFO", "WARNING"}:
        errors.append("LOG_LEVEL must be INFO or WARNING in the private breaker")
    if not configured.public_demo_breaker_audience.strip().startswith("https://"):
        errors.append("PUBLIC_DEMO_BREAKER_AUDIENCE must be an HTTPS Cloud Run audience")
    if len(configured.public_demo_breaker_hmac_secret.strip()) < 32:
        errors.append("PUBLIC_DEMO_BREAKER_HMAC_SECRET must be at least 32 characters")
    if not configured.public_demo_breaker_caller_service_account.strip().endswith(
        ".iam.gserviceaccount.com"
    ):
        errors.append("PUBLIC_DEMO_BREAKER_CALLER_SERVICE_ACCOUNT must be exact")
    if not _TRIGGER_SID_RE.fullmatch(
        configured.public_demo_twilio_usage_trigger_sid.strip()
    ):
        errors.append("PUBLIC_DEMO_TWILIO_USAGE_TRIGGER_SID must be exact")
    if not 0 < configured.public_demo_twilio_daily_spend_limit_usd <= 25:
        errors.append("PUBLIC_DEMO_TWILIO_DAILY_SPEND_LIMIT_USD must be in (0, 25]")

    parent_sid = configured.public_demo_breaker_twilio_parent_account_sid.strip()
    child_sid = configured.public_demo_breaker_twilio_child_account_sid.strip()
    if not _ACCOUNT_SID_RE.fullmatch(parent_sid):
        errors.append("PUBLIC_DEMO_BREAKER_TWILIO_PARENT_ACCOUNT_SID must be exact")
    if not _ACCOUNT_SID_RE.fullmatch(child_sid):
        errors.append("PUBLIC_DEMO_BREAKER_TWILIO_CHILD_ACCOUNT_SID must be exact")
    if parent_sid and child_sid and parent_sid == child_sid:
        errors.append("breaker parent and child Twilio Account SIDs must differ")
    if not _API_KEY_SID_RE.fullmatch(
        configured.public_demo_breaker_twilio_parent_main_api_key_sid.strip()
    ):
        errors.append("PUBLIC_DEMO_BREAKER_TWILIO_PARENT_MAIN_API_KEY_SID must be exact")
    if len(configured.public_demo_breaker_twilio_parent_main_api_key_secret.strip()) < 16:
        errors.append(
            "PUBLIC_DEMO_BREAKER_TWILIO_PARENT_MAIN_API_KEY_SECRET must be configured"
        )
    present_forbidden = [name for name in _FORBIDDEN_BREAKER_ENV if os.getenv(name)]
    if present_forbidden:
        errors.append(
            "private breaker contains forbidden public or parent AuthToken configuration"
        )
    if errors:
        raise RuntimeError("Unsafe public demo breaker configuration: " + "; ".join(errors))


def suppress_breaker_transport_logs() -> None:
    for name in (
        "aiohttp",
        "aiohttp.client",
        "twilio",
        "twilio.http_client",
        "twilio.async_http_client",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


async def _close_twilio_http_client(http_client: object) -> None:
    try:
        await asyncio.wait_for(http_client.close(), timeout=0.5)  # type: ignore[attr-defined]
    except Exception as error:  # noqa: BLE001 - mutation result is already known
        logger.warning(
            "public_demo_breaker event=http_close_failed exception_type=%s",
            type(error).__name__,
        )


async def trip_twilio_parent_breaker(
    configured: PublicDemoBreakerSettings = breaker_settings,
) -> bool:
    """Suspend the exact child using only a revocable parent Main API key.

    Twilio denies parent API keys on subaccount call resources and blocks child
    API activity after suspension. The public demo therefore enforces the bounded
    in-flight drain itself instead of placing the parent account Auth Token in
    this isolated runtime.
    """

    from twilio.http.async_http_client import AsyncTwilioHttpClient
    from twilio.rest import Client

    suppress_breaker_transport_logs()
    http_client = AsyncTwilioHttpClient(
        timeout=PUBLIC_DEMO_BREAKER_TWILIO_HTTP_TIMEOUT_SECONDS,
        logger=logging.getLogger("twilio.async_http_client"),
    )
    key_sid = configured.public_demo_breaker_twilio_parent_main_api_key_sid.strip()
    key_secret = configured.public_demo_breaker_twilio_parent_main_api_key_secret.strip()
    parent_sid = configured.public_demo_breaker_twilio_parent_account_sid.strip()
    child_sid = configured.public_demo_breaker_twilio_child_account_sid.strip()
    parent_client = Client(
        key_sid,
        key_secret,
        account_sid=parent_sid,
        http_client=http_client,
    )

    try:
        try:
            account = await asyncio.wait_for(
                parent_client.api.accounts(child_sid).update_async(status="suspended"),
                timeout=PUBLIC_DEMO_BREAKER_SUSPEND_TIMEOUT_SECONDS,
            )
        except Exception as update_error:  # noqa: BLE001 - verify ambiguous outcome
            logger.warning(
                "public_demo_breaker event=suspend_uncertain exception_type=%s",
                type(update_error).__name__,
            )
            try:
                account = await asyncio.wait_for(
                    parent_client.api.accounts(child_sid).fetch_async(),
                    timeout=PUBLIC_DEMO_BREAKER_SUSPEND_TIMEOUT_SECONDS,
                )
            except Exception as verify_error:  # noqa: BLE001 - fail closed and retry
                logger.error(
                    "public_demo_breaker event=suspend_failed exception_type=%s",
                    type(verify_error).__name__,
                )
                return False
        account_is_exact = (
            str(getattr(account, "status", "")).strip().lower() == "suspended"
            and str(getattr(account, "sid", "")).strip() == child_sid
            and str(getattr(account, "owner_account_sid", "")).strip() == parent_sid
        )
        if not account_is_exact:
            logger.error("public_demo_breaker event=suspend_failed reason=binding")
            return False

        logger.critical("public_demo_breaker event=tripped child=suspended drain=bounded")
        return True
    finally:
        await _close_twilio_http_client(http_client)
