"""Conservative delivery compatibility with legacy, non-acknowledging consumers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import secrets
import time

from app.db.cache import _init_firebase

CALL_COMMANDS_PATH = '/call_commands'
ENRICHED_KEYS = frozenset({'call_sid', 'contractor_id', 'operation_id', 'action', 'claim_nonce'})


@dataclass(frozen=True)
class AdoptedLegacyCommand:
    command: dict
    intent: dict


async def _run_legacy_command_transaction(call_sid, transaction_fn):
    from app.services.owner_call_actions import validate_call_sid
    validate_call_sid(call_sid)
    _init_firebase()
    from firebase_admin import db
    return await asyncio.to_thread(db.reference(f'{CALL_COMMANDS_PATH}/{call_sid}').transaction, transaction_fn)


async def read_legacy_command(call_sid):
    from app.services.owner_call_actions import validate_call_sid
    validate_call_sid(call_sid)
    _init_firebase()
    from firebase_admin import db
    return await asyncio.to_thread(db.reference(f'{CALL_COMMANDS_PATH}/{call_sid}').get)


async def delete_legacy_command_conditional(call_sid, expected_snapshot):
    def txn(current):
        return None if isinstance(current, dict) and current == expected_snapshot else current
    return await _run_legacy_command_transaction(call_sid, txn)


def _projection_matches(record, nonce):
    projection = record.get('legacy_command_projection') if isinstance(record, dict) else None
    return isinstance(projection, dict) and projection.get('nonce') == nonce


def _expected_message(record, expected, *, now=None, acknowledged=False):
    from app.services.owner_call_actions import (
        STATUS_MESSAGE_REQUESTED, STATUS_TAKING_MESSAGE, message_intent_from_record,
    )
    if not isinstance(expected, dict):
        return None
    intent = message_intent_from_record(record, expected.get('contractor_id'),
        statuses=(STATUS_MESSAGE_REQUESTED, STATUS_TAKING_MESSAGE) if acknowledged else (STATUS_MESSAGE_REQUESTED,),
        operation_id=expected.get('owner_operation_id'), action=expected.get('owner_action'),
        claim_nonce=expected.get('claim_nonce'),
        conference_name=expected.get('conference_name', ''), now=now)
    if not intent or record.get('ws_token', '') != expected.get('ws_token', ''):
        return None
    return intent


async def _finish_projection(call_sid, expected, nonce, status):
    from app.services.owner_call_actions import _run_rtdb_transaction
    now = time.time()
    def txn(current):
        if (not _expected_message(current, expected, now=now, acknowledged=True)
                or not _projection_matches(current, nonce)):
            return current
        return {**current, 'legacy_command_projection': {'nonce': nonce, 'status': status, 'updated_at': now}}
    result = await _run_rtdb_transaction(call_sid, txn)
    return bool(_expected_message(result, expected, acknowledged=True)
                and _projection_matches(result, nonce)
                and result['legacy_command_projection']['status'] == status)


async def publish_message_intent(call_sid, expected_record):
    """One durable publication attempt; queue/deletion never means acceptance."""
    try:
        return await _publish_message_intent(call_sid, expected_record)
    except asyncio.CancelledError:
        raise
    except Exception:
        return False  # The original durable intent remains pending.


async def _publish_message_intent(call_sid, expected):
    from app.services.owner_call_actions import _run_rtdb_transaction, read_record, validate_call_sid
    validate_call_sid(call_sid)
    if not _expected_message(expected, expected):
        return False
    nonce, now = secrets.token_hex(16), time.time()
    def reserve(current):
        if (not _expected_message(current, expected, now=now)
                or 'legacy_command_projection' in current):
            return current
        return {**current, 'legacy_command_projection': {'nonce': nonce, 'status': 'attempting', 'updated_at': now}}
    committed = await _run_rtdb_transaction(call_sid, reserve)
    if not _projection_matches(committed, nonce) or not _expected_message(committed, expected):
        return False
    current = await read_record(call_sid)
    if not _projection_matches(current, nonce) or not _expected_message(current, expected):
        return False
    payload = dict(type='take_message', call_sid=call_sid, contractor_id=expected['contractor_id'],
                   operation_id=expected['owner_operation_id'], action=expected['owner_action'],
                   claim_nonce=expected['claim_nonce'])
    def publish(command):
        return payload if command is None else command
    try:
        result = await _run_legacy_command_transaction(call_sid, publish)
    except asyncio.CancelledError:
        raise
    except Exception:
        await _finish_projection(call_sid, expected, nonce, 'uncertain')
        return False
    published = result == payload
    recorded = await _finish_projection(call_sid, expected, nonce, 'published' if published else 'blocked')
    return published and recorded


def _live_binding(record, contractor_id, ws_token, conference_name, now=None):
    from app.services.owner_call_actions import DECISION_STATES, live_record
    return (live_record(record, contractor_id, now) and record.get('state') in DECISION_STATES
            and not record.get('accepted') and record.get('owner_action') != 'accept'
            and record.get('kevin_redirect_status') not in {'pending', 'uncertain'}
            and (not ws_token or record.get('ws_token') == ws_token)
            and (not conference_name or record.get('conference_name') == conference_name))


async def adopt_legacy_message_intent(call_sid, contractor_id, *, ws_token='', conference_name=''):
    """Translate a command using its authenticated consumer's pinned identity."""
    from app.services.owner_call_actions import (
        ACTION_DECLINE, ACTION_TIMEOUT, STATUS_MESSAGE_REQUESTED, STATUS_TAKING_MESSAGE,
        _run_rtdb_transaction, message_intent_from_record, read_record,
        reduce_active_call_action, validate_call_sid,
    )
    if not call_sid or not contractor_id or not contractor_id.strip() or not (ws_token or conference_name):
        return None
    validate_call_sid(call_sid)
    try:
        command = await read_legacy_command(call_sid)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    if not isinstance(command, dict) or command.get('type') != 'take_message':
        return None
    enriched = set(command) == ENRICHED_KEYS | {'type'}
    if not enriched and set(command) != {'type'}:
        return None
    if enriched and (command['call_sid'] != call_sid or command['contractor_id'] != contractor_id
            or command['action'] not in {ACTION_DECLINE, ACTION_TIMEOUT}
            or not isinstance(command['operation_id'], str) or not 0 < len(command['operation_id']) <= 80
            or not isinstance(command['claim_nonce'], str) or not command['claim_nonce']):
        return None

    def observed_intent(record):
        if not _live_binding(record, contractor_id, ws_token, conference_name):
            return None
        return message_intent_from_record(record, contractor_id,
            statuses=(STATUS_MESSAGE_REQUESTED, STATUS_TAKING_MESSAGE),
            operation_id=command['operation_id'] if enriched else None,
            action=command['action'] if enriched else None,
            claim_nonce=command['claim_nonce'] if enriched else None,
            ws_token=ws_token, conference_name=conference_name or None)

    record = await read_record(call_sid)
    if not _live_binding(record, contractor_id, ws_token, conference_name):
        return None
    if enriched or record.get('owner_action'):
        intent = observed_intent(record)
        return AdoptedLegacyCommand(dict(command), intent) if intent else None

    nonce, now = secrets.token_hex(16), time.time()
    def adopt(current):
        if not _live_binding(current, contractor_id, ws_token, conference_name, now):
            return current
        return reduce_active_call_action(current, action=ACTION_DECLINE, contractor_id=contractor_id,
            operation_id='legacy_decline', claim_nonce=nonce, now=now)[0]
    committed = await _run_rtdb_transaction(call_sid, adopt)
    intent = observed_intent(committed)
    if not intent or intent['action'] != ACTION_DECLINE or intent['operation_id'] != 'legacy_decline':
        return None
    return AdoptedLegacyCommand(dict(command), intent)
