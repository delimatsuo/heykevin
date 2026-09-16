"""Durable, exact-call arbitration. RTDB callbacks are pure and never delete rejects."""
from __future__ import annotations

import asyncio
import math
import re
import secrets
import time
from typing import Optional

from fastapi import HTTPException
from app.config import settings
from app.db.cache import ACTIVE_CALLS_PATH, STALE_THRESHOLD, _init_firebase
from app.services.conference_registry import get_conference_binding, new_conference_name, register_conference

ACTION_ACCEPT = 'accept'
ACTION_DECLINE = 'decline'
ACTION_TIMEOUT = 'timeout'
STATUS_READY = 'ready'
STATUS_ACCEPTING = 'accepting'
STATUS_ACCEPTED = 'accepted'
STATUS_MESSAGE_REQUESTED = 'message_requested'
STATUS_TAKING_MESSAGE = 'taking_message'
STATUS_UNCERTAIN = 'uncertain'
STATUS_PREPARATION_FAILED = 'preparation_failed'
STATUS_ENDED = 'ended'
STATUS_ACTION_CONFLICT = 'action_conflict'
LIVE_STATES = frozenset({'pending', 'scoring', 'screening', 'pickup_ringing', 'connected', 'text_replied', 'voicemail_recording'})
DECISION_STATES = frozenset({'screening', 'pickup_ringing'})


def validate_call_sid(call_sid):
    if not isinstance(call_sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', call_sid):
        raise HTTPException(400, 'Invalid call ID')


def live_record(record, contractor_id, now=None):
    now = time.time() if now is None else now
    if not isinstance(record, dict) or not isinstance(contractor_id, str) or not contractor_id.strip():
        return False
    if record.get('contractor_id') != contractor_id or record.get('state') not in LIVE_STATES:
        return False
    stamp = record.get('state_updated_at')
    return (isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
            and math.isfinite(stamp) and 0 < stamp <= now and now - stamp <= STALE_THRESHOLD)


def reduce_active_call_action(current_data, *, action, contractor_id, operation_id,
                              claim_nonce, conference_name='', now=0.0):
    """Return unchanged data on rejection: Firebase commits None as deletion."""
    if not live_record(current_data, contractor_id, now):
        return current_data, {'outcome': 'inactive'}
    if current_data.get('owner_action'):
        match = (current_data.get('owner_action') == action and
                 current_data.get('owner_operation_id') == operation_id)
        return current_data, {'outcome': 'duplicate' if match else 'action_conflict'}
    if (current_data.get('state') not in DECISION_STATES or current_data.get('accepted')
            or current_data.get('kevin_redirect_status') in {'pending', 'uncertain'}):
        return current_data, {'outcome': 'action_conflict'}
    if action not in {ACTION_ACCEPT, ACTION_DECLINE, ACTION_TIMEOUT}:
        return current_data, {'outcome': 'unsupported_action'}
    updated = dict(current_data)
    updated.update(owner_action=action, owner_operation_id=operation_id, claim_nonce=claim_nonce,
                   action_updated_at=now, owner_action_status=STATUS_ACCEPTING if action == ACTION_ACCEPT else STATUS_MESSAGE_REQUESTED)
    if action == ACTION_ACCEPT:
        updated.update(conference_name=conference_name, accepted=False,
                       previous_conference_name=current_data.get('conference_name', ''))
    else:
        updated['message_intent'] = {'type': 'take_message', 'contractor_id': contractor_id,
                                     'operation_id': operation_id, 'action': action}
    return updated, {'outcome': 'claimed'}


async def _run_rtdb_transaction(call_sid, transaction_fn):
    validate_call_sid(call_sid)
    _init_firebase()
    from firebase_admin import db
    return await asyncio.to_thread(db.reference(f'{ACTIVE_CALLS_PATH}/{call_sid}').transaction, transaction_fn)


async def read_record(call_sid):
    validate_call_sid(call_sid)
    _init_firebase()
    from firebase_admin import db
    return await asyncio.to_thread(db.reference(f'{ACTIVE_CALLS_PATH}/{call_sid}').get)


def _require_live(record, contractor_id):
    if isinstance(record, dict) and record.get('contractor_id') and record['contractor_id'] != contractor_id:
        raise HTTPException(403, 'Access denied')
    if not live_record(record, contractor_id):
        raise HTTPException(409 if record else 404, 'Call unavailable')


def _same_claim(record, expected, now):
    return (live_record(record, expected.get('contractor_id'), now)
            and all(record.get(k) == expected.get(k) for k in
                    ('owner_action', 'owner_operation_id', 'claim_nonce', 'conference_name')))


async def _finish(call_sid, expected, target_status):
    now = time.time()
    def txn(current):
        if (not _same_claim(current, expected, now)
                or expected.get('owner_action_status') not in {STATUS_ACCEPTING, STATUS_UNCERTAIN}
                or current.get('owner_action_status') != expected.get('owner_action_status')):
            return current
        return {**current, 'owner_action_status': target_status, 'action_updated_at': now}
    result = await _run_rtdb_transaction(call_sid, txn)
    return result if _same_claim(result, expected, time.time()) and result.get('owner_action_status') == target_status else None



async def _mark_redirect_started(call_sid, expected):
    # The legacy teardown marker must not suppress post-call work during preparation.
    now = time.time()
    def txn(current):
        if not _same_claim(current, expected, now) or current.get('owner_action_status') != STATUS_ACCEPTING:
            return current
        return {**current, 'accepted': True, 'redirect_started_at': now}
    result = await _run_rtdb_transaction(call_sid, txn)
    if not (_same_claim(result, expected, time.time()) and result.get('owner_action_status') == STATUS_ACCEPTING
            and result.get('accepted') is True and result.get('redirect_started_at') == now):
        raise HTTPException(409, 'Call unavailable')


async def _release_preparation(call_sid, expected):
    """Release only a claim whose provider redirect has definitely not started."""
    now = time.time()
    def txn(current):
        if (not _same_claim(current, expected, now)
                or current.get('owner_action_status') != STATUS_ACCEPTING
                or current.get('accepted') is True
                or current.get('redirect_started_at') is not None):
            return current
        updated = dict(current)
        updated['conference_name'] = updated.pop('previous_conference_name', '')
        updated['accepted'] = False
        updated['preparation_release_nonce'] = expected['claim_nonce']
        for key in ('owner_action', 'owner_action_status', 'owner_operation_id', 'claim_nonce', 'action_updated_at', 'redirect_started_at'):
            updated.pop(key, None)
        return updated
    result = await _run_rtdb_transaction(call_sid, txn)
    if not (live_record(result, expected.get('contractor_id'))
            and not result.get('owner_action') and result.get('accepted') is False
            and result.get('redirect_started_at') is None
            and result.get('preparation_release_nonce') == expected['claim_nonce']):
        raise RuntimeError('preparation_release_unconfirmed')


def _generate_access_token(contractor_id=''):
    from twilio.jwt.access_token import AccessToken
    from twilio.jwt.access_token.grants import VoiceGrant
    token = AccessToken(settings.twilio_account_sid, settings.twilio_api_key_sid,
                        settings.twilio_api_key_secret, identity=f'contractor_{contractor_id}', ttl=120)
    token.add_grant(VoiceGrant(outgoing_application_sid=settings.twilio_twiml_app_sid, incoming_allow=True))
    jwt = token.to_jwt()
    return jwt if isinstance(jwt, str) else jwt.decode()


async def _redirect(call_sid, conference_name):
    from twilio.rest import Client
    from twilio.twiml.voice_response import Dial, VoiceResponse
    response = VoiceResponse()
    response.say('One moment please.', voice='Polly.Matthew')
    dial = Dial(time_limit=5400)
    dial.conference(conference_name, start_conference_on_enter=True, end_conference_on_exit=True, beep=False)
    response.append(dial)
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    await asyncio.to_thread(lambda: client.calls(call_sid).update(twiml=str(response)))


async def _conference_contains_call(conference_name, call_sid, contractor_id):
    """Read only: a registered conference must contain this exact live caller."""
    async with asyncio.timeout(5):
        binding = await get_conference_binding(conference_name)
        if not binding or binding.get('contractor_id') != contractor_id or binding.get('call_sid') != call_sid:
            return False
        from twilio.rest import Client
        from twilio.http.http_client import TwilioHttpClient
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token,
                        http_client=TwilioHttpClient(timeout=2))
        def lookup():
            conferences = client.conferences.list(friendly_name=conference_name, status='in-progress', limit=1)
            for conference in conferences:
                if conference.friendly_name != conference_name or conference.status != 'in-progress':
                    continue
                participants = client.conferences(conference.sid).participants.list(limit=20)
                if any(p.call_sid == call_sid and p.status == 'connected' for p in participants):
                    return True
            return False
        return await asyncio.to_thread(lookup)


def _response(record, call_sid, contractor_id, operation_id, action=None, credentials=False):
    status = record.get('owner_action_status') or STATUS_READY
    response = dict(call_sid=call_sid, contractor_id=contractor_id, operation_id=operation_id,
                    action=record.get('owner_action', '') if action is None else action,
                    action_status=status, status='pending' if status in {STATUS_ACCEPTING, STATUS_UNCERTAIN, STATUS_MESSAGE_REQUESTED} else 'ok',
                    active=True, urgent=record.get('urgent') is True, caller_name=record.get('caller_name', ''),
                    caller_phone=record.get('caller_phone', ''), transcript=record.get('transcript_buffer', ''))
    if credentials and status == STATUS_ACCEPTED:
        response.update(conference_name=record['conference_name'], access_token=_generate_access_token(contractor_id))
    return response


async def handle_owner_call_action(*, call_sid, contractor_id, action, operation_id=None, message=''):
    validate_call_sid(call_sid)
    if not contractor_id or not contractor_id.strip():
        raise HTTPException(403, 'Access denied')
    op = (operation_id or f'legacy_{action}').strip()
    if not op or len(op) > 80:
        raise HTTPException(400, 'Invalid operation ID')
    # Existing voicemail/text behavior remains separate, with strict live authorization.
    if action in {'voicemail', 'text_reply'}:
        current = await read_record(call_sid)
        _require_live(current, contractor_id)
        from app.api.voip import _handle_voicemail, _handle_text_reply
        return (await _handle_voicemail(call_sid) if action == 'voicemail' else
                await _handle_text_reply(call_sid, message, contractor_id)), 200
    if action not in {ACTION_ACCEPT, ACTION_DECLINE}:
        raise HTTPException(400, 'Unsupported action')
    nonce, now = secrets.token_hex(16), time.time()
    conference = new_conference_name('pickup') if action == ACTION_ACCEPT else ''
    def txn(current):
        return reduce_active_call_action(current, action=action, contractor_id=contractor_id,
            operation_id=op, claim_nonce=nonce, conference_name=conference, now=now)[0]
    committed = await _run_rtdb_transaction(call_sid, txn)
    _require_live(committed, contractor_id)
    if committed.get('owner_operation_id') != op or committed.get('owner_action') != action:
        raise HTTPException(409, 'action_conflict')
    if action == ACTION_ACCEPT and committed.get('claim_nonce') == nonce:
        try:
            await register_conference(conference, contractor_id, call_sid)
            binding = await get_conference_binding(conference)
            if not binding or binding.get('contractor_id') != contractor_id or binding.get('call_sid') != call_sid:
                raise RuntimeError('binding_unavailable')
            _generate_access_token(contractor_id)  # Validate credentials before provider mutation.
            current = await read_record(call_sid)
            if not _same_claim(current, committed, time.time()) or current.get('owner_action_status') != STATUS_ACCEPTING:
                raise HTTPException(409, 'Call unavailable')
            await _mark_redirect_started(call_sid, committed)
        except HTTPException:
            raise
        except Exception:
            # Definitely no provider redirect occurred. Only a later deliberate POST may retry.
            try:
                await _release_preparation(call_sid, committed)
            except Exception:
                # Unconfirmed rollback retains accepting and cannot be automatically replayed.
                return _response(committed, call_sid, contractor_id, op), 202
            failure = _response(committed, call_sid, contractor_id, op)
            failure.update(status='error', action_status=STATUS_PREPARATION_FAILED, retryable=True)
            return failure, 503
        try:
            await _redirect(call_sid, conference)
        except Exception:
            try:
                await _finish(call_sid, committed, STATUS_UNCERTAIN)
            except Exception:
                pass  # Durable accepting still prevents another redirect.
            return _response({**committed, 'owner_action_status': STATUS_UNCERTAIN}, call_sid, contractor_id, op), 202
        try:
            final = await _finish(call_sid, committed, STATUS_ACCEPTED)
        except Exception:
            final = None
        if final is None:
            # Never issue credentials if the call ended/reassigned or finalization is unconfirmed.
            return _response({**committed, 'owner_action_status': STATUS_UNCERTAIN}, call_sid, contractor_id, op), 202
        committed = final
    if action == ACTION_DECLINE and committed.get('owner_action') == ACTION_DECLINE:
        try:
            from app.services.legacy_call_commands import publish_message_intent
            await publish_message_intent(call_sid, committed)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
    result = _response(committed, call_sid, contractor_id, op, credentials=True)
    return result, 202 if result['status'] == 'pending' else 200


async def get_owner_call_action_status(*, call_sid, contractor_id, operation_id=''):
    validate_call_sid(call_sid)
    if not contractor_id or not contractor_id.strip():
        raise HTTPException(403, 'Access denied')
    op = operation_id or ''
    if len(op) > 80:
        raise HTTPException(400, 'Invalid operation ID')
    record = await read_record(call_sid)
    if isinstance(record, dict) and record.get('contractor_id') and record['contractor_id'] != contractor_id:
        raise HTTPException(403, 'Access denied')
    if not live_record(record, contractor_id):
        return dict(call_sid=call_sid, contractor_id=contractor_id, operation_id=op, action='',
                    action_status=STATUS_ENDED, status='ok', active=False, urgent=False,
                    caller_name='', caller_phone='', transcript='')
    if (op and op == record.get('owner_operation_id') and record.get('owner_action') == ACTION_ACCEPT
            and record.get('owner_action_status') in {STATUS_ACCEPTING, STATUS_UNCERTAIN}
            and record.get('conference_name')):
        try:
            if await _conference_contains_call(record['conference_name'], call_sid, contractor_id):
                await _finish(call_sid, record, STATUS_ACCEPTED)
        except Exception:
            pass  # No evidence is not permission to redirect again or issue credentials.
        # Lookup may race caller end, account reassignment, or another finalizer.
        record = await read_record(call_sid)
        if isinstance(record, dict) and record.get('contractor_id') and record['contractor_id'] != contractor_id:
            raise HTTPException(403, 'Access denied')
        if not live_record(record, contractor_id):
            return dict(call_sid=call_sid, contractor_id=contractor_id, operation_id=op, action='',
                        action_status=STATUS_ENDED, status='ok', active=False, urgent=False,
                        caller_name='', caller_phone='', transcript='')
    result = _response(record, call_sid, contractor_id, op)
    if op and op != record.get('owner_operation_id'):
        result.update(status='error', action_status=STATUS_ACTION_CONFLICT)
    elif op and record.get('owner_action_status') == STATUS_ACCEPTED:
        result = _response(record, call_sid, contractor_id, op, credentials=True)
    return result


async def arbitrate_owner_timeout(call_sid, contractor_id):
    validate_call_sid(call_sid)
    nonce, now = secrets.token_hex(16), time.time()
    def txn(current):
        return reduce_active_call_action(current, action=ACTION_TIMEOUT, contractor_id=contractor_id,
            operation_id=f'timeout_{call_sid}'[:80], claim_nonce=nonce, now=now)[0]
    record = await _run_rtdb_transaction(call_sid, txn)
    won = (live_record(record, contractor_id) and record.get('owner_action') == ACTION_TIMEOUT
           and record.get('owner_action_status') in {STATUS_MESSAGE_REQUESTED, STATUS_TAKING_MESSAGE})
    if won and record.get('owner_action_status') == STATUS_MESSAGE_REQUESTED:
        try:
            from app.services.legacy_call_commands import publish_message_intent
            await publish_message_intent(call_sid, record)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
    return won


def message_intent_from_record(record, contractor_id, *, statuses=(STATUS_MESSAGE_REQUESTED,),
                               operation_id=None, action=None, claim_nonce=None,
                               ws_token='', conference_name=None, now=None):
    """Read one exact durable message claim without granting new authority."""
    if (not live_record(record, contractor_id, now) or record.get('accepted')
            or record.get('state') not in DECISION_STATES
            or record.get('kevin_redirect_status') in {'pending', 'uncertain'}
            or record.get('owner_action_status') not in statuses):
        return None
    intent = record.get('message_intent')
    if (not isinstance(intent, dict) or intent.get('type') != 'take_message'
            or intent.get('contractor_id') != contractor_id
            or intent.get('action') not in {ACTION_DECLINE, ACTION_TIMEOUT}
            or intent.get('action') != record.get('owner_action')
            or not isinstance(intent.get('operation_id'), str) or not intent['operation_id']
            or intent['operation_id'] != record.get('owner_operation_id')
            or not isinstance(record.get('claim_nonce'), str) or not record['claim_nonce']):
        return None
    if ((operation_id is not None and intent['operation_id'] != operation_id)
            or (action is not None and intent['action'] != action)
            or (claim_nonce is not None and record['claim_nonce'] != claim_nonce)
            or (ws_token and record.get('ws_token') != ws_token)
            or (conference_name is not None and record.get('conference_name', '') != conference_name)):
        return None
    return {**intent, 'claim_nonce': record['claim_nonce']}


async def pending_message_intent(call_sid, contractor_id, *, conference_name=None):
    record = await read_record(call_sid)
    return message_intent_from_record(record, contractor_id, conference_name=conference_name)


async def acknowledge_owner_action(call_sid, new_status=STATUS_TAKING_MESSAGE, *, contractor_id,
                                   operation_id, action, ws_token='', claim_nonce=None):
    if new_status != STATUS_TAKING_MESSAGE:
        return False
    now = time.time()
    def matches(current):
        return message_intent_from_record(current, contractor_id,
            statuses=(STATUS_MESSAGE_REQUESTED, STATUS_TAKING_MESSAGE),
            operation_id=operation_id, action=action, claim_nonce=claim_nonce,
            ws_token=ws_token, now=now)
    def txn(current):
        if not matches(current):
            return current
        return {**current, 'owner_action_status': STATUS_TAKING_MESSAGE, 'action_acknowledged_at': now}
    result = await _run_rtdb_transaction(call_sid, txn)
    return bool(matches(result) and result.get('owner_action_status') == new_status)


def _pipeline_cid(pipeline) -> str:
    cfg = getattr(pipeline, '_contractor_config', None)
    if isinstance(cfg, dict):
        return cfg.get('contractor_id', '') or ''
    return ''


def _pipeline_sid(pipeline) -> str:
    return getattr(pipeline, '_call_sid', '') or ''


def _pipeline_ws(pipeline) -> str:
    return getattr(pipeline, '_command_ws_token', '') or ''


def _pipeline_alive(pipeline) -> bool:
    if hasattr(pipeline, '_connected') and not pipeline._connected:
        return False
    if hasattr(pipeline, '_active') and not pipeline._active:
        return False
    if getattr(pipeline, '_ending', False):
        return False
    return True


async def consume_message_intent(pipeline, deliver):
    lock = getattr(pipeline, '_message_delivery_lock', None)
    if lock is None:
        lock = pipeline._message_delivery_lock = asyncio.Lock()
    async with lock:
        return await _consume_message_intent(pipeline, deliver)


async def _consume_message_intent(pipeline, deliver):
    """Fence delivery and acknowledgment to the same claim and authenticated stream."""
    cid = _pipeline_cid(pipeline)
    sid = _pipeline_sid(pipeline)
    ws_token = _pipeline_ws(pipeline)
    if not cid or not sid or not ws_token or not _pipeline_alive(pipeline):
        return False

    continuation_guard = None

    legacy_snapshot = None
    if sid and cid and ws_token:
        try:
            from app.services.legacy_call_commands import adopt_legacy_message_intent
            legacy_snapshot = await adopt_legacy_message_intent(sid, cid, ws_token=ws_token)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if (not _pipeline_alive(pipeline) or _pipeline_cid(pipeline) != cid
                or _pipeline_sid(pipeline) != sid or _pipeline_ws(pipeline) != ws_token):
            return False

    intent = await pending_message_intent(sid, cid)
    if (not _pipeline_alive(pipeline) or _pipeline_cid(pipeline) != cid
            or _pipeline_sid(pipeline) != sid or _pipeline_ws(pipeline) != ws_token):
        return False

    if not intent:
        # An acknowledged instruction may still need exact-command cleanup after
        # a lost cleanup response or a projection that arrived after acknowledgment.
        if legacy_snapshot:
            current = await read_record(sid)
            if (not _pipeline_alive(pipeline) or _pipeline_cid(pipeline) != cid
                    or _pipeline_sid(pipeline) != sid or _pipeline_ws(pipeline) != ws_token):
                return False
            expected = legacy_snapshot.intent
            completed = message_intent_from_record(current, cid, statuses=(STATUS_TAKING_MESSAGE,),
                operation_id=expected['operation_id'], action=expected['action'],
                claim_nonce=expected['claim_nonce'], ws_token=ws_token)
            if completed:
                await _cleanup_legacy_snapshot(sid, legacy_snapshot)
        return False

    current = await read_record(sid)
    if (not _pipeline_alive(pipeline) or _pipeline_cid(pipeline) != cid
            or _pipeline_sid(pipeline) != sid or _pipeline_ws(pipeline) != ws_token):
        return False

    if not message_intent_from_record(current, cid, operation_id=intent['operation_id'],
            action=intent['action'], claim_nonce=intent['claim_nonce'], ws_token=ws_token):
        return False
    if legacy_snapshot and legacy_snapshot.intent != intent:
        return False

    key = (cid, sid, ws_token, intent['operation_id'], intent['action'], intent['claim_nonce'])
    if getattr(pipeline, '_intent_accepted_key', None) == key:
        accepted_at = getattr(pipeline, '_intent_accepted_at', None)
        if accepted_at is None:
            accepted_at = time.monotonic()
            pipeline._intent_accepted_at = accepted_at
    else:
        accepted_at = time.monotonic()
        pipeline._intent_accepted_key = key
        pipeline._intent_accepted_at = accepted_at

    intent_copy = dict(intent)
    intent_copy['accepted_at'] = accepted_at

    async def _guard(*, allow_acknowledged: bool = False) -> bool:
        if not _pipeline_alive(pipeline):
            return False
        if _pipeline_cid(pipeline) != cid or _pipeline_sid(pipeline) != sid or _pipeline_ws(pipeline) != ws_token:
            return False
        rec = await read_record(sid)
        if not _pipeline_alive(pipeline):
            return False
        if _pipeline_cid(pipeline) != cid or _pipeline_sid(pipeline) != sid or _pipeline_ws(pipeline) != ws_token:
            return False
        statuses = (STATUS_MESSAGE_REQUESTED, STATUS_TAKING_MESSAGE) if allow_acknowledged else (STATUS_MESSAGE_REQUESTED,)
        if not message_intent_from_record(rec, cid, statuses=statuses, operation_id=intent['operation_id'],
                action=intent['action'], claim_nonce=intent['claim_nonce'], ws_token=ws_token):
            return False
        return True

    pipeline._message_delivery_guard = _guard
    try:
        if getattr(pipeline, '_message_delivery_key', None) != key:
            pipeline._message_taking_pending = True
            prepare_fn = getattr(pipeline, '_prepare_message_delivery', None)
            if prepare_fn is not None and callable(prepare_fn):
                prepared = await prepare_fn(intent_copy)
                if prepared is False:
                    return False
            if not await _guard():
                return False
            delivered = await deliver()
            if delivered is False:
                return False
            pipeline._message_delivery_key = key

        if not await _guard():
            return False

        async def _continuation() -> bool:
            return await _guard(allow_acknowledged=True)

        continuation_guard = _continuation

        acked = await acknowledge_owner_action(sid, contractor_id=cid,
            operation_id=intent['operation_id'], action=intent['action'], ws_token=ws_token,
            claim_nonce=intent['claim_nonce'])
        if acked and legacy_snapshot:
            await _cleanup_legacy_snapshot(sid, legacy_snapshot)
        return acked
    finally:
        pipeline._message_taking_pending = False
        if hasattr(pipeline, '_message_delivery_guard'):
            try:
                del pipeline._message_delivery_guard
            except AttributeError:
                pass
        pipeline._message_continuation_guard = continuation_guard
        try:
            finish_fn = getattr(pipeline, '_finish_message_delivery_attempt', None)
            if finish_fn is not None and callable(finish_fn):
                try:
                    finish_fn()
                except Exception:
                    pass
        finally:
            if hasattr(pipeline, '_message_continuation_guard'):
                try:
                    del pipeline._message_continuation_guard
                except AttributeError:
                    pass
            if hasattr(pipeline, '_message_delivery_guard'):
                try:
                    del pipeline._message_delivery_guard
                except AttributeError:
                    pass


async def _cleanup_legacy_snapshot(call_sid, snapshot):
    try:
        from app.services.legacy_call_commands import delete_legacy_command_conditional
        await delete_legacy_command_conditional(call_sid, snapshot.command)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass  # Future polls may retry cleanup; instruction delivery stays acknowledged.


async def confirm_fallback_stream(call_sid, *, contractor_id, ws_token, redirect_nonce):
    """Authenticated stream arrival proves this exact fallback reached Twilio."""
    validate_call_sid(call_sid)
    if not contractor_id or not ws_token or not redirect_nonce:
        return False
    now = time.time()
    def matches(current):
        return (live_record(current, contractor_id, now)
                and current.get('call_sid') == call_sid
                and current.get('ws_token') == ws_token
                and current.get('kevin_redirect_nonce') == redirect_nonce
                and current.get('kevin_redirect_status') in {'pending', 'succeeded'}
                and not current.get('accepted') and current.get('owner_action') != ACTION_ACCEPT)
    def confirm(current):
        if not matches(current) or current.get('kevin_redirect_status') != 'pending':
            return current
        return {**current, 'kevin_redirect_status': 'succeeded', 'fallback_stream_confirmed_at': now}
    result = await _run_rtdb_transaction(call_sid, confirm)
    return bool(matches(result) and result.get('kevin_redirect_status') == 'succeeded')


async def ensure_owner_deadline(call_sid, contractor_id, *, urgent=False, deadline=None):
    """The first wait wins; repeated hold/urgency never extends its deadline."""
    now = time.time()
    requested_deadline = min(deadline, now + 30) if deadline is not None else now + 30
    def txn(current):
        if (not live_record(current, contractor_id, now) or current.get('owner_action')
                or current.get('accepted') or current.get('state') not in DECISION_STATES):
            return current
        old = current.get('owner_wait_deadline')
        retained = old if isinstance(old, (int, float)) and math.isfinite(old) and old > 0 else requested_deadline
        return {**current, 'owner_wait_deadline': min(retained, requested_deadline), 'urgent': urgent or current.get('urgent') is True}
    record = await _run_rtdb_transaction(call_sid, txn)
    if (not live_record(record, contractor_id) or record.get('owner_action')
            or record.get('accepted') or record.get('state') not in DECISION_STATES):
        return None
    return record.get('owner_wait_deadline')


async def run_owner_timeout(pipeline, deliver):
    """Keep retrying unavailable storage without crashing or abandoning a live caller."""
    sid, cid = getattr(pipeline, '_call_sid', ''), getattr(pipeline, '_contractor_config', {}).get('contractor_id', '')
    if getattr(pipeline, '_unavailable_said', False):
        return
    # Public/local pipelines without an authenticated real call keep ordinary hold behavior.
    if not sid or not cid:
        await asyncio.sleep(pipeline.OWNER_AVAILABILITY_TIMEOUT_SECONDS)
        if getattr(pipeline, '_connected', getattr(pipeline, '_active', False)):
            await deliver()
        return
    deadline = None
    first_deadline = time.time() + pipeline.OWNER_AVAILABILITY_TIMEOUT_SECONDS
    while getattr(pipeline, '_connected', getattr(pipeline, '_active', False)) and not getattr(pipeline, '_ending', False):
        try:
            if deadline is None:
                deadline = await ensure_owner_deadline(sid, cid, deadline=first_deadline)
                if deadline is None:
                    current = await read_record(sid)
                    if (live_record(current, cid) and current.get('owner_action_status') in {STATUS_ACCEPTING, STATUS_UNCERTAIN}):
                        await asyncio.sleep(2)
                        continue
                    await consume_message_intent(pipeline, deliver)
                    return
            await asyncio.sleep(max(0, deadline - time.time()))
            if await arbitrate_owner_timeout(sid, cid):
                await consume_message_intent(pipeline, deliver)
                if getattr(pipeline, '_message_delivery_key', None):
                    return
            else:
                current = await read_record(sid)
                if not (live_record(current, cid) and current.get('owner_action_status') in {STATUS_ACCEPTING, STATUS_UNCERTAIN}):
                    await consume_message_intent(pipeline, deliver)
                    return
                # A definite preparation release may follow this pending pickup.
                # Keep the original elapsed deadline until the action settles.
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(2)
