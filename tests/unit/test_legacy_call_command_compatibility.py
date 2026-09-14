"""Cross-version compatibility tests for legacy 407 and current call arbitration."""
import asyncio
from copy import deepcopy
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi import HTTPException

# Capture test configuration
for key in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TELEGRAM_BOT_TOKEN', 'USER_PHONE'):
    os.environ.setdefault(key, 'test-placeholder')

from tests.fixtures import legacy_call_commands_407 as f407
from twilio.twiml.voice_response import Dial, VoiceResponse

# Stub globals in fixture module namespace safely
f407.logger = MagicMock()
f407.settings = SimpleNamespace(
    twilio_account_sid='AC123',
    twilio_auth_token='auth_token',
    user_name='Owner',
)
f407.VoiceResponse = VoiceResponse
f407.Dial = Dial
f407._generate_access_token = lambda **kwargs: 'token'
f407.redact_phone = lambda p: p
f407._call_label = lambda s: s[:8] if s else 'unknown'

import app.db.cache as cache_module
import app.services.legacy_call_commands as legacy_cmds
import app.services.owner_call_actions as owner_actions
from app.services.gemini_pipeline import GeminiPipeline
from app.services.relay_pipeline import RelayPipeline
from app.services.voice_pipeline import VoicePipeline
from app.webhooks import twilio_incoming as incoming


def fresh_record(**changes):
    base = dict(
        call_sid='CA1',
        contractor_id='owner',
        state='screening',
        state_updated_at=time.time(),
        accepted=False,
        conference_name='',
        ws_token='token123',
    )
    base.update(changes)
    return base


class ThreadSafeRTDBStore:
    def __init__(self, initial_active=None, initial_commands=None):
        self._lock = threading.Lock()
        self.active_calls = {'CA1': deepcopy(initial_active)} if initial_active is not None else {}
        self.call_commands = {'CA1': deepcopy(initial_commands)} if initial_commands is not None else {}
        self.other_paths = {}
        self.active_retry_with = None
        self.command_retry_with = None

    def reference(self, path: str):
        parts = [p for p in path.strip('/').split('/') if p]
        return ThreadSafeReference(self, parts)


class ThreadSafeReference:
    def __init__(self, db: ThreadSafeRTDBStore, path_parts: list[str]):
        self.db = db
        self.path_parts = path_parts

    def _get_target_dict(self):
        if not self.path_parts:
            return self.db.other_paths
        root = self.path_parts[0]
        if root == 'active_calls':
            return self.db.active_calls
        elif root == 'call_commands':
            return self.db.call_commands
        return self.db.other_paths.setdefault(root, {})

    def _get_key(self):
        return self.path_parts[1] if len(self.path_parts) > 1 else 'root'

    def get(self):
        with self.db._lock:
            d = self._get_target_dict()
            key = self._get_key()
            val = d.get(key)
            return deepcopy(val) if val is not None else None

    def set(self, val):
        with self.db._lock:
            d = self._get_target_dict()
            key = self._get_key()
            d[key] = deepcopy(val)

    def delete(self):
        with self.db._lock:
            d = self._get_target_dict()
            key = self._get_key()
            d.pop(key, None)

    def update(self, val):
        with self.db._lock:
            d = self._get_target_dict()
            key = self._get_key()
            if key not in d or d[key] is None:
                d[key] = {}
            d[key].update(deepcopy(val))

    def transaction(self, callback):
        with self.db._lock:
            d = self._get_target_dict()
            key = self._get_key()
            curr = deepcopy(d.get(key))
            res = callback(curr)

            root = self.path_parts[0] if self.path_parts else ''
            if root == 'active_calls' and self.db.active_retry_with is not None:
                d[key] = deepcopy(self.db.active_retry_with)
                self.db.active_retry_with = None
                res = callback(deepcopy(d.get(key)))
            elif root == 'call_commands' and self.db.command_retry_with is not None:
                d[key] = deepcopy(self.db.command_retry_with)
                self.db.command_retry_with = None
                res = callback(deepcopy(d.get(key)))

            if res is None:
                d.pop(key, None)
            else:
                d[key] = deepcopy(res)
            return deepcopy(res)


@pytest.fixture
def rtdb_env(monkeypatch):
    store = ThreadSafeRTDBStore(initial_active=fresh_record())
    monkeypatch.setattr('app.db.cache._init_firebase', lambda: None)
    monkeypatch.setattr('firebase_admin.db.reference', store.reference)
    monkeypatch.setattr(owner_actions, '_init_firebase', lambda: None)
    monkeypatch.setattr(legacy_cmds, '_init_firebase', lambda: None)
    monkeypatch.setattr(owner_actions, 'register_conference', AsyncMock())
    monkeypatch.setattr(owner_actions, '_generate_access_token', lambda contractor_id='': 'token')
    monkeypatch.setattr('app.api.voip._generate_access_token', lambda contractor_id='': 'token')
    monkeypatch.setattr(owner_actions, '_redirect', AsyncMock())
    monkeypatch.setattr(owner_actions, '_conference_contains_call', AsyncMock(return_value=False))
    monkeypatch.setattr('app.services.push_notification.get_device_token', AsyncMock(return_value='device_token'))
    monkeypatch.setattr('app.services.push_notification.send_voip_push', AsyncMock(return_value=True))
    return store


@pytest.mark.asyncio
async def test_new_decline_all_407_consumers_understand_and_old_delete_leaves_durable_pending(rtdb_env, monkeypatch):
    """Demonstrate: New decline -> each actual407 command consumer understands enriched command.

    Old deletion alone leaves durable status pending.
    """
    # 1. New decline
    res, code = await owner_actions.handle_owner_call_action(
        call_sid='CA1', contractor_id='owner', action='decline', operation_id='op_new'
    )
    assert code == 202
    assert rtdb_env.active_calls['CA1']['owner_action_status'] == 'message_requested'
    cmd = rtdb_env.call_commands['CA1']
    assert cmd['type'] == 'take_message'
    assert cmd['operation_id'] == 'op_new'

    # 2. Test actual407 legacy_voice_commands
    voice_mock = SimpleNamespace(
        _call_sid='CA1',
        _unavailable_said=False,
        _unavailable_task=None,
        _unavailable_now=AsyncMock(),
    )
    await f407.legacy_voice_commands(voice_mock)
    assert 'CA1' not in rtdb_env.call_commands
    # Old deletion alone leaves durable status pending
    assert rtdb_env.active_calls['CA1']['owner_action_status'] == 'message_requested'

    # Re-publish projection
    rtdb_env.active_calls['CA1'].pop('legacy_command_projection', None)
    await legacy_cmds.publish_message_intent('CA1', rtdb_env.active_calls['CA1'])

    # 3. Test actual407 legacy_gemini_commands
    gemini_mock = SimpleNamespace(
        _call_sid='CA1',
        _unavailable_said=False,
        _unavailable_task=None,
        _ws=object(),
        _contractor_config={'owner_name': 'Owner'},
        _finish_owner_availability_wait=MagicMock(),
        _send_client_instruction=AsyncMock(),
        _log_voice_timing=MagicMock(),
    )
    await f407.legacy_gemini_commands(gemini_mock)
    assert gemini_mock._unavailable_said is True
    gemini_mock._send_client_instruction.assert_awaited_once()
    assert 'CA1' not in rtdb_env.call_commands
    assert rtdb_env.active_calls['CA1']['owner_action_status'] == 'message_requested'

    # Re-publish projection
    rtdb_env.active_calls['CA1'].pop('legacy_command_projection', None)
    await legacy_cmds.publish_message_intent('CA1', rtdb_env.active_calls['CA1'])

    # 4. Test actual407 legacy_relay_commands
    relay_mock = SimpleNamespace(
        _call_sid='CA1',
        _unavailable_said=False,
        _contractor_config={'owner_name': 'Owner'},
        _supersede_in_flight=AsyncMock(),
        _start_generation=MagicMock(),
    )
    await f407.legacy_relay_commands(relay_mock)
    assert relay_mock._unavailable_said is True
    relay_mock._start_generation.assert_called_once()
    assert 'CA1' not in rtdb_env.call_commands
    assert rtdb_env.active_calls['CA1']['owner_action_status'] == 'message_requested'

    # 5. Test actual407 legacy_ring_contractor
    rtdb_env.active_calls['CA1'].pop('legacy_command_projection', None)
    rtdb_env.active_calls['CA1']['state'] = 'pickup_ringing'
    rtdb_env.active_calls['CA1']['conference_name'] = 'conf_direct'
    await legacy_cmds.publish_message_intent('CA1', rtdb_env.active_calls['CA1'])

    redirect_mock = AsyncMock()
    f407._async_redirect_to_kevin = redirect_mock

    client_mock = MagicMock()
    client_mock.conferences.list.return_value = []
    monkeypatch.setattr('twilio.rest.Client', lambda *args, **kwargs: client_mock)

    # Run legacy ring loop with instant sleep
    async def fast_sleep(_):
        pass
    monkeypatch.setattr(f407.asyncio, 'sleep', fast_sleep)

    await f407.legacy_ring_contractor('CA1', '+1234567890', 'Caller', 'conf_direct', 'owner')
    redirect_mock.assert_awaited_once_with('CA1')
    assert 'CA1' not in rtdb_env.call_commands
    assert rtdb_env.active_calls['CA1']['owner_action_status'] == 'message_requested'


@pytest.mark.parametrize('engine', ['voice', 'gemini', 'relay'])
@pytest.mark.asyncio
async def test_actual_407_decline_adopted_by_new_engines_with_honest_ack(rtdb_env, monkeypatch, engine):
    """Demonstrate: Actual407 _handle_decline -> new engine adopts with correct pinned identity.

    Exactly one delivery and honest acknowledgment after engine accepts it.
    """
    # 1. 407 decline writes bare command
    await f407.legacy_decline('CA1')
    assert rtdb_env.call_commands['CA1'] == {'type': 'take_message'}

    # 2. Setup active call record
    rtdb_env.active_calls['CA1'] = fresh_record(ws_token='pinned_ws_token')

    cls = {'voice': VoicePipeline, 'gemini': GeminiPipeline, 'relay': RelayPipeline}[engine]
    pipeline = cls.__new__(cls)
    pipeline._call_sid = 'CA1'
    pipeline._contractor_config = {'contractor_id': 'owner', 'owner_name': 'Owner'}
    pipeline._command_ws_token = 'pinned_ws_token'
    pipeline._connected = True
    pipeline._active = True
    pipeline._ending = False
    pipeline._unavailable_said = False
    pipeline._conversation = []
    pipeline._finish_owner_availability_wait = MagicMock()
    pipeline.on_transcript = AsyncMock()
    pipeline._summary_task = None

    if engine == 'voice':
        pipeline._response_lock = asyncio.Lock()
        pipeline._speak = AsyncMock(return_value=True)
    elif engine == 'gemini':
        pipeline._ws = object()
        pipeline._send_client_instruction = AsyncMock(return_value=None)
    else:
        pipeline._supersede_in_flight = AsyncMock()
        pipeline._start_generation = MagicMock()

    # Run check commands
    await pipeline._check_commands()

    # Verify adoption into durable active call record
    rec = rtdb_env.active_calls['CA1']
    assert rec['owner_action'] == 'decline'
    assert rec['owner_operation_id'] == 'legacy_decline'
    assert rec['owner_action_status'] == 'taking_message'

    # Verify legacy command conditionally deleted
    assert 'CA1' not in rtdb_env.call_commands

    # Second poll produces no further instruction
    if engine == 'voice':
        assert pipeline._speak.await_count == 1
    elif engine == 'gemini':
        assert pipeline._send_client_instruction.await_count == 1
    await pipeline._check_commands()
    if engine == 'voice':
        assert pipeline._speak.await_count == 1
    elif engine == 'gemini':
        assert pipeline._send_client_instruction.await_count == 1


@pytest.mark.asyncio
async def test_actual_407_decline_adopted_by_new_direct_ring(rtdb_env, monkeypatch):
    """Demonstrate: Actual407 _handle_decline adopted by new _ring_contractor using conference binding."""
    await f407.legacy_decline('CA1')
    rtdb_env.active_calls['CA1'] = fresh_record(state='pickup_ringing', conference_name='direct_conf')

    redirect_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(incoming, '_async_redirect_to_kevin', redirect_mock)

    async def fast_sleep(_):
        pass
    monkeypatch.setattr(asyncio, 'sleep', fast_sleep)

    client_mock = MagicMock()
    client_mock.conferences.list.return_value = []
    monkeypatch.setattr('twilio.rest.Client', lambda *args, **kwargs: client_mock)

    await incoming._ring_contractor('CA1', '+1234567890', 'Caller', 'direct_conf', 'owner')

    # Verify adopted into active record with decline
    rec = rtdb_env.active_calls['CA1']
    assert rec['owner_action'] == 'decline'
    assert rec['owner_operation_id'] == 'legacy_decline'
    assert rec['owner_action_status'] == 'message_requested'
    redirect_mock.assert_awaited_once_with('CA1', 'owner', expected_conference='direct_conf')
    # Direct ring does not delete command before receiving pipeline accepts it
    assert 'CA1' in rtdb_env.call_commands


@pytest.mark.asyncio
async def test_duplicate_intent_projection_reservations_and_delivery(rtdb_env):
    """Demonstrate: duplicate POST/polls/callback retries create one projection and one delivery."""
    # 1. First decline
    res1, code1 = await owner_actions.handle_owner_call_action(
        call_sid='CA1', contractor_id='owner', action='decline', operation_id='op1'
    )
    assert code1 == 202
    nonce1 = rtdb_env.active_calls['CA1']['legacy_command_projection']['nonce']

    # 2. Duplicate decline POST
    res2, code2 = await owner_actions.handle_owner_call_action(
        call_sid='CA1', contractor_id='owner', action='decline', operation_id='op1'
    )
    assert code2 == 202
    # Nonce did not change — no second projection attempt
    assert rtdb_env.active_calls['CA1']['legacy_command_projection']['nonce'] == nonce1

    # 3. Simulate multiple polls on VoicePipeline
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._call_sid = 'CA1'
    pipeline._contractor_config = {'contractor_id': 'owner', 'owner_name': 'Owner'}
    pipeline._command_ws_token = 'token123'
    pipeline._connected = True
    pipeline._unavailable_said = False
    pipeline._response_lock = asyncio.Lock()
    pipeline._conversation = []
    pipeline._finish_owner_availability_wait = MagicMock()
    pipeline.on_transcript = AsyncMock()
    pipeline._speak = AsyncMock(return_value=True)

    for _ in range(3):
        await pipeline._check_commands()

    assert pipeline._speak.await_count == 1
    assert rtdb_env.active_calls['CA1']['owner_action_status'] == 'taking_message'
    assert 'CA1' not in rtdb_env.call_commands


@pytest.mark.asyncio
async def test_projection_response_lost_old_consumer_removes_no_republish(rtdb_env, monkeypatch):
    """Demonstrate: Projection stored then response lost, old consumer removes it: no republish/false ack."""
    # Simulate failed write response leaving uncertain status
    rtdb_env.active_calls['CA1'] = fresh_record(
        owner_action='decline',
        owner_operation_id='op1',
        claim_nonce='nonce1',
        owner_action_status='message_requested',
        legacy_command_projection={'nonce': 'proj1', 'status': 'uncertain', 'updated_at': time.time()},
    )
    # Old consumer removes legacy command
    rtdb_env.call_commands.pop('CA1', None)

    # Attempt to publish again
    published = await legacy_cmds.publish_message_intent('CA1', rtdb_env.active_calls['CA1'])
    assert published is False
    assert 'CA1' not in rtdb_env.call_commands
    assert rtdb_env.active_calls['CA1']['owner_action_status'] == 'message_requested'


@pytest.mark.parametrize('scenario,setup_fn', [
    ('wrong_owner', lambda env: env.active_calls['CA1'].update(contractor_id='other_owner')),
    ('ended_state', lambda env: env.active_calls['CA1'].update(state='ended')),
    ('stale_time', lambda env: env.active_calls['CA1'].update(state_updated_at=time.time() - 1000)),
    ('already_accepted', lambda env: env.active_calls['CA1'].update(accepted=True)),
    ('conflicting_op', lambda env: env.active_calls['CA1'].update(owner_action='accept', owner_action_status='accepted')),
    ('malformed_enriched', lambda env: env.call_commands.update({'CA1': {'type': 'take_message', 'contractor_id': 'owner'}})),
    ('rotated_token', lambda env: env.active_calls['CA1'].update(ws_token='rotated_token')),
    ('replaced_conference', lambda env: env.active_calls['CA1'].update(state='pickup_ringing', conference_name='replaced_conf')),
])
@pytest.mark.asyncio
async def test_invalid_and_conflicting_contexts_reject_adoption_without_mutation(rtdb_env, scenario, setup_fn):
    """Demonstrate: Wrong owner, ended/stale/accepted, conflicting op, malformed, rotated token:

    No instruction, no unrelated record or command deletion.
    """
    # Start with a valid bare command
    rtdb_env.call_commands['CA1'] = {'type': 'take_message'}
    setup_fn(rtdb_env)
    before_call = deepcopy(rtdb_env.active_calls.get('CA1'))
    before_cmd = deepcopy(rtdb_env.call_commands.get('CA1'))

    if scenario == 'replaced_conference':
        # Direct ring adoption check with mismatching conference name
        adopted = await legacy_cmds.adopt_legacy_message_intent('CA1', 'owner', conference_name='original_conf')
        assert adopted is None
        assert rtdb_env.call_commands.get('CA1') == before_cmd
        assert rtdb_env.active_calls.get('CA1') == before_call
        return

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._call_sid = 'CA1'
    pipeline._contractor_config = {'contractor_id': 'owner', 'owner_name': 'Owner'}
    pipeline._command_ws_token = 'token123'
    pipeline._connected = True
    pipeline._conversation = []
    pipeline._finish_owner_availability_wait = MagicMock()
    pipeline.on_transcript = AsyncMock()
    pipeline._speak = AsyncMock()

    await pipeline._check_commands()

    pipeline._speak.assert_not_called()
    assert rtdb_env.call_commands.get('CA1') == before_cmd
    assert rtdb_env.active_calls.get('CA1') == before_call


@pytest.mark.asyncio
async def test_command_replacement_during_consumption_is_preserved(rtdb_env, monkeypatch):
    """Demonstrate: Command replacement during consumption is preserved."""
    rtdb_env.call_commands['CA1'] = {'type': 'take_message'}
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._call_sid = 'CA1'
    pipeline._contractor_config = {'contractor_id': 'owner', 'owner_name': 'Owner'}
    pipeline._command_ws_token = 'token123'
    pipeline._connected = True
    pipeline._unavailable_said = False
    pipeline._response_lock = asyncio.Lock()
    pipeline._conversation = []
    pipeline._finish_owner_availability_wait = MagicMock()
    pipeline.on_transcript = AsyncMock()

    async def deliver_and_replace(msg):
        # Replace command in RTDB before delivery finishes and conditional deletion runs
        rtdb_env.call_commands['CA1'] = {'type': 'take_message', 'replacement_tag': 'new_cmd'}
        return True

    pipeline._speak = deliver_and_replace
    await pipeline._check_commands()

    # The replacement command must be preserved because snapshot != current!
    assert rtdb_env.call_commands['CA1'] == {'type': 'take_message', 'replacement_tag': 'new_cmd'}
    assert rtdb_env.active_calls['CA1']['owner_action_status'] == 'taking_message'


@pytest.mark.asyncio
async def test_ack_failure_and_cleanup_failure_resilience(rtdb_env, monkeypatch):
    """Demonstrate: Ack failure retries without repeating delivery; cleanup failure likewise."""
    await f407.legacy_decline('CA1')
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._call_sid = 'CA1'
    pipeline._contractor_config = {'contractor_id': 'owner', 'owner_name': 'Owner'}
    pipeline._command_ws_token = 'token123'
    pipeline._connected = True
    pipeline._unavailable_said = False
    pipeline._response_lock = asyncio.Lock()
    pipeline._conversation = []
    pipeline._finish_owner_availability_wait = MagicMock()
    pipeline.on_transcript = AsyncMock()
    pipeline._speak = AsyncMock(return_value=True)

    # 1. Ack failure during consume_message_intent
    real_ack = owner_actions.acknowledge_owner_action
    monkeypatch.setattr(owner_actions, 'acknowledge_owner_action', AsyncMock(side_effect=RuntimeError('transient rtdb error')))

    with pytest.raises(RuntimeError, match='transient rtdb error'):
        await owner_actions.consume_message_intent(pipeline, pipeline._deliver_message_instruction)

    assert pipeline._speak.await_count == 1

    # 2. Second poll: ack succeeds without repeating delivery
    monkeypatch.setattr(owner_actions, 'acknowledge_owner_action', real_ack)
    # Also simulate cleanup error
    monkeypatch.setattr(legacy_cmds, 'delete_legacy_command_conditional', AsyncMock(side_effect=RuntimeError('cleanup failure')))

    consumed = await owner_actions.consume_message_intent(pipeline, pipeline._deliver_message_instruction)
    assert consumed is True
    assert pipeline._speak.await_count == 1
    assert rtdb_env.active_calls['CA1']['owner_action_status'] == 'taking_message'


@pytest.mark.asyncio
async def test_legacy_accepted_injected_rejects_delivery_and_preparation_release(rtdb_env):
    """Demonstrate: Legacy accepted=True injected before delivery/ack and preparation release rejects changes."""
    rtdb_env.call_commands['CA1'] = {'type': 'take_message'}
    rtdb_env.active_calls['CA1']['accepted'] = True

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._call_sid = 'CA1'
    pipeline._contractor_config = {'contractor_id': 'owner', 'owner_name': 'Owner'}
    pipeline._command_ws_token = 'token123'
    pipeline._connected = True
    pipeline._conversation = []
    pipeline._finish_owner_availability_wait = MagicMock()
    pipeline.on_transcript = AsyncMock()
    pipeline._speak = AsyncMock()

    await pipeline._check_commands()
    pipeline._speak.assert_not_called()

    # Preparation release must fail if accepted=True
    expected = dict(
        contractor_id='owner',
        owner_action='accept',
        owner_operation_id='op1',
        claim_nonce='nonce1',
        owner_action_status='accepting',
        conference_name='pickup',
    )
    rtdb_env.active_calls['CA1'].update(expected)
    with pytest.raises(RuntimeError, match='preparation_release_unconfirmed'):
        await owner_actions._release_preparation('CA1', expected)


@pytest.mark.asyncio
async def test_negative_fixture_actual_407_accept_ignores_durable_decline(rtdb_env, monkeypatch):
    """Negative fixture: Unresolved old-writer transition/recovery constraint.

    Actual 407 legacy_accept does not inspect durable ownership claims and blindly
    overwrites accepted=True across a durable decline. This test proves that
    unmodified legacy accept ignores new durable ownership; no adapter can make
    old production writers atomic without server-side arbitration.
    """
    # 1. New durable decline is recorded
    await owner_actions.handle_owner_call_action(
        call_sid='CA1', contractor_id='owner', action='decline', operation_id='op_decline'
    )
    assert rtdb_env.active_calls['CA1']['owner_action'] == 'decline'
    assert rtdb_env.active_calls['CA1']['owner_action_status'] == 'message_requested'
    assert rtdb_env.active_calls['CA1']['accepted'] is False

    # Mock Twilio client for legacy_accept
    client_mock = MagicMock()
    monkeypatch.setattr('twilio.rest.Client', lambda *args, **kwargs: client_mock)
    monkeypatch.setattr('app.services.conference_registry.register_conference', AsyncMock())

    # 2. Run actual407 legacy_accept
    result = await f407.legacy_accept('CA1', contractor_id='owner')
    assert result['status'] == 'ok'

    # 3. Prove old writer blindly overwrote accepted=True without checking durable claim
    assert rtdb_env.active_calls['CA1']['accepted'] is True
    assert rtdb_env.active_calls['CA1']['owner_action'] == 'decline'
