"""Twilio incoming call webhook handler.

This is the most critical endpoint. Every incoming call hits this.

Strategy: Run fast lookups (contacts DB, call history) synchronously within
the webhook, compute trust score, and return the correct TwiML directly.
This avoids the "call not in-progress" issue with async updates.

Twilio gives us 15 seconds to respond. Our target: respond in < 5 seconds.
"""

import asyncio
import re
import secrets
import time

from fastapi import APIRouter, Depends, Request
from twilio.twiml.voice_response import VoiceResponse, Dial, Connect

from app.config import settings
from app.middleware.twilio_verify import verify_twilio_signature
from app.utils.error_handlers import twiml_response, fallback_twiml_response
from app.utils.logging import get_logger, call_sid_var, redact_phone
from app.utils.phone import normalize_phone

logger = get_logger(__name__)

router = APIRouter()


def _forward_twiml(phone: str, caller_id: str = "") -> str:
    """TwiML to forward call directly to a number."""
    cid = caller_id or settings.twilio_phone_number
    response = VoiceResponse()
    dial = Dial(caller_id=cid)
    dial.number(phone)
    response.append(dial)
    return str(response)


def _screening_twiml(call_sid: str, ws_token: str = "") -> str:
    """TwiML to connect caller directly to Kevin via Twilio Media Streams + Gemini.

    <Connect><Stream> opens a WebSocket to our server, which bridges
    the audio to Gemini Live API. The caller talks to Kevin directly
    on this same call — no conference, no transfer, no disconnection.
    """
    ws_url = settings.cloud_run_url.replace("https://", "wss://")
    response = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=f"{ws_url}/media-stream/{call_sid}")
    if ws_token:
        stream.parameter(name="ws_token", value=ws_token)
    response.append(connect)
    return str(response)


def _relay_screening_twiml(
    call_sid: str,
    ws_token: str,
    contractor: dict,
) -> str:
    """TwiML for the ConversationRelay engine (contractor voice_engine=relay).

    Twilio owns STT (Deepgram, `multi` auto language detection), TTS
    (ElevenLabs) and barge-in; our /relay-stream WebSocket exchanges text
    only. The deterministic greeting is spoken by Twilio via welcomeGreeting
    before our server says a word, so time-to-first-audio does not depend on
    any model. STT/TTS providers are left at Twilio defaults (Deepgram +
    ElevenLabs) — both required for automatic language detection.
    """
    from app.services.quiet_hours import is_business_hours
    from app.services.entitlements import effective_mode
    from app.services.relay_pipeline import build_greeting_text

    greeting_config = dict(contractor)
    mode = greeting_config.get("effective_mode") or effective_mode(greeting_config)
    after_hours = mode != "personal" and not is_business_hours(greeting_config)
    greeting = build_greeting_text(greeting_config, after_hours)

    from app.services.voice_pipeline import ELEVENLABS_VOICE_ID

    ws_url = settings.cloud_run_url.replace("https://", "wss://")
    response = VoiceResponse()
    connect = Connect()
    relay = connect.conversation_relay(
        url=f"{ws_url}/relay-stream/{call_sid}",
        welcome_greeting=greeting,
        welcome_greeting_interruptible="any",
        interruptible="any",
        # Kevin's established voice (Eric) — without this, ConversationRelay
        # uses its own ElevenLabs default and Kevin sounds like a stranger.
        voice=ELEVENLABS_VOICE_ID,
        # Playback receipts. The exact message shape is undocumented, so the
        # pipeline logs unknown message types instead of acting on these yet;
        # once observed on a real call, goodbye teardown can key off them
        # instead of the fixed grace sleep.
        events="tokens-played",
    )
    relay.language(code="multi")
    relay.parameter(name="ws_token", value=ws_token)
    # Twilio speaks this before opening our WebSocket. Echo the exact heard
    # greeting into the authenticated setup message so RelayPipeline can seed
    # matching history without persisting customer memory in RTDB.
    relay.parameter(name="spoken_greeting", value=greeting)
    response.append(connect)
    return str(response)


def _safe_incoming_call_push_body(caller_name: str = "", caller_phone: str = "") -> str:
    """Return lock-screen-safe incoming screening copy without caller identity."""
    return "Kevin is screening a call. Open Kevin for details."


def _reject_twiml() -> str:
    """TwiML to reject a spam call."""
    response = VoiceResponse()
    response.reject(reason="busy")
    return str(response)


def _spam_disconnect_twiml() -> str:
    """TwiML to play SIT tone + disconnected message for spam calls.

    The SIT (Special Information Tone) causes autodialers to mark the
    number as disconnected and remove it from their lists.
    """
    response = VoiceResponse()
    response.play("https://storage.googleapis.com/kevin-static-assets/sit-tone.wav")
    response.say(
        "The number you have dialed is not in service. "
        "Please check the number and dial again.",
        voice="Polly.Matthew",
    )
    response.hangup()
    return str(response)


def _conference_twiml(call_sid: str, conference_name: str) -> str:
    """TwiML to put caller into a conference while we ring the contractor."""
    response = VoiceResponse()
    dial = Dial()
    dial.conference(
        conference_name,
        start_conference_on_enter=True,
        end_conference_on_exit=False,
        beep=False,
        wait_url="http://twimlets.com/holdmusic?Bucket=com.twilio.music.classical",
        max_participants=2,
    )
    response.append(dial)
    return str(response)


def _voicemail_twiml() -> str:
    """TwiML for voicemail (circuit breaker mode)."""
    response = VoiceResponse()
    response.say("Hi, I can't take your call right now. Please leave a message after the beep.")
    response.record(max_length=120, transcribe=True, play_beep=True)
    response.say("Thank you. Goodbye.")
    return str(response)


def _expired_voicemail_twiml() -> str:
    """TwiML for deleted-app users — simple voicemail, no AI screening."""
    response = VoiceResponse()
    response.say(
        "Hi, the person you are calling is unavailable. Please leave a message after the tone.",
        voice="Polly.Matthew",
    )
    response.record(
        max_length=120,
        transcribe=True,
        transcribe_callback=f"{settings.cloud_run_url}/webhooks/twilio/voicemail-transcription",
        play_beep=True,
    )
    response.say("Thank you. Goodbye.", voice="Polly.Matthew")
    return str(response)


async def _ring_expired_contractor(
    call_sid: str,
    caller_phone: str,
    conference_name: str,
    contractor_id: str,
    owner_phone: str,
    twilio_number: str,
):
    """Ring expired-subscription contractor via CallKit for 20s, then voicemail."""
    try:
        from twilio.rest import Client
        from app.db.cache import _init_firebase

        _init_firebase()
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        loop = asyncio.get_event_loop()

        for _ in range(10):  # 10 × 2s = 20s
            await asyncio.sleep(2)

            # Check if contractor joined the conference
            try:
                conferences = await loop.run_in_executor(
                    None, lambda: client.conferences.list(friendly_name=conference_name, status="in-progress")
                )
                if conferences:
                    participants = await loop.run_in_executor(
                        None, lambda: conferences[0].participants.list()
                    )
                    if len(participants) >= 2:
                        logger.info(f"Expired contractor answered: {contractor_id}")
                        return  # Connected — nothing more to do
            except Exception:
                pass

        # Timeout — redirect to voicemail TwiML
        logger.info(f"Expired contractor {contractor_id} didn't answer — redirecting to voicemail")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: client.calls(call_sid).update(twiml=_expired_voicemail_twiml())
            )
        except Exception as e:
            logger.error(f"Failed to redirect expired call to voicemail: {e}")

    except Exception as e:
        logger.error(f"_ring_expired_contractor failed: {e}", exc_info=True)


async def _record_forwarding_evidence(contractor_id: str, seen_at: float):
    """Stamp forwarding_last_seen_at after a carrier-confirmed forwarded call.

    Best-effort and fire-and-forget: this is observability, and it must never
    delay or fail the call it was observed on. Throttled to hourly so a busy
    account does not write on every inbound call.
    """
    try:
        from app.db.contractors import get_contractor, update_contractor

        contractor = await get_contractor(contractor_id)
        if not contractor:
            return
        previous = contractor.get("forwarding_last_seen_at") or 0
        if isinstance(previous, (int, float)) and seen_at - previous < 3600:
            return
        await update_contractor(contractor_id, {"forwarding_last_seen_at": seen_at})
        logger.info(f"Forwarding confirmed live via ForwardedFrom: {contractor_id}")
    except Exception as e:
        logger.warning(f"Could not record forwarding evidence: {type(e).__name__}")


async def _handle_deleted_app(
    contractor_id: str,
    caller_phone: str,
    owner_phone: str,
    twilio_number: str,
):
    """Handle call when app is deleted — record deleted_app_detected_at, send voicemail SMS."""
    try:
        from app.db.contractors import get_contractor

        # Deliberately does NOT stamp deleted_app_detected_at. This path runs on
        # *any* VoIP push failure — an APNs outage, a network blip, a briefly
        # invalid token — none of which mean the app was uninstalled. That was
        # harmless while nothing consumed the field, but the 14-day cleanup is
        # live now, so a false positive here starts a countdown to releasing a
        # working user's number.
        #
        # APNs 410 is the authoritative signal and is handled in
        # app/services/push_notification.py. This handler just serves voicemail.
        contractor = await get_contractor(contractor_id)
        if contractor:
            logger.info(
                "VoIP push failed for %s — serving voicemail (not treating as deletion)",
                contractor_id,
            )

    except Exception as e:
        logger.error(f"_handle_deleted_app failed: {e}", exc_info=True)


@router.post("/webhooks/twilio/incoming")
async def handle_incoming_call(request: Request, _=Depends(verify_twilio_signature)):
    """Handle an incoming call from Twilio.

    Runs lookups and scoring synchronously, then returns the correct TwiML
    directly in the response. No async update needed.
    """
    start = time.monotonic()

    try:
        form_data = await request.form()
        call_sid = form_data.get("CallSid", "unknown")
        caller_phone = normalize_phone(form_data.get("From", "")) or form_data.get("From", "")

        call_sid_var.set(call_sid)
        logger.info("Incoming call received", extra={"caller_phone": redact_phone(caller_phone)})

        # Look up contractor by the Twilio number that received the call
        to_number = form_data.get("To", "")
        from app.db.contractors import get_contractor_by_twilio_number
        contractor = await get_contractor_by_twilio_number(to_number)

        # Fall back to default settings if no contractor found (backward compat)
        if contractor:
            contractor_id = contractor["contractor_id"]
            owner_name = contractor.get("owner_name", settings.user_name)
            owner_phone = contractor.get("owner_phone", "")
            business_name = contractor.get("business_name", f"{owner_name}'s office")
            logger.info(f"Contractor found: {business_name} ({contractor_id})")

            # ForwardedFrom is the only positive proof that a user's call
            # forwarding is actually live — iOS exposes no forwarding state and
            # the onboarding step cannot verify itself. Carrier-dependent, so
            # absence means nothing; presence is conclusive.
            from app.utils.phone import forwarding_confirms_owner

            if forwarding_confirms_owner(form_data.get("ForwardedFrom", ""), owner_phone):
                asyncio.create_task(
                    _record_forwarding_evidence(contractor_id, time.time())
                )
        else:
            contractor_id = ""
            owner_name = settings.user_name
            owner_phone = getattr(settings, "user_phone", "")
            business_name = f"{owner_name}'s office"
            contractor = {}
            logger.info("No contractor found — using default settings")

        # Subscription check — must happen BEFORE routing decisions.
        # evaluate_subscription_access compares the expiry to the clock for trials
        # (which we time ourselves) but deliberately fails open on stale `active`
        # expiries, which may just be a missed Apple renewal notification.
        from app.services.subscription import evaluate_subscription_access

        now = time.time()
        is_subscription_active, access_reason = evaluate_subscription_access(contractor, now)

        if access_reason == "active_stale_expiry_needs_reconciliation":
            logger.warning(
                "Subscription expiry is stale for %s — allowing call, needs App Store "
                "reconciliation (status=active, expired %.0fd ago)",
                contractor_id,
                (now - (contractor.get("subscription_expires") or now)) / 86400,
            )

        if not is_subscription_active and contractor:
            # Expired subscription — special handling
            logger.info(f"Expired subscription for {contractor_id} — attempting VoIP ring-through")

            # Attempt VoIP push to ring via CallKit
            from app.services.push_notification import send_voip_push, get_device_token
            device_token = await get_device_token(token_type="voip", contractor_id=contractor_id)

            push_succeeded = False
            conference_name = ""
            if device_token:
                from app.api.voip import _generate_access_token
                from app.services.conference_registry import (
                    new_conference_name,
                    register_conference,
                )

                # F-07: bind the access token's identity to this contractor.
                # F-13: opaque random conference name (was f"expired_{call_sid}").
                access_token = _generate_access_token(contractor_id=contractor_id)
                conference_name = new_conference_name("expired")
                await register_conference(conference_name, contractor_id, call_sid)
                push_succeeded = await send_voip_push(
                    device_token=device_token,
                    caller_phone=caller_phone,
                    caller_name="",
                    call_sid=call_sid,
                    conference_name=conference_name,
                    access_token=access_token,
                    contractor_id=contractor_id,
                )

            if push_succeeded:
                # App is installed — ring for 20s then voicemail
                asyncio.create_task(_ring_expired_contractor(
                    call_sid=call_sid,
                    caller_phone=caller_phone,
                    conference_name=conference_name,
                    contractor_id=contractor_id,
                    owner_phone=owner_phone,
                    twilio_number=to_number,
                ))
                return twiml_response(_conference_twiml(call_sid, conference_name))
            else:
                # App deleted — simple voicemail + SMS
                asyncio.create_task(_handle_deleted_app(
                    contractor_id=contractor_id,
                    caller_phone=caller_phone,
                    owner_phone=owner_phone,
                    twilio_number=to_number,
                ))
                return twiml_response(_expired_voicemail_twiml())

        # Circuit breaker check
        from app.services.circuit_breaker import is_circuit_open
        if is_circuit_open():
            logger.warning("Circuit breaker open — voicemail")
            return twiml_response(_voicemail_twiml())

        # Fast lookups — contacts and call history only (Firestore, ~100ms each)
        # Skip Twilio Lookup API (too slow at 3s) for the synchronous path
        from app.db.contacts import get_contact
        from app.db.calls import get_call_history
        from app.services.scoring import calculate_trust_score
        from app.services.routing import determine_route, Route

        contact = None
        history = {}
        try:
            contact = await asyncio.wait_for(get_contact(caller_phone, contractor_id=contractor_id), timeout=2.0)
        except Exception:
            pass

        try:
            calls = await asyncio.wait_for(
                get_call_history(caller_phone, contractor_id=contractor_id, limit=10),
                timeout=2.0,
            )
            if calls:
                history = {
                    "times_picked_up": sum(1 for c in calls if c.get("outcome") == "picked_up"),
                    "times_ignored": sum(1 for c in calls if c.get("outcome") == "ignored"),
                }
        except Exception:
            pass

        lookups = {
            "contact": contact,
            "history": history,
            "twilio": {},      # Skip Twilio Lookup in sync path
            "nomorobo": {},    # Skip Nomorobo in sync path
        }

        trust_score, score_breakdown = calculate_trust_score(caller_phone, lookups)
        route = determine_route(trust_score)

        # Ring-through toggle: if OFF, downgrade whitelist forwards to screening
        rtc_value = contractor.get("ring_through_contacts", True)
        logger.info(f"Ring-through check: ring_through_contacts={rtc_value!r} (type={type(rtc_value).__name__}), route={route.value}")
        if not rtc_value and route == Route.WHITELIST_FORWARD:
            route = Route.AI_SCREENING
            logger.info("Ring-through contacts disabled — downgrading to AI screening")

        # Quiet hours override
        from app.services.quiet_hours import get_quiet_hours_routing_override
        override = get_quiet_hours_routing_override(trust_score)
        if override and route != Route.SPAM_BLOCK:
            route = Route.AI_SCREENING

        caller_name = contact.get("name", "") if contact else ""
        caller_name_trusted = bool(caller_name)

        # Check if we know this caller from previous calls (per-contractor
        # caller_contacts subcollection — F-14: was a global collection that leaked
        # caller names across contractors). The new helper reads from
        # contractors/{contractor_id}/caller_contacts/{phone_key} and transparently
        # falls back to the legacy global doc until the migration script runs.
        if not caller_name and contractor_id:
            try:
                from app.db.contacts import get_caller_contact as _get_caller_contact
                contact_data = await asyncio.wait_for(
                    _get_caller_contact(contractor_id, caller_phone),
                    timeout=1.0,
                )
                if contact_data:
                    caller_name = contact_data.get("caller_name", "")
                    caller_name_trusted = False
                    if caller_name:
                        logger.info(f"Known caller: {caller_name[:1]}*** ({redact_phone(caller_phone)})")
            except Exception:
                pass

        # F-07/F-13: opaque random conference names (no longer derived from
        # call_sid). Register the binding so /ios-voice and /call-action can
        # resolve back to the contractor and enforce ownership.
        from app.services.conference_registry import (
            new_conference_name,
            register_conference,
        )

        conference_name = new_conference_name("call")
        if contractor_id:
            await register_conference(conference_name, contractor_id, call_sid)

        # Generate WebSocket auth token for AI screening media stream
        ws_token = secrets.token_urlsafe(32) if route == Route.AI_SCREENING else ""

        greeting_config = dict(contractor)
        if caller_name_trusted:
            greeting_config["known_caller_name"] = caller_name
            greeting_config["known_caller_name_trusted"] = True

        # ConversationRelay speaks welcomeGreeting before its WebSocket is
        # established, so only that engine needs a bounded pre-TwiML identity
        # lookup. Request details are loaded later, after WebSocket auth. The
        # media-stream engines do all memory reads after auth and buffer ingress.
        is_relay = contractor.get("voice_engine") == "relay"
        if route == Route.AI_SCREENING and is_relay:
            from app.services.receptionist_context import load_customer_memory_context

            greeting_config.update(
                await load_customer_memory_context(
                    contractor_id,
                    caller_phone,
                    include_requests=False,
                    personalization_enabled=contractor.get(
                        "customer_memory_personalization_enabled"
                    )
                    is True,
                    mutations_enabled=False,
                )
            )

        # Save call record and send notifications in background (don't block response)
        asyncio.create_task(_post_routing_tasks(
            call_sid=call_sid,
            caller_phone=caller_phone,
            caller_name=caller_name,
            trust_score=trust_score,
            score_breakdown=score_breakdown,
            route=route,
            lookups=lookups,
            conference_name=conference_name,
            contractor_id=contractor_id,
            ws_token=ws_token,
            caller_name_trusted=caller_name_trusted,
        ))

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            f"Routing decided in {duration_ms}ms",
            extra={"route": route.value, "trust_score": trust_score, "duration_ms": duration_ms},
        )

        # Fast-track known/whitelisted contacts — ring the contractor directly
        if contact and contact.get("is_whitelisted") and route == Route.WHITELIST_FORWARD:
            # F-07/F-13: opaque random conference name (was f"direct_{call_sid}").
            conference_name = new_conference_name("direct")
            if contractor_id:
                await register_conference(conference_name, contractor_id, call_sid)

            # Start background task to send VoIP push and handle timeout
            asyncio.create_task(_ring_contractor(
                call_sid=call_sid,
                caller_phone=caller_phone,
                caller_name=caller_name or contact.get("name", ""),
                conference_name=conference_name,
                contractor_id=contractor_id,
            ))

            return twiml_response(_conference_twiml(call_sid, conference_name))

        # Return the correct TwiML directly — no async update needed
        if route == Route.WHITELIST_FORWARD:
            return twiml_response(_forward_twiml(owner_phone or settings.user_phone, caller_id=to_number))

        elif route == Route.RING_THEN_SCREEN:
            # For now, forward to user
            return twiml_response(_forward_twiml(owner_phone or settings.user_phone, caller_id=to_number))

        elif route == Route.AI_SCREENING:
            # Engine per contractor: "relay" hands the audio layer to Twilio
            # ConversationRelay; anything else keeps the Media Streams bridge.
            if is_relay:
                return twiml_response(
                    _relay_screening_twiml(
                        call_sid,
                        ws_token,
                        greeting_config,
                    )
                )
            return twiml_response(_screening_twiml(call_sid, ws_token=ws_token))

        elif route == Route.SPAM_BLOCK:
            if contractor.get("sit_tone_enabled", False):
                return twiml_response(_spam_disconnect_twiml())
            return twiml_response(_reject_twiml())

        # Default fallback
        return twiml_response(_forward_twiml(owner_phone or settings.user_phone, caller_id=to_number))

    except Exception as e:
        logger.error(f"Error handling incoming call: {e}", exc_info=True)
        from app.services.circuit_breaker import record_error
        record_error()
        return fallback_twiml_response()


async def _ring_contractor(call_sid: str, caller_phone: str, caller_name: str, conference_name: str, contractor_id: str = ""):
    """Send VoIP push to ring the contractor, with 20-second timeout to Kevin takeover."""
    try:
        from app.services.push_notification import send_voip_push, get_device_token
        from app.api.voip import _generate_access_token

        device_token = await get_device_token(token_type="voip", contractor_id=contractor_id)
        if not device_token:
            logger.warning("No VoIP token — falling back to Kevin screening")
            await _async_redirect_to_kevin(call_sid)
            return

        # F-07: bind the Twilio access token's identity to this contractor so a
        # leaked token cannot impersonate another contractor at the Twilio layer.
        access_token = _generate_access_token(contractor_id=contractor_id)

        # Send VoIP push — this will ring the contractor's phone via CallKit
        await send_voip_push(
            device_token=device_token,
            caller_phone=caller_phone,
            caller_name=caller_name,
            call_sid=call_sid,
            conference_name=conference_name,
            access_token=access_token,
            contractor_id=contractor_id,
        )

        logger.info(f"VoIP push sent for known contact: {caller_name[:1] if caller_name else ''}*** ({redact_phone(caller_phone)})")

        # Poll every 2 seconds for 20 seconds — check for decline or answer
        from app.db.cache import _init_firebase
        from firebase_admin import db as rtdb
        from twilio.rest import Client

        _init_firebase()
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        loop = asyncio.get_event_loop()

        for _ in range(10):  # 10 iterations × 2s = 20s
            await asyncio.sleep(2)

            # Check if contractor declined via RTDB command
            try:
                ref = rtdb.reference(f"/call_commands/{call_sid}")
                command = await loop.run_in_executor(None, ref.get)
                if command and command.get("type") == "take_message":
                    await loop.run_in_executor(None, ref.delete)
                    logger.info("Contractor declined — Kevin taking over")
                    await _async_redirect_to_kevin(call_sid)
                    return
            except Exception:
                pass

            # Check if contractor joined the conference
            try:
                conferences = await loop.run_in_executor(
                    None, lambda: client.conferences.list(friendly_name=conference_name, status="in-progress")
                )
                if conferences:
                    participants = await loop.run_in_executor(
                        None, lambda: conferences[0].participants.list()
                    )
                    if len(participants) >= 2:
                        logger.info("Contractor answered — call connected")
                        return
            except Exception:
                pass

        # Timeout — contractor didn't answer or decline
        logger.info("Contractor didn't answer in 20s — Kevin taking over")
        try:
            await _async_redirect_to_kevin(call_sid)
        except Exception as e:
            logger.error(f"Redirect to Kevin failed: {e}")
            await _async_redirect_to_kevin(call_sid)

    except Exception as e:
        logger.error(f"Ring contractor failed: {e}")
        await _async_redirect_to_kevin(call_sid)


async def _async_redirect_to_kevin(call_sid: str):
    """Redirect a call from conference to Kevin's screening stream (async version)."""
    try:
        from twilio.rest import Client
        from app.db.cache import update_active_call
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

        # Generate ws_token for the media stream and save to RTDB
        ws_token = secrets.token_urlsafe(32)
        await update_active_call(call_sid, {"ws_token": ws_token})

        ws_url = settings.cloud_run_url.replace("https://", "wss://")
        response = VoiceResponse()
        response.say("Thanks for holding. Let me connect you with our assistant.", voice="Polly.Matthew")
        connect = Connect()
        stream = connect.stream(url=f"{ws_url}/media-stream/{call_sid}")
        stream.parameter(name="ws_token", value=ws_token)
        response.append(connect)

        twiml_str = str(response)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: client.calls(call_sid).update(twiml=twiml_str)
        )
        logger.info(f"Call {call_sid} redirected to Kevin screening")
    except Exception as e:
        logger.error(f"Redirect to Kevin failed: {e}")


async def _post_routing_tasks(
    call_sid: str,
    caller_phone: str,
    caller_name: str,
    trust_score: int,
    score_breakdown: dict,
    route,
    lookups: dict,
    conference_name: str,
    contractor_id: str = "",
    ws_token: str = "",
    caller_name_trusted: bool = False,
):
    """Background tasks after routing — save call record, send push, save RTDB state."""
    call_sid_var.set(call_sid)

    try:
        from app.db.calls import save_call
        from app.services.routing import Route

        carrier = lookups.get("twilio", {}).get("carrier", "")
        line_type = lookups.get("twilio", {}).get("line_type", "")
        spam_score = lookups.get("nomorobo", {}).get("spam_score", 0)

        # Save call record
        call_record = {
            "caller_phone": caller_phone,
            "caller_name": caller_name,
            "timestamp": time.time(),
            "trust_score": trust_score,
            "score_breakdown": score_breakdown,
            "route_taken": route.value,
            "lookup_data": {"carrier": carrier, "line_type": line_type, "spam_score": spam_score},
        }
        if contractor_id:
            call_record["contractor_id"] = contractor_id
        await save_call(call_sid, call_record)

        # Push notification is sent after save_active_call below (has contractor_id, fires before transcription)

        # Save active call state to RTDB
        if route == Route.AI_SCREENING:
                from app.db.cache import save_active_call
                from app.services.state_machine import ActiveCall, CallState

                active_call = ActiveCall(
                    call_sid=call_sid,
                    caller_phone=caller_phone,
                    state=CallState.SCREENING,
                    conference_name=conference_name,
                    trust_score=trust_score,
                    caller_name=caller_name,
                    carrier=carrier,
                    line_type=line_type,
                    spam_score=spam_score,
                    contractor_id=contractor_id,
                    ws_token=ws_token,
                    caller_name_trusted=caller_name_trusted,
                )
                await save_active_call(active_call)

                # Send push notification now that RTDB is saved and contractor_id is known
                if contractor_id:
                    from app.services.push_notification import send_regular_push, get_device_token
                    _push_token = await get_device_token(contractor_id=contractor_id)
                    if _push_token:
                        await send_regular_push(
                            device_token=_push_token,
                            title="Incoming Call",
                            body=_safe_incoming_call_push_body(caller_name=caller_name, caller_phone=caller_phone),
                            call_sid=call_sid,
                            caller_phone=caller_phone,
                            caller_name=caller_name,
                            contractor_id=contractor_id,
                        )
                    else:
                        logger.warning(f"No push token for contractor {contractor_id} — notification not sent")

                # Run Twilio Lookup in background (too slow for sync webhook path)
                # Include CNAM if caller is unknown and contractor has it enabled
                include_cnam = False
                if not caller_name:
                    from app.db.contractors import get_contractor
                    cnam_contractor = await get_contractor(contractor_id) if contractor_id else None
                    if cnam_contractor and cnam_contractor.get("cnam_lookup_enabled", False):
                        include_cnam = True

                try:
                    from app.services.lookup import _lookup_twilio
                    lookup_result = await asyncio.wait_for(
                        _lookup_twilio(caller_phone, include_cnam=include_cnam), timeout=5.0
                    )
                    if lookup_result:
                        carrier_from_lookup = lookup_result.get("carrier", "")
                        line_type_from_lookup = lookup_result.get("line_type", "")

                        updates = {}
                        if carrier_from_lookup:
                            updates["carrier"] = carrier_from_lookup
                        if line_type_from_lookup:
                            updates["line_type"] = line_type_from_lookup

                        # If CNAM returned a name, save it
                        cnam_name = lookup_result.get("caller_name", "")
                        if cnam_name:
                            updates["caller_name"] = cnam_name
                            # Also update the Firestore call record
                            from app.db.calls import save_call
                            await save_call(call_sid, {"caller_name": cnam_name})
                            logger.info(f"CNAM lookup resolved: {cnam_name[:1]}***")

                        if updates:
                            from app.db.cache import update_active_call
                            await update_active_call(call_sid, updates)
                            logger.info(f"Lookup enrichment saved: {updates}")
                except Exception as e:
                    logger.warning(f"Background Twilio lookup failed: {e}")

    except Exception as e:
        logger.error(f"Post-routing task error: {e}", exc_info=True)


@router.post("/webhooks/twilio/ios-voice")
async def handle_ios_voice(request: Request, _=Depends(verify_twilio_signature)):
    """TwiML App webhook for iOS Twilio Voice SDK.

    When the iOS app accepts a call, the Twilio Voice SDK makes an outgoing
    call to this TwiML App. We return Conference TwiML so the app joins
    the same conference as the caller.

    F-13: ownership of the conference is enforced by checking that the
    Twilio Voice SDK access-token identity that initiated this leg
    (delivered by Twilio in the `From` field as ``client:contractor_<id>``)
    matches the contractor registered for the requested conference name.
    Twilio signature verification (``verify_twilio_signature``) guarantees
    this request actually came from Twilio and the `From` value is the
    identity Twilio extracted from the signed JWT — i.e. it cannot be
    forged by an iOS attacker holding a stolen conference name.
    """
    try:
        form_data = await request.form()
        # Accept both "conference" and "conference_name" parameter names
        conference_name = form_data.get("conference", "") or form_data.get("conference_name", "")

        logger.info(f"iOS Voice SDK connecting to conference: {conference_name}")

        if not conference_name:
            response = VoiceResponse()
            response.say("No conference specified.")
            response.hangup()
            return twiml_response(str(response))

        # Validate conference_name: only allow alphanumeric, underscores, hyphens
        if not re.match(r'^[a-zA-Z0-9_-]+$', conference_name):
            logger.warning(f"Invalid conference name rejected: {conference_name!r}")
            response = VoiceResponse()
            response.say("Invalid conference name.")
            response.hangup()
            return twiml_response(str(response))

        # F-13: verify the conference belongs to the contractor whose iOS Voice
        # SDK identity initiated this leg.
        from_field = form_data.get("From", "")
        token_identity = ""
        if from_field.startswith("client:"):
            token_identity = from_field[len("client:"):]

        if token_identity.startswith("contractor_"):
            requesting_contractor = token_identity[len("contractor_"):]
        else:
            requesting_contractor = ""

        from app.services.conference_registry import resolve_conference_owner

        owner = await resolve_conference_owner(conference_name)
        if owner is None:
            # No registered binding — could be a legacy/expired call, or a
            # conference name an attacker is probing. Reject defensively.
            logger.warning(
                f"iOS-voice rejected: no binding for conference {conference_name!r} "
                f"(from={from_field!r})"
            )
            response = VoiceResponse()
            response.say("This call session has expired.")
            response.hangup()
            return twiml_response(str(response))

        if not requesting_contractor or requesting_contractor != owner:
            logger.warning(
                f"iOS-voice rejected: conference {conference_name!r} owner={owner!r} "
                f"does not match requester={requesting_contractor!r}"
            )
            response = VoiceResponse()
            response.say("Access denied.")
            response.hangup()
            return twiml_response(str(response))

        response = VoiceResponse()
        dial = Dial(time_limit=5400)  # 90 min max — matches caller's side
        dial.conference(
            conference_name,
            start_conference_on_enter=True,
            end_conference_on_exit=True,  # End conference when either party hangs up
            beep=False,
        )
        response.append(dial)

        return twiml_response(str(response))

    except Exception as e:
        logger.error(f"iOS voice webhook error: {e}", exc_info=True)
        response = VoiceResponse()
        response.say("Something went wrong.")
        return twiml_response(str(response))


# Legacy hand-rolled limiter removed in favor of the persistent rolling-window
# limiter exposed by ``app.api.forwarding.check_dial_in_pin_attempt``. See
# security audit F-15.


@router.post("/webhooks/twilio/dial-in")
async def handle_dial_in(request: Request, _=Depends(verify_twilio_signature)):
    """Handle user dialing in to pick up a screened call.

    The caller has already been redirected into a conference by warm_transfer.
    The user calls the dial-in number, enters a PIN, and joins the same conference.
    """
    try:
        form_data = await request.form()
        caller = form_data.get("From", "")
        digits = form_data.get("Digits", "")  # PIN from <Gather>

        logger.info(f"Dial-in from {redact_phone(caller)}, digits={'[redacted]' if digits else 'none'}")

        # If no digits yet, prompt for PIN. The Gather webhook re-fires when the
        # caller enters digits — we only want to charge a rate-limit slot per
        # actual PIN attempt, not per "give me the prompt" hit.
        if not digits:
            response = VoiceResponse()
            gather = response.gather(num_digits=6, action=f"{settings.cloud_run_url}/webhooks/twilio/dial-in", method="POST")
            gather.say("Enter your six digit PIN.", voice="Polly.Joey")
            response.say("No PIN entered. Goodbye.")
            return twiml_response(str(response))

        # Rate-limit each PIN attempt (F-15). The persistent rolling-window
        # limiter is per-caller and burns one slot per call.
        from app.api.forwarding import check_dial_in_pin_attempt
        rl = await check_dial_in_pin_attempt(caller_phone=caller)
        if not rl.allowed:
            logger.warning(f"Dial-in rate limited: {redact_phone(caller)}")
            response = VoiceResponse()
            response.say("Too many failed attempts. Please try again later.")
            return twiml_response(str(response))

        # Look up contractor by PIN in Firestore
        from app.db.contractors import get_contractor_by_pin
        contractor = await get_contractor_by_pin(digits)
        if not contractor:
            # The check above already counted this attempt; no extra record needed.
            logger.warning(f"Invalid dial-in PIN from {redact_phone(caller)}")
            response = VoiceResponse()
            response.say("Invalid PIN. Goodbye.")
            return twiml_response(str(response))

        contractor_id = contractor["contractor_id"]
        logger.info(f"PIN matched contractor: {contractor_id}")

        # Find the most recent active call in RTDB for this contractor
        from app.db.cache import _init_firebase, ACTIVE_CALLS_PATH
        _init_firebase()
        from firebase_admin import db as rtdb
        import secrets as sec

        ref = rtdb.reference(ACTIVE_CALLS_PATH)
        all_calls = ref.get() or {}

        # Find the most recent active screening call for the matched contractor
        active_call_id = None
        active_call_data = None
        latest_time = 0
        for cid, cdata in all_calls.items():
            if cdata.get("state") == "screening" and cdata.get("contractor_id") == contractor_id:
                updated = cdata.get("state_updated_at", 0)
                if updated > latest_time:
                    latest_time = updated
                    active_call_id = cid
                    active_call_data = cdata

        if not active_call_id or not active_call_data:
            logger.warning("No active screening call found")
            response = VoiceResponse()
            response.say("No active call to join. Goodbye.")
            return twiml_response(str(response))

        logger.info(f"Picking up call {active_call_id}")

        # Create a conference and redirect the caller into it
        conference_name = f"pickup_{sec.token_urlsafe(8)}"

        # Redirect the caller's Twilio call from <Stream> to <Conference>
        from twilio.rest import Client
        twilio = Client(settings.twilio_account_sid, settings.twilio_auth_token)

        try:
            caller_response = VoiceResponse()
            caller_response.say("Connecting you now.", voice="Polly.Joey")
            caller_dial = Dial()
            caller_dial.conference(
                conference_name,
                start_conference_on_enter=True,
                end_conference_on_exit=True,
                beep=False,
                wait_url="http://twimlets.com/holdmusic?Bucket=com.twilio.music.classical",
            )
            caller_response.append(caller_dial)
            twiml_str = str(caller_response)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: twilio.calls(active_call_id).update(twiml=twiml_str)
            )
            logger.info(f"Caller redirected to conference: {conference_name}")
        except Exception as e:
            logger.error(f"Failed to redirect caller: {e}")
            response = VoiceResponse()
            response.say("The caller may have hung up. Goodbye.")
            return twiml_response(str(response))

        # Update state
        from app.db.cache import transition_state
        from app.services.state_machine import CallState
        asyncio.create_task(transition_state(active_call_id, CallState.CONNECTED))

        # Put the user in the same conference
        response = VoiceResponse()
        response.say("Connecting you now.", voice="Polly.Joey")
        dial = Dial()
        dial.conference(
            conference_name,
            start_conference_on_enter=True,
            end_conference_on_exit=True,
            beep=False,
        )
        response.append(dial)

        return twiml_response(str(response))

    except Exception as e:
        logger.error(f"Dial-in error: {e}", exc_info=True)
        response = VoiceResponse()
        response.say("Something went wrong. Please try again.")
        return twiml_response(str(response))


async def _lookup_contractor_for_message(to_number: str):
    """Resolve the contractor who owns the number an inbound message arrived on."""
    from app.db.contractors import get_contractor_by_twilio_number

    return await get_contractor_by_twilio_number(to_number)


async def _record_inbound_message(contractor_id: str, payload: dict) -> bool:
    """Persist an inbound message under the owning contractor."""
    from app.db.firestore_client import get_firestore_client

    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: db.collection("contractors")
        .document(contractor_id)
        .collection("inbound_messages")
        .document(payload["message_sid"])
        .set(payload),
    )
    return True


@router.post("/webhooks/twilio/mms-incoming")
async def handle_inbound_message(request: Request, _=Depends(verify_twilio_signature)):
    """Receive SMS/MMS sent to a Kevin number.

    Every provisioned number points its `sms_url` here (see
    `app/db/contractors.py`), but this route did not exist — so every text a
    caller sent to a Kevin number returned 404 and Twilio recorded an 11200
    HTTP retrieval failure. The messages were lost silently.

    Deliberately does not auto-reply. Responding to inbound traffic carries A2P
    and consent implications and is a product decision; this handler's job is to
    stop dropping messages.

    Response semantics matter here (review finding on PR #143): an unrecognized
    To number returns 200 — redelivery cannot make the number recognized. But a
    lookup or persistence failure returns 500 so Twilio redelivers the webhook
    and the message gets another chance to be stored. Redelivery is idempotent
    because the record is keyed by MessageSid.
    """
    form_data = await request.form()
    to_number = str(form_data.get("To", "") or "")
    message_sid = str(form_data.get("MessageSid", "") or "")

    try:
        num_media = int(str(form_data.get("NumMedia", "0") or "0"))
    except (TypeError, ValueError):
        num_media = 0

    # MMS attachments arrive as MediaUrl{N}/MediaContentType{N} form fields.
    # Without capturing them, an image-only message would be acknowledged and
    # its content silently discarded.
    media = []
    for i in range(min(num_media, 10)):
        url = str(form_data.get(f"MediaUrl{i}", "") or "")
        if url:
            media.append({
                "url": url,
                "content_type": str(form_data.get(f"MediaContentType{i}", "") or ""),
            })

    try:
        contractor = await _lookup_contractor_for_message(to_number)
        if contractor:
            await _record_inbound_message(
                contractor["contractor_id"],
                {
                    "message_sid": message_sid,
                    "from_number": normalize_phone(str(form_data.get("From", "") or ""))
                    or str(form_data.get("From", "") or ""),
                    "to_number": to_number,
                    "body": str(form_data.get("Body", "") or ""),
                    "num_media": num_media,
                    "media": media,
                    "received_at": time.time(),
                },
            )
            logger.info(
                "Inbound message recorded for %s (media=%d)",
                contractor["contractor_id"],
                num_media,
            )
        else:
            logger.warning("Inbound message to an unrecognized number — dropped")
    except Exception as e:
        logger.error(f"Failed to record inbound message: {type(e).__name__}")
        # Non-2xx → Twilio redelivers later; the write is idempotent by SID.
        return twiml_response("<Response></Response>", status_code=500)

    return twiml_response("<Response></Response>")


@router.post("/webhooks/twilio/fallback")
async def handle_fallback(request: Request, _=Depends(verify_twilio_signature)):
    """Emergency fallback. Zero dependencies. Just forward."""
    logger.error("Twilio fallback URL hit — primary handler failed")
    return fallback_twiml_response()


@router.post("/webhooks/twilio/status")
async def handle_status(request: Request, _=Depends(verify_twilio_signature)):
    """Handle call status updates from Twilio."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    status = form_data.get("CallStatus", "")
    call_sid_var.set(call_sid)
    logger.info(f"Call status update: {status}")

    # Clean up RTDB active call when call ends
    if status in ("completed", "busy", "no-answer", "canceled", "failed"):
        updates = {"call_status": status, "ended_at": time.time()}
        duration = form_data.get("CallDuration") or form_data.get("Duration")
        if duration:
            try:
                updates["duration_seconds"] = int(duration)
            except (TypeError, ValueError):
                logger.warning("Invalid Twilio CallDuration for %s: %r", call_sid, duration)
        if call_sid:
            try:
                from app.db.calls import save_call
                await save_call(call_sid, updates)
            except Exception as e:
                logger.warning(f"Failed to save call status: {e}")
        try:
            from app.db.cache import _init_firebase, ACTIVE_CALLS_PATH
            _init_firebase()
            from firebase_admin import db as rtdb
            ref = rtdb.reference(f"{ACTIVE_CALLS_PATH}/{call_sid}")
            ref.delete()
            logger.info(f"Active call cleaned up: {call_sid}")
        except Exception as e:
            logger.warning(f"Failed to clean up active call: {e}")

    return {"status": "ok"}


@router.post("/webhooks/twilio/voicemail-transcription")
async def handle_voicemail_transcription(request: Request, _=Depends(verify_twilio_signature)):
    """Handle Twilio transcription callback for deleted-app voicemails."""
    try:
        form_data = await request.form()
        transcription_text = form_data.get("TranscriptionText", "")
        caller_phone = normalize_phone(form_data.get("From", "")) or form_data.get("From", "")
        to_number = form_data.get("To", "")

        if not to_number:
            return {"status": "ok"}

        from app.db.contractors import get_contractor_by_twilio_number
        contractor = await get_contractor_by_twilio_number(to_number)
        if not contractor:
            # Also check inactive contractors (app may have been deactivated by cleanup)
            from app.db.firestore_client import get_firestore_client
            import asyncio as _asyncio
            db = get_firestore_client()
            loop = _asyncio.get_event_loop()
            docs = await loop.run_in_executor(
                None,
                lambda: list(db.collection("contractors").where("twilio_number", "==", to_number).limit(1).stream())
            )
            if docs:
                contractor = docs[0].to_dict()
                contractor["contractor_id"] = docs[0].id
        if not contractor:
            return {"status": "ok"}

        owner_phone = contractor.get("owner_phone", "")
        if not owner_phone:
            return {"status": "ok"}

        from app.services.sms import send_sms
        caller_display = caller_phone or "Unknown"

        if transcription_text:
            sms_body = (
                f"Voicemail from {caller_display}: \"{transcription_text}\"\n\n"
                f"Kevin AI is no longer installed on your phone. "
                f"To remove call forwarding, visit kevinai.app/support."
            )
        else:
            sms_body = (
                f"You received a voicemail from {caller_display} "
                f"(transcription unavailable).\n\n"
                f"Kevin AI is no longer installed on your phone. "
                f"To remove call forwarding, visit kevinai.app/support."
            )

        twilio_number = contractor.get("twilio_number", "")
        await send_sms(owner_phone, sms_body, from_number=twilio_number)
        logger.info(f"Voicemail SMS sent to {contractor['contractor_id']}")
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Voicemail transcription handler error: {e}", exc_info=True)
        return {"status": "ok"}
