"""Once-only, bounded urgent escalation without allocating a conference."""
from __future__ import annotations
import asyncio
import secrets
import time
from app.db.contractors import get_contractor
from app.services import owner_call_actions as actions
from app.services.push_notification import send_urgent_push, send_voip_push


def safe_urgent_push_body(caller_name='', caller_phone=''):
    return 'Urgent call needs review. Open Kevin for details.'


async def _get_device(contractor_id):
    from app.db.firestore_client import get_firestore_client
    db = get_firestore_client()
    doc = await asyncio.to_thread(lambda: db.document(f'contractors/{contractor_id}/devices/primary').get())
    return (doc.to_dict() or {}) if doc.exists else {}


async def dispatch_urgent_escalation(*, call_sid, contractor_id, transcript_snippet=''):
    try:
        deadline = await actions.ensure_owner_deadline(call_sid, contractor_id, urgent=True)
        if not deadline or deadline <= time.time():
            return {'status': 'suppressed'}
        contractor = await get_contractor(contractor_id)
        if not contractor or contractor.get('smart_interruption', True) is not True:
            return {'status': 'suppressed'}
        nonce, now = secrets.token_hex(16), time.time()
        def txn(current):
            if (not actions.live_record(current, contractor_id, now) or current.get('owner_action')
                    or current.get('accepted') or current.get('state') not in actions.DECISION_STATES
                    or current.get('urgency_escalated') or current.get('owner_wait_deadline', 0) <= now):
                return current
            return {**current, 'urgency_escalated': True, 'urgency_claim_nonce': nonce, 'urgent': True}
        claimed = await actions._run_rtdb_transaction(call_sid, txn)
        if not claimed or claimed.get('urgency_claim_nonce') != nonce:
            return {'status': 'suppressed'}
        device = await _get_device(contractor_id)
        async def eligible():
            current = await actions.read_record(call_sid)
            profile = await get_contractor(contractor_id)
            return (actions.live_record(current, contractor_id) and profile
                    and profile.get('smart_interruption', True) is True
                    and current.get('urgency_claim_nonce') == nonce and not current.get('owner_action')
                    and not current.get('accepted') and current.get('state') in actions.DECISION_STATES
                    and current.get('owner_wait_deadline', 0) > time.time())
        async def voip():
            try:
                if device.get('voip_token') and device.get('urgent_handoff_v1') is True and await eligible():
                    return await send_voip_push(device_token=device['voip_token'], caller_phone=claimed.get('caller_phone', ''),
                        caller_name=claimed.get('caller_name', ''), reason='urgent_call', call_sid=call_sid,
                        contractor_id=contractor_id, expires_at=int(deadline), conference_name='', access_token='')
            except Exception:
                pass
            return False
        async def banner():
            try:
                if device.get('push_token') and await eligible():
                    return await send_urgent_push(device_token=device['push_token'], title='URGENT CALL',
                        body=safe_urgent_push_body(), call_sid=call_sid, caller_phone=claimed.get('caller_phone', ''),
                        caller_name=claimed.get('caller_name', ''), contractor_id=contractor_id,
                        collapse_id=f'call_{call_sid}', category='SCREENING_CALL')
            except Exception:
                pass
            return False
        # A stalled channel does not serialize/delay the other. Never outlive the owner wait.
        try:
            async with asyncio.timeout(max(0, deadline - time.time())):
                sent = await asyncio.gather(voip(), banner())
        except TimeoutError:
            return {'status': 'expired'}
        return {'status': 'escalated', 'voip_sent': sent[0], 'push_sent': sent[1]}
    except asyncio.CancelledError:
        raise
    except Exception:
        return {'status': 'suppressed'}


def start_urgency_task(pipeline, callback, text):
    """Pipeline-owned task, cancelled on stop; failures cannot escape supervision."""
    async def run():
        try:
            await callback(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
    task = asyncio.create_task(run())
    pipeline._urgency_task = task
    return task
