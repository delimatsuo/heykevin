"""Fail-closed Twilio ingress for the public, fictional Kevin phone demo.

This route is deliberately separate from ordinary contractor routing. It does
not look up contacts, history, subscriptions, owners, devices, CRM data, or
calendars, and it never invokes the normal forwarding fallback. The stream is
authenticated without storing caller identity or transcript data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

from fastapi import APIRouter, Depends, Request, Response, WebSocket
from twilio.twiml.voice_response import Connect, VoiceResponse

from app.config import settings
from app.db.rate_limits import check_and_increment
from app.middleware.twilio_verify import verify_twilio_signature
from app.services.public_demo import (
    acquire_public_demo_lease,
    claim_public_demo_stream,
    claim_public_demo_usage_trigger,
    complete_public_demo_usage_trigger,
    hash_public_demo_identifier,
    release_public_demo_lease,
    sign_public_demo_stream_token,
    verify_public_demo_stream_token,
)
from app.services.public_demo_breaker_client import trip_public_demo_breaker
from app.services.public_demo_pipeline import PublicDemoGeminiPipeline
from app.utils.error_handlers import twiml_response
from app.utils.logging import get_logger
from app.utils.phone import normalize_phone
from app.webhooks.media_stream import (
    _cancel_task,
    _log_safe_exception,
    _send_twilio_audio,
    _send_twilio_clear,
    _send_twilio_playback_mark,
    _serve_pipeline_ingress,
    _TwilioMediaIngress,
    _TwilioPlaybackMarks,
)

logger = get_logger(__name__)

router = APIRouter()
_TWILIO_SIGNATURE_REQUIRED = Depends(verify_twilio_signature)

TERMINAL_CALL_STATUSES = frozenset({"completed", "busy", "no-answer", "canceled", "failed"})
PUBLIC_DEMO_TWILIO_HTTP_TIMEOUT_SECONDS = 2.0
PUBLIC_DEMO_COMPLETION_TIMEOUT_SECONDS = 3.0
PUBLIC_DEMO_WEBSOCKET_CLOSE_TIMEOUT_SECONDS = 0.25
PUBLIC_DEMO_RATE_DOCUMENT_TTL_SECONDS = 86_400
PUBLIC_DEMO_USAGE_TRIGGER_MAX_AGE_SECONDS = 900
PUBLIC_DEMO_USAGE_TRIGGER_CLAIM_TIMEOUT_SECONDS = 2.0


def _public_demo_usage_callback_age_is_fresh(callback_age: float) -> bool:
    """Allow limited clock skew but reject the exact 15-minute replay boundary."""

    return -60 <= callback_age < PUBLIC_DEMO_USAGE_TRIGGER_MAX_AGE_SECONDS


def suppress_public_demo_sensitive_transport_logs() -> None:
    """Keep provider URLs, credentials, identifiers, and audio frames out of logs."""

    for name in ("twilio", "twilio.http_client", "twilio.async_http_client"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("websockets", "websockets.client", "websockets.server"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def _close_public_demo_websocket(
    websocket: WebSocket,
    *,
    code: int,
    safe_call_label: str = "",
) -> bool:
    """Bound ASGI close without waiting for cancellation-hostile transport code."""

    close_task = asyncio.create_task(websocket.close(code=code))
    done, _pending = await asyncio.wait(
        {close_task},
        timeout=PUBLIC_DEMO_WEBSOCKET_CLOSE_TIMEOUT_SECONDS,
    )
    if close_task not in done:
        close_task.cancel()
        close_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        logger.warning(
            "public_demo event=stream_close_timeout call=%s",
            safe_call_label or "unknown",
        )
        return False
    try:
        close_task.result()
    except Exception as error:  # noqa: BLE001 - socket may already be closed
        _log_safe_exception("public_demo_stream_close_error", error, safe_call_label)
        return False
    return True


def _configured_demo_number() -> str:
    raw = (settings.public_demo_number or "").strip()
    return normalize_phone(raw) or raw


def _is_expected_demo_number(value: object) -> bool:
    configured = _configured_demo_number()
    raw = str(value or "").strip()
    received = normalize_phone(raw) or raw
    return bool(configured and received and received == configured)


def _public_demo_unavailable_twiml(*, busy: bool = False) -> str:
    response = VoiceResponse()
    if busy:
        response.say(
            "The Kevin demo is busy right now. Please try again later.",
            voice="Polly.Matthew",
        )
    else:
        response.say(
            "The Kevin demo is unavailable right now. Please try again later.",
            voice="Polly.Matthew",
        )
    response.hangup()
    return str(response)


def _public_demo_limit_twiml() -> str:
    response = VoiceResponse()
    response.say(
        "This demo call has reached its time limit. Thanks for trying Kevin.",
        voice="Polly.Matthew",
    )
    response.hangup()
    return str(response)


def _public_demo_stream_twiml(stream_token: str) -> str:
    ws_url = settings.cloud_run_url.replace("https://", "wss://")
    response = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=f"{ws_url}/public-demo-stream")
    stream.parameter(name="demo_token", value=stream_token)
    response.append(connect)
    return str(response)


async def _enforce_public_demo_max_duration(
    *,
    started_at: float,
    max_duration_seconds: int,
    websocket: WebSocket,
    safe_call_label: str,
    on_max_duration: Callable[[], Awaitable[None]],
) -> None:
    """Close media at the wall-clock cutoff even if provider REST is stalled."""

    remaining = max(0.0, max_duration_seconds - (time.monotonic() - started_at))
    await asyncio.sleep(remaining)
    # Start both cutoff mechanisms immediately. Neither a stalled ASGI close nor a
    # stalled REST client can prevent the other from running.
    close_task = asyncio.create_task(
        _close_public_demo_websocket(
            websocket,
            code=1000,
            safe_call_label=safe_call_label,
        )
    )
    completion_task = asyncio.create_task(on_max_duration())
    await asyncio.gather(close_task, completion_task, return_exceptions=True)


async def _complete_public_demo_call(
    *,
    call_sid: str,
    safe_call_label: str,
    websocket: WebSocket,
    twiml: str | None = None,
) -> bool:
    """End a demo call without sending its raw provider identifier to app logs."""

    try:
        from twilio.http.http_client import TwilioHttpClient
        from twilio.rest import Client

        suppress_public_demo_sensitive_transport_logs()
        http_client = TwilioHttpClient(
            timeout=PUBLIC_DEMO_TWILIO_HTTP_TIMEOUT_SECONDS,
            max_retries=0,
            logger=logging.getLogger("twilio.http_client"),
        )
        client = Client(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            http_client=http_client,
        )
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: client.calls(call_sid).update(
                    twiml=twiml or "<Response><Hangup/></Response>"
                ),
            ),
            timeout=PUBLIC_DEMO_COMPLETION_TIMEOUT_SECONDS,
        )
    except Exception as error:  # noqa: BLE001 - socket close is the fail-safe
        _log_safe_exception("public_demo_hangup_error", error, safe_call_label)
        return False
    finally:
        # REST completion is best effort. The media WebSocket is always closed within
        # the bounded wait above, so a stalled provider request cannot extend paid AI.
        await _close_public_demo_websocket(
            websocket,
            code=1000,
            safe_call_label=safe_call_label,
        )
    return True


async def _release_lease_safely(call_sid: str) -> None:
    secret = (settings.public_demo_hmac_secret or "").strip()
    if not call_sid or not secret:
        return
    try:
        await release_public_demo_lease(call_sid, secret)
    except Exception as error:  # noqa: BLE001 - release helper itself fails closed
        safe_label = hash_public_demo_identifier(secret, "log-call", call_sid)
        _log_safe_exception("public_demo_lease_release_error", error, safe_label)


@router.post("/webhooks/twilio/public-demo/incoming")
async def handle_public_demo_incoming(
    request: Request,
    _=_TWILIO_SIGNATURE_REQUIRED,
):
    """Admit a public demo call before any AI provider is connected."""
    try:
        form_data = await request.form()
    except Exception as error:  # noqa: BLE001 - Twilio must receive fail-closed TwiML
        logger.error(
            "public_demo event=admission_denied reason=form_error exception_type=%s",
            type(error).__name__,
        )
        return twiml_response(_public_demo_unavailable_twiml())
    call_sid = str(form_data.get("CallSid", "") or "").strip()
    to_number = form_data.get("To", "")
    secret = (settings.public_demo_hmac_secret or "").strip()

    if (
        not settings.public_demo_enabled
        or len(secret) < 32
        or not call_sid
        or not _is_expected_demo_number(to_number)
    ):
        logger.warning("public_demo event=admission_denied reason=disabled_or_misbound")
        return twiml_response(_public_demo_unavailable_twiml())

    caller_value = str(form_data.get("From", "") or "anonymous").strip() or "anonymous"
    caller_key = hash_public_demo_identifier(secret, "caller", caller_value)

    try:
        per_caller = await check_and_increment(
            scope="public_demo_per_caller",
            key=caller_key,
            limit=settings.public_demo_per_caller_limit,
            window_seconds=settings.public_demo_per_caller_window_seconds,
            document_ttl_seconds=settings.public_demo_per_caller_window_seconds,
        )
    except Exception as error:  # noqa: BLE001 - admission storage must fail closed
        logger.error(
            "public_demo event=admission_denied reason=caller_limit_error "
            "exception_type=%s",
            type(error).__name__,
        )
        return twiml_response(_public_demo_unavailable_twiml())
    if not per_caller.allowed:
        logger.info("public_demo event=admission_denied reason=caller_limit")
        return twiml_response(_public_demo_unavailable_twiml(busy=True))

    try:
        admitted = await acquire_public_demo_lease(
            call_sid,
            secret,
            limit=settings.public_demo_concurrency_limit,
            ttl_seconds=settings.public_demo_lease_ttl_seconds,
        )
    except Exception as error:  # noqa: BLE001 - concurrency uncertainty denies access
        logger.error(
            "public_demo event=admission_denied reason=concurrency_error exception_type=%s",
            type(error).__name__,
        )
        return twiml_response(_public_demo_unavailable_twiml())
    if not admitted:
        logger.info("public_demo event=admission_denied reason=concurrency_limit")
        return twiml_response(_public_demo_unavailable_twiml(busy=True))

    try:
        daily = await check_and_increment(
            scope="public_demo_daily",
            key="all",
            limit=settings.public_demo_daily_call_limit,
            window_seconds=86_400,
            document_ttl_seconds=PUBLIC_DEMO_RATE_DOCUMENT_TTL_SECONDS,
        )
    except Exception as error:  # noqa: BLE001 - daily budget uncertainty denies access
        logger.error(
            "public_demo event=admission_denied reason=daily_limit_error exception_type=%s",
            type(error).__name__,
        )
        await _release_lease_safely(call_sid)
        return twiml_response(_public_demo_unavailable_twiml())
    if not daily.allowed:
        logger.info("public_demo event=admission_denied reason=daily_limit")
        await _release_lease_safely(call_sid)
        return twiml_response(_public_demo_unavailable_twiml(busy=True))

    try:
        token = sign_public_demo_stream_token(
            secret,
            call_sid,
            _configured_demo_number(),
            ttl_seconds=settings.public_demo_max_call_duration_seconds + 60,
        )
    except Exception as error:  # noqa: BLE001 - token uncertainty denies admission
        safe_label = hash_public_demo_identifier(secret, "log-call", call_sid)
        _log_safe_exception("public_demo_token_error", error, safe_label)
        await _release_lease_safely(call_sid)
        return twiml_response(_public_demo_unavailable_twiml())

    logger.info("public_demo event=admission_allowed")
    return twiml_response(_public_demo_stream_twiml(token))


@router.post("/webhooks/twilio/public-demo/fallback")
async def handle_public_demo_fallback(
    request: Request,
    _=_TWILIO_SIGNATURE_REQUIRED,
):
    """Fail closed; never forward a broken demo call to a configured owner."""
    form_data = await request.form()
    if _is_expected_demo_number(form_data.get("To", "")):
        await _release_lease_safely(str(form_data.get("CallSid", "") or ""))
    logger.warning("public_demo event=fallback")
    return twiml_response(_public_demo_unavailable_twiml())


@router.post("/webhooks/twilio/public-demo/status")
async def handle_public_demo_status(
    request: Request,
    _=_TWILIO_SIGNATURE_REQUIRED,
):
    """Release ephemeral concurrency state without writing a call record."""
    form_data = await request.form()
    status = str(form_data.get("CallStatus", "") or "").strip().lower()
    if status in TERMINAL_CALL_STATUSES and _is_expected_demo_number(form_data.get("To", "")):
        await _release_lease_safely(str(form_data.get("CallSid", "") or ""))
    return {"status": "ok"}


@router.post("/webhooks/twilio/public-demo/message")
async def discard_public_demo_message(
    request: Request,
    _=_TWILIO_SIGNATURE_REQUIRED,
):
    """Accept-and-discard inbound SMS/MMS; retain no sender, body, or media URL."""
    await request.form()
    logger.info("public_demo event=inbound_message_discarded")
    return twiml_response("<Response></Response>")


@router.post("/webhooks/twilio/public-demo/usage-limit")
async def handle_public_demo_usage_limit(
    request: Request,
    _=_TWILIO_SIGNATURE_REQUIRED,
):
    """Suspend the isolated Twilio child when its exact daily trigger fires."""

    try:
        form_data = await request.form()
        configured_limit = Decimal(
            str(settings.public_demo_twilio_daily_spend_limit_usd)
        )
        trigger_value = Decimal(str(form_data.get("TriggerValue", "")))
        current_value = Decimal(str(form_data.get("CurrentValue", "")))
        date_fired = parsedate_to_datetime(str(form_data.get("DateFired", "")))
        if date_fired.tzinfo is None:
            date_fired = date_fired.replace(tzinfo=UTC)
        callback_age = (datetime.now(UTC) - date_fired.astimezone(UTC)).total_seconds()
    except (InvalidOperation, TypeError, ValueError):
        logger.warning("public_demo event=usage_breaker_rejected reason=invalid_amount")
        return Response(status_code=403)
    except Exception as error:  # noqa: BLE001 - preserve provider callback retries
        _log_safe_exception("public_demo_usage_breaker_form_error", error)
        return Response(status_code=503)

    idempotency_token = str(form_data.get("IdempotencyToken", ""))
    bound = (
        str(form_data.get("AccountSid", "")) == settings.twilio_account_sid
        and str(form_data.get("UsageTriggerSid", ""))
        == settings.public_demo_twilio_usage_trigger_sid
        and str(form_data.get("UsageCategory", "")).strip().lower() == "totalprice"
        and str(form_data.get("TriggerBy", "")).strip().lower() == "price"
        and str(form_data.get("Recurring", "")).strip().lower() == "daily"
        and trigger_value == configured_limit
        and current_value >= configured_limit
        and 8 <= len(idempotency_token) <= 512
        and all(33 <= ord(char) <= 126 for char in idempotency_token)
        and _public_demo_usage_callback_age_is_fresh(callback_age)
    )
    if not bound:
        logger.warning("public_demo event=usage_breaker_rejected reason=misbound")
        return Response(status_code=403)

    # The provider stop comes before Firestore. A stalled replay seal must never
    # delay suspension; DateFired freshness plus the fail-closed recovery procedure
    # prevents an old callback from affecting a deliberately restored child.
    if not await trip_public_demo_breaker(
        child_account_sid=settings.twilio_account_sid,
        usage_trigger_sid=settings.public_demo_twilio_usage_trigger_sid,
        trigger_value=trigger_value,
        current_value=current_value,
        date_fired=date_fired,
        idempotency_token=idempotency_token,
    ):
        return Response(status_code=503)

    secret = (settings.public_demo_hmac_secret or "").strip()
    try:
        claim_state = await asyncio.wait_for(
            claim_public_demo_usage_trigger(
                idempotency_token,
                secret,
            ),
            timeout=PUBLIC_DEMO_USAGE_TRIGGER_CLAIM_TIMEOUT_SECONDS,
        )
    except Exception as error:  # noqa: BLE001 - preserve provider callback retries
        _log_safe_exception("public_demo_usage_breaker_claim_error", error)
        return Response(status_code=503)
    if claim_state == "completed":
        return {"status": "already_suspended"}
    if claim_state not in {"new", "pending"}:
        return Response(status_code=503)

    try:
        completed = await asyncio.wait_for(
            complete_public_demo_usage_trigger(idempotency_token, secret),
            timeout=PUBLIC_DEMO_USAGE_TRIGGER_CLAIM_TIMEOUT_SECONDS,
        )
    except Exception as error:  # noqa: BLE001 - preserve provider callback retries
        _log_safe_exception("public_demo_usage_breaker_completion_error", error)
        return Response(status_code=503)
    if not completed:
        return Response(status_code=503)

    logger.critical("public_demo event=usage_breaker_tripped account=suspended")
    return {"status": "suspended"}


@router.websocket("/public-demo-stream")
async def public_demo_stream_ws(websocket: WebSocket):
    """Stateless Twilio-to-Gemini bridge with no transcript or caller persistence."""
    await websocket.accept()
    call_sid = ""
    stream_sid = ""
    demo_token = ""
    deadline_started_at = time.monotonic()
    call_started_at = time.time()

    try:
        for _ in range(5):
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=5)
            message = json.loads(raw)
            if message.get("event") == "start":
                start_payload = message.get("start", {})
                if not isinstance(start_payload, dict):
                    break
                deadline_started_at = time.monotonic()
                call_started_at = time.time()
                stream_sid = str(message.get("streamSid", "") or "")
                call_sid = str(start_payload.get("callSid", "") or "")
                demo_token = str(
                    start_payload.get("customParameters", {})
                    .get("demo_token", "")
                    or ""
                )
                break
    except Exception as error:  # noqa: BLE001 - malformed streams fail closed
        _log_safe_exception("public_demo_start_error", error)

    secret = (settings.public_demo_hmac_secret or "").strip()
    token_verified = (
        settings.public_demo_enabled
        and bool(stream_sid)
        and len(secret) >= 32
        and verify_public_demo_stream_token(
            demo_token,
            secret,
            call_sid,
            _configured_demo_number(),
        )
    )
    if not token_verified:
        logger.warning("public_demo event=stream_auth_rejected")
        await _close_public_demo_websocket(websocket, code=1008)
        return

    claimed = await claim_public_demo_stream(
        call_sid,
        secret,
        ttl_seconds=max(
            30,
            min(int(settings.public_demo_max_call_duration_seconds), 180),
        )
        + 60,
    )
    if not claimed:
        logger.warning("public_demo event=stream_auth_rejected reason=replayed_or_uncertain")
        await _close_public_demo_websocket(websocket, code=1008)
        return

    safe_call_label = hash_public_demo_identifier(secret, "log-call", call_sid)
    playback_marks = _TwilioPlaybackMarks(call_sid=safe_call_label)
    ingress = _TwilioMediaIngress(
        websocket,
        call_sid=safe_call_label,
        on_playback_mark=playback_marks.resolve,
    )
    ingress_task = asyncio.create_task(ingress.run())
    pipeline = None
    duration_task = None
    completion_task = None
    completion_lock = asyncio.Lock()

    async def on_audio_out(mulaw_chunk: bytes):
        return await _send_twilio_audio(
            websocket,
            stream_sid=stream_sid,
            mulaw_chunk=mulaw_chunk,
            call_sid=safe_call_label,
        )

    async def on_clear_audio():
        cleared = await _send_twilio_clear(
            websocket,
            stream_sid=stream_sid,
            call_sid=safe_call_label,
        )
        if cleared:
            playback_marks.mark_pending_cleared()
        return cleared

    async def on_first_media(turn: int):
        return await _send_twilio_playback_mark(
            websocket,
            stream_sid=stream_sid,
            playback_marks=playback_marks,
            turn=turn,
            phase="first_media",
            call_sid=safe_call_label,
        )

    async def on_end_media(turn: int):
        return await _send_twilio_playback_mark(
            websocket,
            stream_sid=stream_sid,
            playback_marks=playback_marks,
            turn=turn,
            phase="response_end",
            call_sid=safe_call_label,
        )

    async def on_transcript(_speaker: str, _text: str):
        # Speech is transient provider/session input only. It is deliberately
        # not copied to RTDB, Firestore, logs, jobs, contacts, or handoffs.
        return None

    async def on_call_complete(*, twiml: str | None = None):
        nonlocal completion_task
        async with completion_lock:
            if completion_task is None:
                completion_task = asyncio.create_task(
                    _complete_public_demo_call(
                        call_sid=call_sid,
                        safe_call_label=safe_call_label,
                        websocket=websocket,
                        twiml=twiml,
                    )
                )
            task = completion_task
        await asyncio.shield(task)

    async def on_stream_stop():
        return None

    async def on_max_duration():
        if pipeline:
            # Synchronous first step: immediately stop accepting provider/media work
            # and abort the Gemini transport before any potentially blocking cleanup.
            pipeline.enforce_deadline()
        await on_call_complete(twiml=_public_demo_limit_twiml())

    max_duration = max(
        30,
        min(int(settings.public_demo_max_call_duration_seconds), 180),
    )

    duration_task = asyncio.create_task(
        _enforce_public_demo_max_duration(
            started_at=deadline_started_at,
            max_duration_seconds=max_duration,
            websocket=websocket,
            safe_call_label=safe_call_label,
            on_max_duration=on_max_duration,
        )
    )

    try:
        if not settings.gemini_api_key:
            logger.error("public_demo event=pipeline_unavailable reason=missing_provider_config")
            await on_call_complete(twiml=_public_demo_unavailable_twiml())
            return

        pipeline = PublicDemoGeminiPipeline(
            on_audio_out=on_audio_out,
            on_transcript=on_transcript,
            on_clear_audio=on_clear_audio,
            on_response_first_media_sent=on_first_media,
            on_response_end_media_sent=on_end_media,
            on_call_complete=on_call_complete,
            on_urgency_detected=None,
            call_started_at=time.monotonic(),
        )
        started = await _serve_pipeline_ingress(
            pipeline,
            ingress,
            call_sid=safe_call_label,
            media_stream_started_at=time.monotonic(),
            call_started_at=call_started_at,
            max_call_duration_seconds=max_duration,
            on_stream_stop=on_stream_stop,
            on_max_duration=on_max_duration,
        )
        if not started:
            await on_call_complete(twiml=_public_demo_unavailable_twiml())
    except Exception as error:  # noqa: BLE001 - stream failures must hang up
        _log_safe_exception("public_demo_stream_error", error, safe_call_label)
        await on_call_complete(twiml=_public_demo_unavailable_twiml())
    finally:
        await _cancel_task(ingress_task)
        await _cancel_task(duration_task)
        if pipeline:
            try:
                await pipeline.stop()
            except Exception as error:  # noqa: BLE001 - teardown stays best effort
                _log_safe_exception(
                    "public_demo_pipeline_stop_error",
                    error,
                    safe_call_label,
                )
        if completion_task:
            await asyncio.gather(completion_task, return_exceptions=True)
        await _close_public_demo_websocket(
            websocket,
            code=1000,
            safe_call_label=safe_call_label,
        )
        await _release_lease_safely(call_sid)
        logger.info("public_demo event=stream_closed")
