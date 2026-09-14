"""Causal cross-revision probes: actually replace records and lose write responses."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.unit.test_legacy_call_command_compatibility import (
    f407, fresh_record, rtdb_env,
)
from app.services import legacy_call_commands as legacy
from app.services import owner_call_actions as actions
from app.webhooks import twilio_incoming as incoming


def seed_message(store):
    record, result = actions.reduce_active_call_action(
        fresh_record(), action='decline', contractor_id='owner',
        operation_id='op1', claim_nonce='original-claim', now=actions.time.time(),
    )
    assert result['outcome'] == 'claimed'
    store.active_calls['CA1'] = record
    return deepcopy(record)


def pipeline():
    return SimpleNamespace(
        _call_sid='CA1', _contractor_config={'contractor_id': 'owner'},
        _command_ws_token='token123',
    )


@pytest.mark.asyncio
async def test_projection_reservation_retry_loses_committed_nonce(rtdb_env):
    expected = seed_message(rtdb_env)
    competing = deepcopy(expected)
    competing['legacy_command_projection'] = {'nonce': 'competing', 'status': 'attempting'}
    rtdb_env.active_retry_with = competing
    assert not await legacy.publish_message_intent('CA1', expected)
    assert rtdb_env.active_calls['CA1'] == competing
    assert not rtdb_env.call_commands


@pytest.mark.parametrize('changed', [
    {'conference_name': 'replacement'}, {'ws_token': 'rotated'},
])
@pytest.mark.asyncio
async def test_projection_rechecks_generation_after_reservation(rtdb_env, monkeypatch, changed):
    expected = seed_message(rtdb_env)
    original = actions.read_record
    async def changed_read(sid):
        rtdb_env.active_calls[sid].update(changed)
        return await original(sid)
    monkeypatch.setattr(actions, 'read_record', changed_read)
    assert not await legacy.publish_message_intent('CA1', expected)
    assert not rtdb_env.call_commands


@pytest.mark.parametrize('changed', [
    {'claim_nonce': 'replacement-claim'}, {'owner_action_status': 'taking_message'},
])
@pytest.mark.asyncio
async def test_message_changed_between_reads_cannot_deliver(rtdb_env, monkeypatch, changed):
    seed_message(rtdb_env)
    original = actions.read_record
    reads = 0
    async def changed_read(sid):
        nonlocal reads
        reads += 1
        if reads == 2:
            rtdb_env.active_calls[sid].update(changed)
        return await original(sid)
    monkeypatch.setattr(actions, 'read_record', changed_read)
    deliver = AsyncMock(return_value=True)
    assert not await actions.consume_message_intent(pipeline(), deliver)
    deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_acknowledgment_cannot_settle_a_replacement_claim(rtdb_env):
    seed_message(rtdb_env)
    async def deliver():
        rtdb_env.active_calls['CA1']['claim_nonce'] = 'replacement-claim'
        return True
    assert not await actions.consume_message_intent(pipeline(), deliver)
    assert rtdb_env.active_calls['CA1']['owner_action_status'] == 'message_requested'


@pytest.mark.asyncio
async def test_ack_retry_rejects_replaced_intent_identity(rtdb_env):
    expected = seed_message(rtdb_env)
    replacement = deepcopy(expected)
    replacement['message_intent']['operation_id'] = 'another-operation'
    rtdb_env.active_retry_with = replacement
    assert not await actions.acknowledge_owner_action(
        'CA1', contractor_id='owner', operation_id='op1', action='decline', ws_token='token123',
    )
    assert rtdb_env.active_calls['CA1'] == replacement


@pytest.mark.parametrize('changed,binding', [
    ({'ws_token': 'rotated'}, {'ws_token': 'token123'}),
    ({'conference_name': 'replacement'}, {'conference_name': 'original'}),
])
@pytest.mark.asyncio
async def test_adoption_callback_retry_rejects_replaced_binding(rtdb_env, changed, binding):
    rtdb_env.active_calls['CA1']['conference_name'] = 'original'
    rtdb_env.call_commands['CA1'] = {'type': 'take_message'}
    replacement = deepcopy(rtdb_env.active_calls['CA1'])
    replacement.update(changed)
    rtdb_env.active_retry_with = replacement
    assert await legacy.adopt_legacy_message_intent('CA1', 'owner', **binding) is None
    assert rtdb_env.active_calls['CA1'] == replacement
    assert rtdb_env.call_commands['CA1'] == {'type': 'take_message'}


@pytest.mark.asyncio
async def test_cleanup_callback_retry_preserves_replacement(rtdb_env):
    snapshot = {'type': 'take_message'}
    replacement = {'type': 'take_message', 'operation_id': 'replacement'}
    rtdb_env.call_commands['CA1'] = snapshot
    rtdb_env.command_retry_with = replacement
    await legacy.delete_legacy_command_conditional('CA1', snapshot)
    assert rtdb_env.call_commands['CA1'] == replacement


@pytest.mark.asyncio
async def test_real_write_then_lost_response_never_republishes(rtdb_env, monkeypatch):
    original = legacy._run_legacy_command_transaction
    attempts = 0
    async def lose_response(sid, callback):
        nonlocal attempts
        attempts += 1
        await original(sid, callback)
        raise RuntimeError('response lost after commit')
    monkeypatch.setattr(legacy, '_run_legacy_command_transaction', lose_response)
    result, status = await actions.handle_owner_call_action(
        call_sid='CA1', contractor_id='owner', action='decline', operation_id='op1',
    )
    assert status == 202 and result['action_status'] == 'message_requested'
    assert rtdb_env.call_commands['CA1']['type'] == 'take_message'
    old = SimpleNamespace(
        _call_sid='CA1', _unavailable_said=False, _unavailable_task=None,
        _unavailable_now=AsyncMock(),
    )
    await f407.legacy_voice_commands(old)
    await asyncio.sleep(0)
    old._unavailable_now.assert_awaited_once()
    assert not rtdb_env.call_commands
    await actions.handle_owner_call_action(
        call_sid='CA1', contractor_id='owner', action='decline', operation_id='op1',
    )
    assert attempts == 1 and not rtdb_env.call_commands
    assert rtdb_env.active_calls['CA1']['owner_action_status'] == 'message_requested'


@pytest.mark.parametrize('replace_during_claim', [False, True])
@pytest.mark.asyncio
async def test_stale_direct_ring_cannot_redirect_replacement_conference(rtdb_env, monkeypatch, replace_during_claim):
    seed_message(rtdb_env)
    rtdb_env.active_calls['CA1'].update(state='pickup_ringing', conference_name='replacement')
    before = deepcopy(rtdb_env.active_calls['CA1'])
    if replace_during_claim:
        rtdb_env.active_calls['CA1']['conference_name'] = 'original'
        rtdb_env.active_retry_with = before
    client = MagicMock()
    client.conferences.list.return_value = []
    monkeypatch.setattr('twilio.rest.Client', lambda *a, **kw: client)
    async def no_sleep(_):
        pass
    monkeypatch.setattr(incoming.asyncio, 'sleep', no_sleep)
    await incoming._ring_contractor('CA1', '', '', 'original', contractor_id='owner')
    client.calls.assert_not_called()
    assert rtdb_env.active_calls['CA1'] == before


@pytest.mark.asyncio
async def test_negative_legacy_fallback_ignores_existing_acceptance(rtdb_env, monkeypatch):
    """Old background work remains a release constraint, even with new readers."""
    from twilio.twiml.voice_response import Connect
    rtdb_env.active_calls['CA1'].update(
        accepted=True, owner_action='accept', owner_operation_id='pickup-op',
        owner_action_status='accepted', claim_nonce='pickup-claim',
    )
    client = MagicMock()
    monkeypatch.setattr('twilio.rest.Client', lambda *a, **kw: client)
    monkeypatch.setattr(f407, 'Connect', Connect, raising=False)
    monkeypatch.setattr(f407, 'secrets', SimpleNamespace(token_urlsafe=lambda _: 'legacy-rotated-token'), raising=False)
    monkeypatch.setattr(f407.settings, 'cloud_run_url', 'https://fictional.invalid', raising=False)
    await f407.legacy_redirect_to_kevin('CA1')
    client.calls.assert_called_once_with('CA1')
    assert rtdb_env.active_calls['CA1']['ws_token'] == 'legacy-rotated-token'
    assert rtdb_env.active_calls['CA1']['owner_action'] == 'accept'
