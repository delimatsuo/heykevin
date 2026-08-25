"""WebSocket endpoint for Twilio ConversationRelay sessions.

The audio never touches this process: Twilio streams the caller's words as
text `prompt` messages and speaks whatever text we send back. This handler
authenticates the session, wires a RelayPipeline to the same transcript /
urgency / post-call machinery the media-stream engine uses, and preserves
the accepted-call (conference redirect) semantics.
"""

import asyncio
import json
import time

from fastapi import APIRouter, WebSocket

from app.config import settings
from app.db.cache import (
    ACTIVE_CALLS_PATH,
    _init_firebase,
    get_active_call,
)
from app.services.relay_pipeline import RelayPipeline
from app.services.voice_pipeline import _call_label
from app.webhooks.media_stream import (
    LiveTranscriptPusher,
    _log_safe_exception,
    _resolve_active_call,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

# Same ceiling as the media-stream engine: a hard stop on runaway calls.
MAX_CALL_DURATION = 5400  # seconds

SETUP_TIMEOUT_SECONDS = 10


@router.websocket("/relay-stream/{call_sid}")
async def relay_stream_ws(websocket: WebSocket, call_sid: str):
    await websocket.accept()
    logger.info("relay_event event=stream_connected call=%s", _call_label(call_sid))

    # ConversationRelay's first message is `setup`, carrying customParameters.
    setup = None
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=SETUP_TIMEOUT_SECONDS
        )
        message = json.loads(raw)
        if message.get("type") == "setup":
            setup = message
    except Exception as error:
        _log_safe_exception("relay_setup_error", error, call_sid)

    ws_token = (setup or {}).get("customParameters", {}).get("ws_token", "")
    spoken_greeting = (setup or {}).get("customParameters", {}).get(
        "spoken_greeting", ""
    )
    if not setup or not ws_token:
        logger.warning(
            "relay_event event=stream_auth_rejected call=%s reason=invalid_setup",
            _call_label(call_sid),
        )
        await websocket.close(code=1008)
        return

    # Validate the token against RTDB — identical trust model to /media-stream.
    call_data = None
    try:
        _init_firebase()
        from firebase_admin import db as rtdb

        ref = rtdb.reference(f"{ACTIVE_CALLS_PATH}/{call_sid}")
        loop = asyncio.get_event_loop()
        for _attempt in range(3):
            call_data = await loop.run_in_executor(None, ref.get)
            if call_data:
                break
            await asyncio.sleep(0.5)
    except Exception as error:
        _log_safe_exception("relay_rtdb_lookup_error", error, call_sid)

    if (
        not call_data
        or not call_data.get("ws_token")
        or ws_token != call_data["ws_token"]
    ):
        logger.warning(
            "relay_event event=stream_auth_rejected call=%s reason=invalid_token",
            _call_label(call_sid),
        )
        await websocket.close(code=1008)
        return

    transcript_lines: list[str] = []
    transcript_pusher = LiveTranscriptPusher(call_sid, transcript_lines)
    call_redirected = False
    pipeline = None
    active_call = None
    contractor_config_loaded: dict = {}

    try:
        active_call = await _resolve_active_call(call_sid, call_data)
        _contractor_id = ""
        if active_call:
            _contractor_id = getattr(active_call, "contractor_id", "") or ""
            if _contractor_id:
                from app.db.contractors import get_contractor
                from app.services.entitlements import with_entitlement_flags

                contractor_data = await get_contractor(_contractor_id)
                if contractor_data:
                    contractor_config_loaded = with_entitlement_flags(contractor_data)
            if active_call.caller_name:
                contractor_config_loaded["known_caller_name"] = active_call.caller_name
                contractor_config_loaded["known_caller_name_trusted"] = (
                    getattr(active_call, "caller_name_trusted", False) is True
                )
            if _contractor_id and active_call.caller_phone:
                from app.services.receptionist_context import load_customer_memory_context

                contractor_config_loaded.update(
                    await load_customer_memory_context(
                        _contractor_id,
                        active_call.caller_phone,
                        personalization_enabled=contractor_config_loaded.get(
                            "customer_memory_personalization_enabled"
                        )
                        is True,
                        mutations_enabled=(
                            settings.service_request_recovery_enabled is True
                            and
                            contractor_config_loaded.get(
                                "service_request_mutations_enabled"
                            )
                            is True
                            and contractor_config_loaded.get(
                                "integration_write_status"
                            )
                            == "approved"
                        ),
                    )
                )
            from app.services.integration_tokens import has_usable_token

            if contractor_config_loaded.get("customer_memory") or has_usable_token(
                contractor_config_loaded, "google_calendar"
            ):
                from app.db.service_requests import FirestoreServiceRequestRepository
                from app.services.google_calendar_request_provider import (
                    GoogleCalendarRequestProvider,
                )
                from app.services.service_request_repository import (
                    ServiceRequestCommandService,
                )

                provider_adapter = (
                    GoogleCalendarRequestProvider(contractor_config_loaded)
                    if has_usable_token(contractor_config_loaded, "google_calendar")
                    else None
                )
                contractor_config_loaded["service_request_command_service"] = (
                    ServiceRequestCommandService(
                        FirestoreServiceRequestRepository(),
                        provider_adapter=provider_adapter,
                    )
                )
    except Exception as error:
        _log_safe_exception("relay_stream_setup_error", error, call_sid)
        await websocket.close(code=1011)
        return

    async def send_to_twilio(message: dict) -> None:
        await websocket.send_text(json.dumps(message))

    async def on_transcript(speaker: str, text: str):
        transcript_lines.append(f"{speaker}: {text}")
        if len(transcript_lines) > 500:
            transcript_lines[:] = transcript_lines[-500:]
        transcript_pusher.push()

    async def on_urgency_detected(transcript_snippet: str):
        # Same escalation path as the media-stream engine, minus the audio
        # plumbing: delegate to its implementation via the shared helpers.
        from app.webhooks.media_stream import _safe_urgent_push_body
        from app.services.push_notification import (
            get_device_token,
            send_urgent_push,
            send_voip_push,
        )
        from app.services.conference_registry import (
            new_conference_name,
            register_conference,
        )

        _cid = contractor_config_loaded.get("contractor_id", "")
        caller_phone = active_call.caller_phone if active_call else ""
        caller_name = active_call.caller_name if active_call else ""

        voip_token = await get_device_token(token_type="voip", contractor_id=_cid)
        if voip_token:
            urgent_conf = new_conference_name("urgent")
            if _cid:
                await register_conference(urgent_conf, _cid, call_sid)
            await send_voip_push(
                device_token=voip_token,
                caller_phone=caller_phone,
                caller_name=f"URGENT: {caller_name or caller_phone}",
                reason="urgent_call",
                call_sid=call_sid,
                conference_name=urgent_conf,
                contractor_id=_cid,
            )
        push_token = await get_device_token(contractor_id=_cid)
        if push_token:
            await send_urgent_push(
                device_token=push_token,
                title="URGENT CALL",
                body=_safe_urgent_push_body(
                    caller_name=caller_name, caller_phone=caller_phone
                ),
                call_sid=call_sid,
                caller_phone=caller_phone,
                caller_name=caller_name,
                contractor_id=_cid,
            )
        logger.info(
            "relay_event event=urgency_escalated call=%s", _call_label(call_sid)
        )

    session_done = asyncio.Event()

    async def on_call_complete():
        session_done.set()

    pipeline = RelayPipeline(
        contractor_config=contractor_config_loaded,
        call_sid=call_sid,
        caller_phone=active_call.caller_phone if active_call else "",
        send_to_twilio=send_to_twilio,
        on_transcript=on_transcript,
        on_urgency_detected=on_urgency_detected,
        on_call_complete=on_call_complete,
        spoken_greeting=spoken_greeting,
    )
    pipeline.start_background_tasks()
    logger.info(
        "relay_event event=pipeline_selected call=%s engine=relay",
        _call_label(call_sid),
    )

    started_at = time.monotonic()
    try:
        while not session_done.is_set():
            remaining = MAX_CALL_DURATION - (time.monotonic() - started_at)
            if remaining <= 0:
                logger.info(
                    "relay_event event=max_duration call=%s", _call_label(call_sid)
                )
                await pipeline.end_call()
                break
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=min(remaining, 30.0)
                )
            except asyncio.TimeoutError:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await pipeline.handle_message(message)
    except Exception as error:
        # Includes the normal WebSocket disconnect when the call ends.
        _log_safe_exception("relay_stream_closed", error, call_sid)
    finally:
        if pipeline:
            try:
                await pipeline.stop()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass

        # Owner pickup redirects the call to a conference; Twilio tears the
        # relay session down. Same accepted-call semantics as media-stream.
        try:
            refreshed = await get_active_call(call_sid)
            if refreshed and getattr(refreshed, "accepted", False):
                call_redirected = True
        except Exception as error:
            _log_safe_exception("relay_stop_refresh_error", error, call_sid)

        transcript_saved = False
        if transcript_lines:
            from app.db.calls import save_call

            try:
                transcript_saved = await save_call(
                    call_sid, {"transcript": "\n".join(transcript_lines)}
                )
            except Exception:
                logger.error("Relay post-call transcript persistence raised")
            if not transcript_saved:
                logger.error("Relay post-call transcript persistence failed")

        if transcript_saved and active_call and not call_redirected:
            from app.services.post_call_handoff import enqueue_and_run_post_call

            await enqueue_and_run_post_call(
                transcript_lines=list(transcript_lines),
                caller_phone=active_call.caller_phone if active_call else "",
                call_sid=call_sid,
                contractor_phone=contractor_config_loaded.get("owner_phone", ""),
                twilio_number=contractor_config_loaded.get("twilio_number", ""),
                contractor=contractor_config_loaded,
                caller_language=pipeline.language if pipeline else "en",
            )

        logger.info("relay_event event=stream_closed call=%s", _call_label(call_sid))
