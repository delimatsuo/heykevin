"""Realistic RTDB commits, callback retries and provider-race behavior."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
import os
# Fictional test configuration, after tests/conftest.py captures the pristine environment.
for key in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TELEGRAM_BOT_TOKEN', 'USER_PHONE'):
    os.environ.setdefault(key, 'test-placeholder')
from app.services import owner_call_actions as a
_provider_lookup = a._conference_contains_call


def fresh(**changes):
    return dict(contractor_id='owner', state='screening', state_updated_at=time.time(), **{'ws_token': 'ws1', **changes})


class Store:
    def __init__(self, record):
        self.record = record
        self.lock = asyncio.Lock()
        self.retry_with = None
    async def transaction(self, sid, callback):
        async with self.lock:
            first = callback(deepcopy(self.record))
            if self.retry_with is not None:
                self.record, self.retry_with = self.retry_with, None
                first = callback(deepcopy(self.record))
            # Firebase null really deletes the location. No special abort interpretation.
            self.record = deepcopy(first)
            return deepcopy(first)
    async def read(self, sid):
        return deepcopy(self.record)


@pytest.fixture
def backend(monkeypatch):
    store = Store(fresh())
    monkeypatch.setattr(a, '_run_rtdb_transaction', store.transaction)
    monkeypatch.setattr(a, 'read_record', store.read)
    monkeypatch.setattr(a, 'register_conference', AsyncMock())
    async def binding(name):
        return {'contractor_id': 'owner', 'call_sid': 'CA1'}
    monkeypatch.setattr(a, 'get_conference_binding', binding)
    monkeypatch.setattr(a, '_generate_access_token', lambda contractor_id: 'token')
    monkeypatch.setattr(a, '_redirect', AsyncMock())
    monkeypatch.setattr(a, '_conference_contains_call', AsyncMock(return_value=False))
    monkeypatch.setattr('app.services.legacy_call_commands._run_legacy_command_transaction', AsyncMock(return_value={'type': 'take_message'}))
    monkeypatch.setattr('app.services.legacy_call_commands.read_legacy_command', AsyncMock(return_value=None))
    monkeypatch.setattr('app.services.legacy_call_commands.delete_legacy_command_conditional', AsyncMock(return_value=None))
    return store


async def act(action='accept', op='op1'):
    return await a.handle_owner_call_action(call_sid='CA1', contractor_id='owner', action=action, operation_id=op)


@pytest.mark.asyncio
async def test_concurrent_same_operation_redirects_once(backend):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def redirect(sid, conf):
        calls.append((sid, conf)); entered.set(); await release.wait()
    a._redirect = redirect
    first = asyncio.create_task(act()); await entered.wait()
    pending, code = await act()
    assert code == 202 and pending['action_status'] == 'accepting'
    for action, op in [('accept','other'), ('decline','op1'), ('decline','other')]:
        with pytest.raises(HTTPException) as err:
            await act(action, op)
        assert err.value.status_code == 409
    release.set(); accepted, code = await first
    duplicate, _ = await act()
    assert accepted['conference_name'] == duplicate['conference_name']
    assert code == 200 and len(calls) == 1


@pytest.mark.asyncio
async def test_retried_callback_loses_nonce_cannot_redirect(backend):
    backend.retry_with = fresh(owner_action='accept', owner_action_status='accepting', owner_operation_id='op1',
                               claim_nonce='another-request', conference_name='other-conference', accepted=True)
    result, code = await act()
    assert code == 202 and result['action_status'] == 'accepting'
    a._redirect.assert_not_awaited()


@pytest.mark.parametrize('change', [
    {'state':'ended'}, {'state':'unknown'}, {'contractor_id':''}, {'contractor_id':'other'},
    {'state_updated_at':None}, {'state_updated_at':0}, {'state_updated_at':float('nan')},
    {'state_updated_at':float('inf')}, {'state_updated_at':'future'},
    {'state_updated_at':time.time()-1000,'accepted':True},
])
@pytest.mark.asyncio
async def test_rejected_records_are_preserved(backend, change):
    if change.get('state_updated_at') == 'future':
        # Collection can precede this test by minutes in the full suite.
        change = {'state_updated_at': time.time() + 100}
    backend.record.update(change)
    before = deepcopy(backend.record)
    with pytest.raises(HTTPException): await act()
    assert backend.record is not None
    assert backend.record.keys() == before.keys()
    assert backend.record.get('owner_action') is None
    a._redirect.assert_not_awaited()


@pytest.mark.parametrize('sid',['../other','CA.foo','CA/other','CA#x','CA[x]',''])
@pytest.mark.asyncio
async def test_invalid_path_ids_rejected(backend,sid):
    with pytest.raises(HTTPException):
        await a.handle_owner_call_action(call_sid=sid, contractor_id='owner', action='accept')
    a._redirect.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_redirect_never_replays(backend):
    a._redirect.side_effect=RuntimeError('sensitive provider detail')
    result, code=await act()
    again, again_code=await act()
    assert code==again_code==202 and result['action_status']==again['action_status']=='uncertain'
    assert 'sensitive' not in str(backend.record)
    assert a._redirect.await_count==1


@pytest.mark.parametrize('replacement',[None, {'state':'ended'}, {'contractor_id':'other'}])
@pytest.mark.asyncio
async def test_finalization_never_resurrects_or_reassigns(backend,replacement):
    async def redirect(*args):
        if replacement is None: backend.record=None
        else: backend.record.update(replacement)
    a._redirect=redirect
    result,code=await act()
    assert code==202 and 'access_token' not in result
    if replacement is None: assert backend.record is None
    else:
        for k,v in replacement.items():assert backend.record[k]==v


@pytest.mark.asyncio
async def test_get_requires_explicit_matching_operation_for_credentials(backend):
    await act()
    for op in ['', 'other']:
        result=await a.get_owner_call_action_status(call_sid='CA1',contractor_id='owner',operation_id=op)
        assert result['operation_id']==op and 'access_token' not in result
        if op: assert result['action_status']=='action_conflict'
    result=await a.get_owner_call_action_status(call_sid='CA1',contractor_id='owner',operation_id='op1')
    assert result['access_token']=='token'
    backend.record['state_updated_at']=1
    result=await a.get_owner_call_action_status(call_sid='CA1',contractor_id='owner',operation_id='op1')
    assert not result['active'] and 'access_token' not in result


@pytest.mark.asyncio
async def test_get_unmatched_operation_conflicts_before_any_action(backend):
    result=await a.get_owner_call_action_status(call_sid='CA1',contractor_id='owner',operation_id='other')
    assert result['action_status']=='action_conflict' and result['operation_id']=='other'


@pytest.mark.asyncio
async def test_decline_intent_atomic_other_operations_conflict(backend):
    result,code=await act('decline')
    assert code==202 and result['action_status']=='message_requested'
    assert backend.record['message_intent']['operation_id']=='op1'
    with pytest.raises(HTTPException):await act('decline','other')
    with pytest.raises(HTTPException):await act('accept','op1')
    assert not await a.arbitrate_owner_timeout('CA1','owner')


@pytest.mark.asyncio
async def test_ack_failure_retries_without_instruction_replay(backend,monkeypatch):
    await act('decline')
    pipeline=SimpleNamespace(_call_sid='CA1',_contractor_config={'contractor_id':'owner'},_command_ws_token='ws1')
    deliver=AsyncMock(return_value=True)
    real=a.acknowledge_owner_action
    monkeypatch.setattr(a,'acknowledge_owner_action',AsyncMock(side_effect=RuntimeError('storage')))
    with pytest.raises(RuntimeError):await a.consume_message_intent(pipeline,deliver)
    assert backend.record['owner_action_status']=='message_requested'
    monkeypatch.setattr(a,'acknowledge_owner_action',real)
    assert await a.consume_message_intent(pipeline,deliver)
    assert deliver.await_count==1 and backend.record['owner_action_status']=='taking_message'


@pytest.mark.asyncio
async def test_failed_delivery_retains_intent_then_retries(backend):
    await act('decline')
    pipeline=SimpleNamespace(_call_sid='CA1',_contractor_config={'contractor_id':'owner'},_command_ws_token='ws1')
    deliver=AsyncMock(side_effect=[False,True])
    assert not await a.consume_message_intent(pipeline,deliver)
    assert backend.record['owner_action_status']=='message_requested'
    assert await a.consume_message_intent(pipeline,deliver)
    assert deliver.await_count==2


@pytest.mark.asyncio
async def test_timeout_persists_same_consumable_intent(backend):
    assert await a.arbitrate_owner_timeout('CA1','owner')
    intent=await a.pending_message_intent('CA1','owner')
    assert intent['action']=='timeout'
    with pytest.raises(HTTPException):await act()
    assert await a.acknowledge_owner_action('CA1',contractor_id='owner',operation_id=intent['operation_id'],action='timeout')


@pytest.mark.asyncio
async def test_ack_after_delete_does_not_resurrect(backend):
    await act('decline'); backend.record=None
    assert not await a.acknowledge_owner_action('CA1',contractor_id='owner',operation_id='op1',action='decline')
    assert backend.record is None


@pytest.mark.parametrize('engine', ['voice','gemini'])
@pytest.mark.asyncio
async def test_engine_failed_instruction_retries_and_ack_failure_never_repeats(backend,monkeypatch,engine):
    from unittest.mock import MagicMock
    from app.services.voice_pipeline import VoicePipeline
    from app.services.gemini_pipeline import GeminiPipeline
    cls = VoicePipeline if engine=='voice' else GeminiPipeline
    pipeline=cls.__new__(cls)
    pipeline._call_sid='CA1';pipeline._contractor_config={'contractor_id':'owner','owner_name':'Owner'}
    pipeline._command_ws_token='ws1';pipeline._prepare_message_delivery=AsyncMock(return_value=True)
    pipeline._connected=True;pipeline._unavailable_said=False;pipeline._ws=object()
    pipeline._response_lock=asyncio.Lock();pipeline._conversation=[]
    pipeline._finish_owner_availability_wait=MagicMock();pipeline.on_transcript=AsyncMock()
    if engine=='voice':
        delivery=pipeline._speak=AsyncMock(side_effect=[False,True])
    else:
        delivery=pipeline._send_client_instruction=AsyncMock(side_effect=[RuntimeError('socket'),True])
    await act('decline')
    await pipeline._check_commands()
    assert not pipeline._unavailable_said and backend.record['owner_action_status']=='message_requested'
    real=a.acknowledge_owner_action
    monkeypatch.setattr(a,'acknowledge_owner_action',AsyncMock(side_effect=RuntimeError('storage')))
    await pipeline._check_commands()
    assert pipeline._unavailable_said and backend.record['owner_action_status']=='message_requested'
    monkeypatch.setattr(a,'acknowledge_owner_action',real)
    await pipeline._check_commands()
    assert backend.record['owner_action_status']=='taking_message' and delivery.await_count==2


@pytest.mark.asyncio
async def test_gemini_missing_socket_retains_durable_timeout(backend):
    from app.services.gemini_pipeline import GeminiPipeline
    pipeline=GeminiPipeline.__new__(GeminiPipeline)
    pipeline._call_sid='CA1';pipeline._contractor_config={'contractor_id':'owner'}
    pipeline._command_ws_token='ws1';pipeline._prepare_message_delivery=AsyncMock(return_value=True)
    pipeline._connected=True;pipeline._unavailable_said=False;pipeline._ws=None
    assert await a.arbitrate_owner_timeout('CA1','owner')
    await pipeline._check_commands()
    assert backend.record['owner_action_status']=='message_requested'
    assert not pipeline._unavailable_said


@pytest.mark.parametrize('engine',['voice','gemini','relay'])
@pytest.mark.asyncio
async def test_all_engine_timeouts_retry_storage_failure_and_delivery(backend,monkeypatch,engine):
    from app.services.voice_pipeline import VoicePipeline
    from app.services.gemini_pipeline import GeminiPipeline
    from app.services.relay_pipeline import RelayPipeline
    cls={'voice':VoicePipeline,'gemini':GeminiPipeline,'relay':RelayPipeline}[engine]
    pipeline=cls.__new__(cls)
    pipeline._call_sid='CA1';pipeline._contractor_config={'contractor_id':'owner'}
    pipeline._command_ws_token='ws1';pipeline._prepare_message_delivery=AsyncMock(return_value=True)
    pipeline._connected=True;pipeline._active=True;pipeline._ending=False;pipeline._unavailable_said=False
    pipeline.OWNER_AVAILABILITY_TIMEOUT_SECONDS=0
    pipeline._deliver_message_instruction=AsyncMock(side_effect=[False,True])
    ensure=AsyncMock(side_effect=[RuntimeError('storage unavailable'),time.time()-1])
    monkeypatch.setattr(a,'ensure_owner_deadline',ensure)
    async def sleep(_):pass
    monkeypatch.setattr(a.asyncio,'sleep',sleep)
    timer=pipeline._owner_hold_timer if engine=='relay' else pipeline._unavailable_timer
    await timer()
    assert ensure.await_count==2
    assert pipeline._deliver_message_instruction.await_count==2
    assert backend.record['owner_action_status']=='taking_message'


@pytest.mark.asyncio
async def test_relay_interrupted_instruction_survives_until_completed(backend):
    from app.services.relay_pipeline import RelayPipeline
    seen=[]; first_started=asyncio.Event();never=asyncio.Event()
    async def stream(contents):
        seen.append(deepcopy(contents))
        if len(seen)==1:
            first_started.set();await never.wait()
        yield {'text':'The owner is unavailable; may I take a message?'}
    pipeline=RelayPipeline(contractor_config={'contractor_id':'owner','owner_name':'Owner'},call_sid='CA1',
        caller_phone='',send_to_twilio=AsyncMock(),on_transcript=AsyncMock(),stream_generate=stream)
    pipeline._command_ws_token='ws1'
    try:
        await act('decline')
        command_task=asyncio.create_task(pipeline._check_commands())
        await asyncio.wait_for(first_started.wait(),timeout=1.0)
        assert backend.record['owner_action_status']=='message_requested'
        await pipeline._supersede_in_flight()
        await asyncio.wait_for(command_task,timeout=1.0)
        assert backend.record['owner_action_status']=='message_requested'
        assert not getattr(pipeline,'_unavailable_said',False)
        await pipeline._check_commands()
        assert backend.record['owner_action_status']=='taking_message'
        assert getattr(pipeline,'_unavailable_said',False) is True
        assert len(seen)==2 and any('unavailable' in str(part) for part in seen[1])
    finally:
        await pipeline.stop()


@pytest.mark.asyncio
async def test_get_route_uses_202_for_pending_and_preserves_identity(backend):
    from app.api import voip
    await act('decline')
    request=SimpleNamespace(state=SimpleNamespace(contractor_id='owner',is_admin=False))
    response=await voip.get_call_action('CA1',request,contractor_id='owner',operation_id='op1')
    import json
    body=json.loads(response.body)
    assert response.status_code==202 and body['active'] is True
    assert body['status']=='pending' and body['operation_id']=='op1' and body['action']=='decline'
    assert 'access_token' not in body


@pytest.mark.asyncio
async def test_wrong_command_owner_or_operation_cannot_deliver(backend):
    await act('decline')
    backend.record['message_intent']['contractor_id']='someone-else'
    assert await a.pending_message_intent('CA1','owner') is None
    backend.record['message_intent']['contractor_id']='owner'
    backend.record['message_intent']['operation_id']='not-this-operation'
    assert await a.pending_message_intent('CA1','owner') is None


@pytest.mark.asyncio
async def test_direct_decline_ack_waits_for_successful_redirect(backend,monkeypatch):
    """Run the existing known-contact ring loop with providers fully stubbed."""
    from unittest.mock import MagicMock
    from app.webhooks import twilio_incoming as incoming
    from firebase_admin import db
    import twilio.rest
    backend.record.update(state='pickup_ringing', conference_name='direct')
    await act('decline')
    monkeypatch.setattr('app.db.cache._init_firebase',lambda:None)
    monkeypatch.setattr(db,'reference',lambda path:MagicMock())
    client=MagicMock();client.conferences.list.return_value=[]
    monkeypatch.setattr(twilio.rest,'Client',lambda *args:client)
    # Token/device lookup and initial ring push are isolated.
    monkeypatch.setattr('app.api.voip._generate_access_token',lambda **kwargs:'token')
    monkeypatch.setattr('app.services.push_notification.get_device_token',AsyncMock(return_value='device'))
    monkeypatch.setattr('app.services.push_notification.send_voip_push',AsyncMock(return_value=True))
    redirect=AsyncMock(side_effect=[False,True])
    monkeypatch.setattr(incoming,'_async_redirect_to_kevin',redirect)
    async def sleep(_):pass
    monkeypatch.setattr(asyncio,'sleep',sleep)
    await incoming._ring_contractor(call_sid='CA1',caller_phone='',caller_name='',conference_name='direct',contractor_id='owner')
    assert redirect.await_count==2
    assert backend.record['owner_action_status']=='message_requested'
    pipeline=SimpleNamespace(_call_sid='CA1', _contractor_config={'contractor_id':'owner'}, _command_ws_token='ws1')
    assert await a.consume_message_intent(pipeline, AsyncMock(return_value=True))
    assert backend.record['owner_action_status']=='taking_message'


@pytest.mark.asyncio
async def test_definite_preparation_failure_allows_deliberate_retry(backend,monkeypatch):
    backend.record['conference_name']='original-direct-conference'
    real=a.get_conference_binding
    monkeypatch.setattr(a,'get_conference_binding',AsyncMock(return_value=None))
    failure, code = await act()
    assert code == 503 and failure['action_status'] == 'preparation_failed' and failure['retryable'] is True
    assert not backend.record.get('owner_action') and not backend.record['accepted']
    assert backend.record['conference_name']=='original-direct-conference'
    a._redirect.assert_not_awaited()
    monkeypatch.setattr(a,'get_conference_binding',real)
    result,code=await act(op='deliberate-retry')
    assert code==200 and result['action_status']=='accepted'
    a._redirect.assert_awaited_once()


@pytest.mark.asyncio
async def test_lost_redirect_response_reconciles_without_second_redirect(backend):
    a._redirect.side_effect = RuntimeError('response lost after redirect')
    pending, code = await act()
    conference = backend.record['conference_name']
    assert code == 202
    a._conference_contains_call.return_value = True
    result = await a.get_owner_call_action_status(call_sid='CA1', contractor_id='owner', operation_id='op1')
    assert result['status'] == 'ok' and result['action_status'] == 'accepted' and result['active'] is True
    assert result['conference_name'] == conference and result['access_token'] == 'token'
    again = await a.get_owner_call_action_status(call_sid='CA1', contractor_id='owner', operation_id='op1')
    assert again['conference_name'] == conference
    a._conference_contains_call.assert_awaited_once_with(conference, 'CA1', 'owner')
    a._redirect.assert_awaited_once()


@pytest.mark.parametrize('failure', [False, RuntimeError('provider unavailable')])
@pytest.mark.asyncio
async def test_reconciliation_no_evidence_stays_pending(backend, failure):
    a._redirect.side_effect = RuntimeError('response lost')
    await act()
    if isinstance(failure, Exception): a._conference_contains_call.side_effect = failure
    result = await a.get_owner_call_action_status(call_sid='CA1', contractor_id='owner', operation_id='op1')
    assert result['status'] == 'pending' and 'access_token' not in result
    await act()
    a._redirect.assert_awaited_once()


@pytest.mark.asyncio
async def test_navigation_and_wrong_operation_never_reconcile(backend):
    a._redirect.side_effect = RuntimeError('response lost')
    await act()
    for op in ('', 'wrong'):
        result = await a.get_owner_call_action_status(call_sid='CA1', contractor_id='owner', operation_id=op)
        assert 'access_token' not in result
    a._conference_contains_call.assert_not_awaited()


@pytest.mark.parametrize('replacement', [None, {'state':'ended'}, {'contractor_id':'other'}])
@pytest.mark.asyncio
async def test_reconciliation_cannot_resurrect_ended_or_reassigned_call(backend, replacement):
    a._redirect.side_effect = RuntimeError('response lost')
    await act()
    async def lookup(*args):
        backend.record = None if replacement is None else {**backend.record, **replacement}
        return True
    a._conference_contains_call.side_effect = lookup
    if replacement and replacement.get('contractor_id') == 'other':
        with pytest.raises(HTTPException) as error:
            await a.get_owner_call_action_status(call_sid='CA1', contractor_id='owner', operation_id='op1')
        assert error.value.status_code == 403
    else:
        result = await a.get_owner_call_action_status(call_sid='CA1', contractor_id='owner', operation_id='op1')
        assert result['active'] is False and 'access_token' not in result
    if replacement is None: assert backend.record is None
    else: assert backend.record['owner_action_status'] == 'uncertain'
    a._redirect.assert_awaited_once()


@pytest.mark.parametrize('name,state,participant_sid,participant_status,expected', [
    ('exact','in-progress','CA1','connected',True),
    ('other','in-progress','CA1','connected',False),
    ('exact','completed','CA1','connected',False),
    ('exact','in-progress','CA2','connected',False),
    ('exact','in-progress','CA1','disconnected',False),
])
@pytest.mark.asyncio
async def test_provider_reconciliation_requires_exact_live_participant(backend, monkeypatch, name, state, participant_sid, participant_status, expected):
    from unittest.mock import MagicMock
    import twilio.rest
    client = MagicMock()
    client.conferences.list.return_value = [SimpleNamespace(friendly_name=name, status=state, sid='CF1')]
    client.conferences.return_value.participants.list.return_value = [SimpleNamespace(call_sid=participant_sid, status=participant_status)]
    monkeypatch.setattr(twilio.rest, 'Client', lambda *args, **kwargs: client)
    assert await _provider_lookup('exact', 'CA1', 'owner') is expected
    client.calls.assert_not_called()
    client.conferences.list.assert_called_once_with(friendly_name='exact', status='in-progress', limit=1)


@pytest.mark.asyncio
async def test_provider_lookup_does_not_run_without_exact_binding(backend, monkeypatch):
    from unittest.mock import MagicMock
    import twilio.rest
    provider = MagicMock()
    monkeypatch.setattr(twilio.rest, 'Client', provider)
    monkeypatch.setattr(a, 'get_conference_binding', AsyncMock(return_value={'contractor_id':'owner','call_sid':'different'}))
    assert await _provider_lookup('exact', 'CA1', 'owner') is False
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_caller_ending_during_preparation_never_sets_redirect_marker(backend, monkeypatch):
    async def binding(name):
        assert backend.record.get('accepted') is False
        backend.record['state'] = 'ended'
        return {'contractor_id':'owner', 'call_sid':'CA1'}
    monkeypatch.setattr(a, 'get_conference_binding', binding)
    with pytest.raises(HTTPException) as error:
        await act()
    assert error.value.status_code == 409
    assert backend.record['state'] == 'ended' and backend.record['accepted'] is False
    a._redirect.assert_not_awaited()


@pytest.mark.parametrize('outcome', ['error', 'lost_owner'])
@pytest.mark.asyncio
async def test_unconfirmed_preparation_release_never_promises_retry(backend, monkeypatch, outcome):
    original = backend.transaction
    async def transaction(sid, callback):
        if backend.record.get('owner_action'):
            if outcome == 'error': raise RuntimeError('unavailable storage')
            return {**backend.record, 'claim_nonce':'another-owner'}
        return await original(sid, callback)
    monkeypatch.setattr(a, '_run_rtdb_transaction', transaction)
    monkeypatch.setattr(a, 'get_conference_binding', AsyncMock(return_value=None))
    response, code = await act()
    assert code == 202 and response['status'] == 'pending'
    a._redirect.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_preparation_failure_requires_structured_exact_identity(backend, monkeypatch):
    import json
    from app.api import voip
    monkeypatch.setattr(a, 'get_conference_binding', AsyncMock(return_value=None))
    request = SimpleNamespace(state=SimpleNamespace(contractor_id='owner', is_admin=False))
    response = await voip.handle_call_action(request, voip.CallAction(call_sid='CA1', action='accept', operation_id='op1'), contractor_id='owner')
    body = json.loads(response.body)
    assert response.status_code == 503
    assert {key:body[key] for key in ('call_sid','contractor_id','operation_id','action','active','status','action_status','retryable')} == {
        'call_sid':'CA1','contractor_id':'owner','operation_id':'op1','action':'accept','active':True,
        'status':'error','action_status':'preparation_failed','retryable':True}
    assert 'conference_name' not in body and 'access_token' not in body
    assert not backend.record.get('owner_action') and backend.record['accepted'] is False
    a._redirect.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_ring_creates_live_authority_before_decline(backend):
    from app.webhooks import twilio_incoming as incoming
    backend.record = None
    assert await incoming._prepare_direct_call('CA1','owner','direct','phone','Caller')
    assert backend.record['state'] == 'pickup_ringing'
    result, code = await act('decline')
    assert code == 202 and result['action_status'] == 'message_requested'
    assert backend.record['message_intent']['contractor_id'] == 'owner'


@pytest.mark.parametrize('provider_failure', [False,True])
@pytest.mark.asyncio
async def test_direct_fallback_redirect_attempt_never_replays(backend, monkeypatch, provider_failure):
    from unittest.mock import MagicMock
    from app.webhooks import twilio_incoming as incoming
    import twilio.rest
    client = MagicMock()
    if provider_failure: client.calls.return_value.update.side_effect = RuntimeError('response lost')
    monkeypatch.setattr(twilio.rest, 'Client', lambda *args, **kwargs: client)
    assert await incoming._async_redirect_to_kevin('CA1','owner') is (not provider_failure)
    assert await incoming._async_redirect_to_kevin('CA1','owner') is (not provider_failure)
    client.calls.return_value.update.assert_called_once()
    if provider_failure:
        with pytest.raises(HTTPException) as error: await act()
        assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_direct_fallback_cannot_override_accept_winning_transaction(backend, monkeypatch):
    from unittest.mock import MagicMock
    from app.webhooks import twilio_incoming as incoming
    import twilio.rest
    client = MagicMock()
    monkeypatch.setattr(twilio.rest, 'Client', lambda *args, **kwargs: client)
    backend.retry_with = fresh(owner_action='accept', owner_operation_id='winning', owner_action_status='accepting',
                               claim_nonce='winner', conference_name='pickup', accepted=False)
    assert await incoming._async_redirect_to_kevin('CA1','owner') is False
    client.calls.assert_not_called()
    assert backend.record['owner_operation_id'] == 'winning'


@pytest.mark.asyncio
async def test_direct_fallback_rejects_reassigned_owner(backend, monkeypatch):
    from unittest.mock import MagicMock
    from app.webhooks import twilio_incoming as incoming
    import twilio.rest
    client = MagicMock()
    monkeypatch.setattr(twilio.rest, 'Client', lambda *args, **kwargs: client)
    backend.record['contractor_id'] = 'new-owner'
    assert await incoming._async_redirect_to_kevin('CA1','owner') is False
    client.calls.assert_not_called()


@pytest.mark.asyncio
async def test_elapsed_timeout_survives_pending_pickup_then_preparation_release(backend, monkeypatch):
    pipeline = SimpleNamespace(_call_sid='CA1', _contractor_config={'contractor_id':'owner'},
                               _connected=True, _command_ws_token='ws1', OWNER_AVAILABILITY_TIMEOUT_SECONDS=0)
    now = time.time()
    backend.record = a.reduce_active_call_action(backend.record, action='accept', contractor_id='owner',
        operation_id='op1', claim_nonce='pending', conference_name='pickup', now=now)[0]
    expected = deepcopy(backend.record)
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 1:
            await a._release_preparation('CA1', expected)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    deliver = AsyncMock(return_value=True)
    await a.run_owner_timeout(pipeline, deliver)
    assert backend.record['owner_action'] == 'timeout'
    assert backend.record['owner_action_status'] == 'taking_message'
    deliver.assert_awaited_once()
    assert any(delay == 0 for delay in sleeps)


@pytest.mark.parametrize('next_action', ['accept', 'timeout'])
@pytest.mark.asyncio
async def test_authenticated_stream_recovers_lost_fallback_response(backend, monkeypatch, next_action):
    from unittest.mock import MagicMock
    from app.webhooks import twilio_incoming as incoming
    import twilio.rest
    backend.record = None
    assert await incoming._prepare_direct_call('CA1','owner','direct','phone','Caller')
    client = MagicMock()
    client.calls.return_value.update.side_effect = RuntimeError('response lost after successful redirect')
    monkeypatch.setattr(twilio.rest, 'Client', lambda *args, **kwargs: client)
    assert await incoming._async_redirect_to_kevin('CA1','owner') is False
    assert backend.record['kevin_redirect_status'] == 'pending'
    assert await a.confirm_fallback_stream('CA1', contractor_id='owner', ws_token=backend.record['ws_token'],
                                           redirect_nonce=backend.record['kevin_redirect_nonce'])
    if next_action == 'accept':
        result, code = await act()
        assert code == 200 and result['action_status'] == 'accepted'
    else:
        assert await a.arbitrate_owner_timeout('CA1','owner')
    client.calls.return_value.update.assert_called_once()


@pytest.mark.parametrize('change', [None, {'call_sid':'CA2'}, {'contractor_id':'other'}, {'ws_token':'new-token'},
    {'kevin_redirect_nonce':'new-nonce'}, {'state':'ended'}, {'state_updated_at':0},
    {'state_updated_at':time.time()-1000}, {'accepted':True}, {'owner_action':'accept'},
    {'kevin_redirect_status':'unrecognized'}])
@pytest.mark.asyncio
async def test_stale_stream_evidence_cannot_finalize_fallback(backend, change):
    original = fresh(call_sid='CA1', ws_token='token', kevin_redirect_nonce='nonce', kevin_redirect_status='pending')
    backend.record = None if change is None else {**original, **change}
    before = deepcopy(backend.record)
    assert not await a.confirm_fallback_stream('CA1', contractor_id='owner', ws_token='token', redirect_nonce='nonce')
    assert backend.record == before


@pytest.mark.asyncio
async def test_stream_confirmation_callback_retry_rechecks_token_and_nonce(backend):
    backend.record = fresh(call_sid='CA1', ws_token='token', kevin_redirect_nonce='nonce', kevin_redirect_status='pending')
    backend.retry_with = {**backend.record, 'ws_token':'new-token', 'kevin_redirect_nonce':'new-nonce'}
    assert not await a.confirm_fallback_stream('CA1', contractor_id='owner', ws_token='token', redirect_nonce='nonce')
    assert backend.record['kevin_redirect_status'] == 'pending'


@pytest.mark.parametrize('engine', ['media','relay'])
@pytest.mark.asyncio
async def test_stream_entry_rejects_rotated_fallback_evidence(backend, monkeypatch, engine):
    import json
    from unittest.mock import MagicMock
    from firebase_admin import db
    from app.webhooks import media_stream, relay_stream
    module = media_stream if engine == 'media' else relay_stream
    captured = fresh(call_sid='CA1', ws_token='token', kevin_redirect_nonce='nonce', kevin_redirect_status='pending')
    backend.record = {**captured, 'ws_token':'rotated-token'}
    monkeypatch.setattr(module, '_init_firebase', lambda:None)
    reference = MagicMock(); reference.get.return_value = captured
    monkeypatch.setattr(db, 'reference', lambda path:reference)
    first = {'event':'start','streamSid':'MZ1','start':{'customParameters':{'ws_token':'token'}}} if engine == 'media' else {
        'type':'setup','customParameters':{'ws_token':'token'}}
    reads = 0
    async def receive():
        nonlocal reads
        reads += 1
        if reads == 1: return json.dumps(first)
        await asyncio.Future()
    websocket = SimpleNamespace(accept=AsyncMock(), close=AsyncMock(), receive_text=receive)
    entry = module.media_stream_ws if engine == 'media' else module.relay_stream_ws
    await entry(websocket, 'CA1')
    websocket.close.assert_awaited_once_with(code=1008)
    assert backend.record['kevin_redirect_status'] == 'pending'


@pytest.mark.asyncio
async def test_stream_token_guard_mutation_is_detected(backend):
    import inspect
    backend.record = fresh(call_sid='CA1', ws_token='new-token', kevin_redirect_nonce='nonce', kevin_redirect_status='pending')
    args = dict(contractor_id='owner', ws_token='old-token', redirect_nonce='nonce')
    assert not await a.confirm_fallback_stream('CA1', **args)
    source = inspect.getsource(a.confirm_fallback_stream)
    guard = "                and current.get('ws_token') == ws_token\n"
    assert guard in source
    namespace = dict(vars(a))
    exec(source.replace(guard, ''), namespace)
    assert await namespace['confirm_fallback_stream']('CA1', **args)
    assert backend.record['kevin_redirect_status'] == 'succeeded'
