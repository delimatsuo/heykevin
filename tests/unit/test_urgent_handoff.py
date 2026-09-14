"""Deadline, preferences, concurrent claim and independent-channel checks."""
import asyncio
from copy import deepcopy
import time
from unittest.mock import AsyncMock
import pytest
import os
# Fictional test configuration, after tests/conftest.py captures the pristine environment.
for key in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TELEGRAM_BOT_TOKEN', 'USER_PHONE'):
    os.environ.setdefault(key, 'test-placeholder')
from app.services import owner_call_actions as a, urgent_handoff as u


@pytest.fixture
def context(monkeypatch):
    record={'contractor_id':'owner','state':'screening','state_updated_at':time.time()}
    lock=asyncio.Lock()
    async def txn(sid,fn):
        async with lock:
            result=fn(deepcopy(record));record.clear();record.update(result or {});return deepcopy(record)
    async def read(sid):return deepcopy(record)
    monkeypatch.setattr(a,'_run_rtdb_transaction',txn)
    monkeypatch.setattr(a,'read_record',read)
    monkeypatch.setattr(u,'get_contractor',AsyncMock(return_value={'smart_interruption':True}))
    monkeypatch.setattr(u,'_get_device',AsyncMock(return_value={'push_token':'push','voip_token':'voip','urgent_handoff_v1':True}))
    monkeypatch.setattr(u,'send_urgent_push',AsyncMock(return_value=True))
    monkeypatch.setattr(u,'send_voip_push',AsyncMock(return_value=True))
    return record


async def dispatch():return await u.dispatch_urgent_escalation(call_sid='CA1',contractor_id='owner')


@pytest.mark.asyncio
async def test_concurrent_urgency_only_one_claim(context):
    await asyncio.gather(dispatch(),dispatch())
    assert u.send_voip_push.await_count==u.send_urgent_push.await_count==1
    payload=u.send_voip_push.call_args.kwargs
    assert payload['expires_at']==int(context['owner_wait_deadline'])
    assert payload['access_token']==payload['conference_name']==''


@pytest.mark.parametrize('profile',[None,{}, {'smart_interruption':False}])
@pytest.mark.asyncio
async def test_disabled_or_missing_profile_no_alert(context,profile):
    u.get_contractor.return_value=profile
    await dispatch()
    u.send_voip_push.assert_not_awaited();u.send_urgent_push.assert_not_awaited()
    assert context['owner_wait_deadline']>time.time()


@pytest.mark.asyncio
async def test_legacy_capability_and_failed_channel(context):
    u._get_device.return_value={'push_token':'push','voip_token':'voip'}
    await dispatch();u.send_voip_push.assert_not_awaited();u.send_urgent_push.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_voip_does_not_block_banner(context):
    u.send_voip_push.side_effect=RuntimeError('private provider error')
    result=await dispatch()
    assert result['push_sent'] and not result['voip_sent']


@pytest.mark.asyncio
async def test_slow_device_fetch_expired_no_alert(context):
    async def device(cid):
        context['owner_wait_deadline']=time.time()-1
        return {'push_token':'p','voip_token':'v','urgent_handoff_v1':True}
    u._get_device.side_effect=device
    await dispatch();u.send_voip_push.assert_not_awaited();u.send_urgent_push.assert_not_awaited()


@pytest.mark.asyncio
async def test_decision_during_profile_fetch_suppresses(context):
    async def profile(cid):
        context['owner_action']='accept';return {'smart_interruption':True}
    u.get_contractor.side_effect=profile
    await dispatch();u.send_voip_push.assert_not_awaited();u.send_urgent_push.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_hold_deadline_never_extended(context):
    old=time.time()+5;context['owner_wait_deadline']=old
    assert await a.ensure_owner_deadline('CA1','owner',urgent=True)==old
    await dispatch()
    assert context['owner_wait_deadline']==old


def test_safe_copy():
    assert 'SECRET' not in u.safe_urgent_push_body('SECRET','SECRET')


@pytest.mark.parametrize('change',[{'accepted':True},{'state':'connected'},{'state':'ended'},{'contractor_id':'other'}])
@pytest.mark.asyncio
async def test_nonanswerable_calls_suppress_urgency(context,change):
    context.update(change)
    await dispatch()
    u.send_voip_push.assert_not_awaited();u.send_urgent_push.assert_not_awaited()
