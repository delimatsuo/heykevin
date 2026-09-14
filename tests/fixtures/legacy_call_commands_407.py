"""Fictional test execution of production 407 call-command functions.

Extracted verbatim bodies via AST; no original module is imported. Provider and
RTDB dependencies must be replaced by in-memory doubles before execution.
"""
import asyncio

# Supplied by the fictional test harness; no original production globals load.
Connect = None
Dial = None
VoiceResponse = None
_async_redirect_to_kevin = None
_call_label = None
_generate_access_token = None
logger = None
redact_phone = None
secrets = None
settings = None

# 407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc:app/api/voip.py:393
# Original function SHA-256: 063babd3c8cd71128bca2e3fcccae91e921fe9edda59c71717fa8960bbd23fe7
async def legacy_decline(call_sid: str) -> dict:
    """Write take_message command to RTDB for the voice pipeline to pick up."""
    from app.db.cache import _init_firebase
    from firebase_admin import db as rtdb
    import asyncio
    _init_firebase()
    ref = rtdb.reference(f'/call_commands/{call_sid}')
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, ref.set, {'type': 'take_message'})
    logger.info(f'Take-message command queued for {call_sid}')
    return {'status': 'ok'}

# 407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc:app/api/voip.py:339
# Original function SHA-256: 7597d68ce69d2eb013b80187a24f6db35ef0244ea537ece28fa860e04ca649d3
async def legacy_accept(call_sid: str, contractor_id: str='') -> dict:
    """Accept a call — move caller to conference, return token for direct SDK connection.

    The iOS app connects directly via Twilio Voice SDK using the returned
    access_token and conference_name. No VoIP push needed — the user already
    tapped 'Pick Up' in the app.
    """
    from twilio.rest import Client
    import asyncio
    from app.services.conference_registry import new_conference_name, register_conference
    conference_name = new_conference_name('pickup')
    if contractor_id:
        await register_conference(conference_name, contractor_id, call_sid)
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    response = VoiceResponse()
    response.say('One moment please.', voice='Polly.Matthew')
    dial = Dial(time_limit=5400)
    dial.conference(conference_name, start_conference_on_enter=True, end_conference_on_exit=True, beep=False, wait_url='http://twimlets.com/holdmusic?Bucket=com.twilio.music.soft-rock')
    response.append(dial)
    conf_twiml = str(response)
    from app.db.cache import update_active_call
    await update_active_call(call_sid, {'accepted': True, 'conference_name': conference_name})
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda : client.calls(call_sid).update(twiml=conf_twiml))
    logger.info(f'Caller redirected to conference: {conference_name}')
    access_token = _generate_access_token(contractor_id=contractor_id)
    return {'status': 'ok', 'conference_name': conference_name, 'access_token': access_token}

# 407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc:app/webhooks/twilio_incoming.py:625
# Original function SHA-256: 732ce1da21424dd05d8a4cb84835e2411c4c4282a42ce83bbe3e3cddd53036f4
async def legacy_ring_contractor(call_sid: str, caller_phone: str, caller_name: str, conference_name: str, contractor_id: str=''):
    """Send VoIP push to ring the contractor, with 20-second timeout to Kevin takeover."""
    try:
        from app.services.push_notification import send_voip_push, get_device_token
        from app.api.voip import _generate_access_token
        device_token = await get_device_token(token_type='voip', contractor_id=contractor_id)
        if not device_token:
            logger.warning('No VoIP token — falling back to Kevin screening')
            await _async_redirect_to_kevin(call_sid)
            return
        access_token = _generate_access_token(contractor_id=contractor_id)
        await send_voip_push(device_token=device_token, caller_phone=caller_phone, caller_name=caller_name, call_sid=call_sid, conference_name=conference_name, access_token=access_token, contractor_id=contractor_id)
        logger.info(f"VoIP push sent for known contact: {(caller_name[:1] if caller_name else '')}*** ({redact_phone(caller_phone)})")
        from app.db.cache import _init_firebase
        from firebase_admin import db as rtdb
        from twilio.rest import Client
        _init_firebase()
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        loop = asyncio.get_event_loop()
        for _ in range(10):
            await asyncio.sleep(2)
            try:
                ref = rtdb.reference(f'/call_commands/{call_sid}')
                command = await loop.run_in_executor(None, ref.get)
                if command and command.get('type') == 'take_message':
                    await loop.run_in_executor(None, ref.delete)
                    logger.info('Contractor declined — Kevin taking over')
                    await _async_redirect_to_kevin(call_sid)
                    return
            except Exception:
                pass
            try:
                conferences = await loop.run_in_executor(None, lambda : client.conferences.list(friendly_name=conference_name, status='in-progress'))
                if conferences:
                    participants = await loop.run_in_executor(None, lambda : conferences[0].participants.list())
                    if len(participants) >= 2:
                        logger.info('Contractor answered — call connected')
                        return
            except Exception:
                pass
        logger.info("Contractor didn't answer in 20s — Kevin taking over")
        try:
            await _async_redirect_to_kevin(call_sid)
        except Exception as e:
            logger.error(f'Redirect to Kevin failed: {e}')
            await _async_redirect_to_kevin(call_sid)
    except Exception as e:
        logger.error(f'Ring contractor failed: {e}')
        await _async_redirect_to_kevin(call_sid)

# 407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc:app/services/voice_pipeline.py:2002
# Original function SHA-256: 17a9b3aa0e675c3ca58f28b50e4fc748f28a11caa12926f90e895bcbfb2ca17f
async def legacy_voice_commands(self):
    """Check RTDB for pending commands (decline, take_message, hangup)."""
    if not self._call_sid:
        return
    try:
        from firebase_admin import db as rtdb
        from app.db.cache import _init_firebase
        _init_firebase()
        ref = rtdb.reference(f'/call_commands/{self._call_sid}')
        loop = asyncio.get_event_loop()
        command = await loop.run_in_executor(None, ref.get)
        if command:
            await loop.run_in_executor(None, ref.delete)
            cmd_type = command.get('type', '')
            if cmd_type == 'take_message' and (not self._unavailable_said):
                if self._unavailable_task:
                    self._unavailable_task.cancel()
                asyncio.create_task(self._unavailable_now())
    except Exception:
        pass

# 407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc:app/services/gemini_pipeline.py:1894
# Original function SHA-256: dd8a684cdf213de3f68d33136062430d5d3b691b8cc4e18b8ed03e0cc2ca1347
async def legacy_gemini_commands(self):
    """Check for pending commands."""
    if not self._call_sid:
        return
    try:
        from app.db.cache import _init_firebase
        from firebase_admin import db as rtdb
        _init_firebase()
        ref = rtdb.reference(f'/call_commands/{self._call_sid}')
        loop = asyncio.get_event_loop()
        command = await loop.run_in_executor(None, ref.get)
        if command:
            await loop.run_in_executor(None, ref.delete)
            cmd_type = command.get('type', '')
            if cmd_type == 'take_message' and (not self._unavailable_said):
                if self._unavailable_task:
                    self._unavailable_task.cancel()
                if not self._ws:
                    logger.warning('take_message: Gemini WS not open — cannot inject')
                    return
                owner_name = self._contractor_config.get('owner_name', settings.user_name)
                try:
                    self._finish_owner_availability_wait()
                    await self._send_client_instruction(f'The owner ({owner_name}) has declined the call. Tell the caller they are unavailable and offer to take a message. Be warm and apologetic.')
                    self._unavailable_said = True
                    logger.info(f'take_message injected into Gemini for {self._call_sid[:8]}')
                except Exception as e:
                    self._log_voice_timing('take_message_instruction_error', exception_type=type(e).__name__)
                    self._assistant_instruction_pending = False
    except Exception as e:
        self._log_voice_timing('command_check_error', exception_type=type(e).__name__)

# 407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc:app/services/relay_pipeline.py:835
# Original function SHA-256: 6976cb8d6bc4be3382310a0970f67942041b84ac60e10339cbdd2767f8c12a45
async def legacy_relay_commands(self):
    if not self._call_sid:
        return
    try:
        from app.db.cache import _init_firebase
        from firebase_admin import db as rtdb
        _init_firebase()
        ref = rtdb.reference(f'/call_commands/{self._call_sid}')
        loop = asyncio.get_event_loop()
        command = await loop.run_in_executor(None, ref.get)
        if not command:
            return
        await loop.run_in_executor(None, ref.delete)
        if command.get('type') == 'take_message' and (not self._unavailable_said):
            self._unavailable_said = True
            owner_name = self._contractor_config.get('owner_name', settings.user_name)
            await self._supersede_in_flight()
            self._start_generation(extra_instruction=f'SYSTEM INSTRUCTION: The owner ({owner_name}) has declined the call. Tell the caller they are unavailable and offer to take a message. Be warm and apologetic.')
    except Exception as error:
        logger.error('relay_event event=command_check_error call=%s type=%s', _call_label(self._call_sid), type(error).__name__)

# 407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc:app/webhooks/twilio_incoming.py:706
# Original function SHA-256: d077556be38061c99b622f530caf5b1bf41af9fe6afefc612c9225faa12f9f05
async def legacy_redirect_to_kevin(call_sid: str):
    """Redirect a call from conference to Kevin's screening stream (async version)."""
    try:
        from twilio.rest import Client
        from app.db.cache import update_active_call
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        ws_token = secrets.token_urlsafe(32)
        await update_active_call(call_sid, {'ws_token': ws_token})
        ws_url = settings.cloud_run_url.replace('https://', 'wss://')
        response = VoiceResponse()
        response.say('Thanks for holding. Let me connect you with our assistant.', voice='Polly.Matthew')
        connect = Connect()
        stream = connect.stream(url=f'{ws_url}/media-stream/{call_sid}')
        stream.parameter(name='ws_token', value=ws_token)
        response.append(connect)
        twiml_str = str(response)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda : client.calls(call_sid).update(twiml=twiml_str))
        logger.info(f'Call {call_sid} redirected to Kevin screening')
    except Exception as e:
        logger.error(f'Redirect to Kevin failed: {e}')
