"""Synthetic incoming-to-stream registration; no providers or customer data."""

import asyncio
from copy import deepcopy
import os
import threading
from unittest.mock import AsyncMock
import xml.etree.ElementTree as ET

import pytest

for name, value in {
    "TWILIO_ACCOUNT_SID": "test-account", "TWILIO_AUTH_TOKEN": "test-token",
    "TWILIO_PHONE_NUMBER": "+15550000000", "TELEGRAM_BOT_TOKEN": "test-token",
    "USER_PHONE": "+15550000001",
}.items():
    os.environ.setdefault(name, value)

from app.db import calls
from app.services import call_registration as registration
from app.services.state_machine import ActiveCall, CallState
from app.webhooks import twilio_incoming
from tests.fixtures.call_registration import registration_stores


class Form:
    def __init__(self, **data):
        self.data = {"CallSid": "CA_registration", "From": "+15550000002", "To": "+15550000000", **data}

    async def form(self):
        return self.data


def call(**kwargs):
    return ActiveCall(**{
        "call_sid": "CA_registration", "contractor_id": "tenant-a",
        "caller_phone": "+15550000002", "caller_name": "Synthetic Caller",
        "state": CallState.SCREENING, "conference_name": "opaque_conference",
        "ws_token": "original-token", **kwargs,
    })


def incoming(monkeypatch, engine="media", *, contractor=True):
    config = {
        "contractor_id": "tenant-a", "owner_phone": "+15550000001",
        "owner_name": "Synthetic Owner", "subscription_status": "active",
        "voice_engine": engine, "ring_through_contacts": False,
    }
    for target, value in {
        "app.db.contractors.get_contractor_by_twilio_number": config if contractor else None,
        "app.db.contacts.get_contact": {"name": "Synthetic Caller"},
        "app.db.calls.get_call_history": [],
        "app.services.quiet_hours.load_quiet_hours_preferences": {},
        "app.services.receptionist_context.load_customer_memory_context": {},
        "app.services.push_notification.get_device_token": "synthetic-push-token",
    }.items():
        monkeypatch.setattr(target, AsyncMock(return_value=value))
    monkeypatch.setattr("app.services.circuit_breaker.is_circuit_open", lambda: False)
    monkeypatch.setattr(twilio_incoming, "_record_inbound_call_evidence", AsyncMock(return_value=True))
    monkeypatch.setattr(twilio_incoming, "_record_inbound_call_measurement", AsyncMock())
    push = AsyncMock()
    monkeypatch.setattr("app.services.push_notification.send_regular_push", push)
    return push


def stream_token(response):
    return ET.fromstring(response.body).find(".//Parameter[@name='ws_token']").attrib["value"]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["media", "relay"])
@pytest.mark.parametrize("history_outcome", ["slow", "false", "raised", "cancelled"])
async def test_incoming_authority_and_push_do_not_wait_for_history(monkeypatch, engine, history_outcome):
    from tests.unit.test_call_lifecycle_regressions import _AsyncioProxy, _SetupSocket, _SetupProbeComplete, _settle
    from app.webhooks import media_stream, relay_stream
    stores = registration_stores(monkeypatch)
    push = incoming(monkeypatch, engine)
    proxy = _AsyncioProxy()
    monkeypatch.setattr(twilio_incoming, "asyncio", proxy)
    entered, release = asyncio.Event(), asyncio.Event()

    async def history(*_args):
        entered.set()
        await release.wait()
        if history_outcome == "raised":
            raise RuntimeError("synthetic history failure")
        if history_outcome == "cancelled":
            raise asyncio.CancelledError()
        return history_outcome != "false"

    monkeypatch.setattr(calls, "save_call", history)
    module = media_stream if engine == "media" else relay_stream
    monkeypatch.setattr(module, "_init_firebase", lambda: None)
    auth = []

    async def resolved(sid, record):
        auth.append((sid, record))
        raise _SetupProbeComplete()

    monkeypatch.setattr(module, "_resolve_active_call", resolved)
    try:
        async with asyncio.timeout(3):
            response = await twilio_incoming.handle_incoming_call(Form())
            token = stream_token(response)
            await entered.wait()
            assert not release.is_set()
            push.assert_awaited_once()
            assert stores.client.records["CA_registration"]["ws_token"] == token
            entry = module.media_stream_ws if engine == "media" else module.relay_stream_ws
            good = _SetupSocket(engine, token)
            await entry(good, "CA_registration")
            bad = _SetupSocket(engine, "replacement-token")
            await entry(bad, "CA_registration")
            assert len(auth) == 1 and auth[0][1]["contractor_id"] == "tenant-a"
            assert good.close_codes == [1011] and bad.close_codes == [1008]
            release.set()
            await asyncio.gather(*proxy.tasks, return_exceptions=True)
    finally:
        release.set()
        await _settle(proxy.tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_ack", [False, True])
async def test_repeated_incoming_reuses_token_and_sends_side_effects_once(monkeypatch, lost_ack):
    from tests.unit.test_call_lifecycle_regressions import _AsyncioProxy, _settle
    stores = registration_stores(monkeypatch)
    push = incoming(monkeypatch)
    proxy = _AsyncioProxy()
    monkeypatch.setattr(twilio_incoming, "asyncio", proxy)
    history = AsyncMock(return_value=True)
    monkeypatch.setattr(calls, "save_call", history)
    if lost_ack:
        original = stores.client.headers

        def lose_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic committed write with lost acknowledgement")

        monkeypatch.setattr(stores.client, "headers", lose_ack)
    try:
        first = await twilio_incoming.handle_incoming_call(Form())
        snapshot = deepcopy(stores.client.records)
        second = await twilio_incoming.handle_incoming_call(Form())
        await asyncio.gather(*proxy.tasks)
        assert stream_token(first) == stream_token(second)
        assert stores.client.records == snapshot
        assert len(stores.documents) == 1
        push.assert_awaited_once()
        history.assert_awaited_once()
        stores.conference.assert_awaited_once()
    finally:
        await _settle(proxy.tasks)


@pytest.mark.asyncio
async def test_concurrent_retry_waits_for_winner_without_rotating_token(monkeypatch):
    stores = registration_stores(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def conference(*args):
        entered.set()
        await release.wait()

    stores.conference.side_effect = conference
    first = asyncio.create_task(registration.register_screening_call(call()))
    second = None
    try:
        async with asyncio.timeout(3):
            await entered.wait()
            second = asyncio.create_task(registration.register_screening_call(call(ws_token="new-token")))
            assert await asyncio.to_thread(stores.receipts_read.wait, 1)
            assert not second.done()
            release.set()
            one, two = await asyncio.gather(first, second)
        assert one.created and not two.created
        assert one.record == two.record and two.record["ws_token"] == "original-token"
        stores.conference.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(*(t for t in (first, second) if t is not None), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "ended", "accepted", "owner", "tenant", "rotated-token", "call-id", "stale"])
async def test_retry_never_recreates_or_overwrites_changed_authority(monkeypatch, change):
    stores = registration_stores(monkeypatch)
    assert await registration.register_screening_call(call())
    record = stores.client.records["CA_registration"]
    if change == "deleted":
        stores.client.records.clear()
        monkeypatch.setattr(registration, "REGISTRATION_TIMEOUT_SECONDS", 0.02)
    else:
        key, value = {
            "ended": ("state", "ended"), "accepted": ("accepted", True),
            "owner": ("owner_action", "decline"), "tenant": ("contractor_id", "tenant-b"),
            "rotated-token": ("ws_token", "new-owner-token"), "call-id": ("call_sid", "CA_other"),
            "stale": ("state_updated_at", 1),
        }[change]
        record[key] = value
    before = deepcopy(stores.client.records)
    if change == "deleted":
        assert await registration.register_screening_call(call(ws_token="retry-token")) is None
        assert stores.client.records["CA_registration"]["registration_disposition"] == "forward"
    else:
        with pytest.raises(registration.RegistrationUnavailable):
            await registration.register_screening_call(call(ws_token="retry-token"))
        assert stores.client.records == before
    stores.conference.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["ended", "new-token"])
async def test_sdk_retry_does_not_overwrite_newer_state_or_cancelled_work(monkeypatch, change):
    stores = registration_stores(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = stores.client.before_write

    def conflict(records):
        entered.set()
        assert release.wait(2)
        if change == "ended":
            records["CA_registration"] = {"state": "ended"}
        else:
            records["CA_registration"] = {"ws_token": "replacement"}

    stores.client.before_write = conflict
    task = asyncio.create_task(registration.register_screening_call(call()))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        release.set()
        with pytest.raises(registration.RegistrationUnavailable):
            await task
        await asyncio.to_thread(lambda: None)
        assert stores.client.records["CA_registration"].get("ws_token") != "original-token"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        stores.client.before_write = original


@pytest.mark.asyncio
async def test_status_tombstone_precedes_blocked_history_and_receipt_outlives_it(monkeypatch):
    stores = registration_stores(monkeypatch)
    assert await registration.register_screening_call(call())
    entered, release = asyncio.Event(), asyncio.Event()

    async def save(*_args):
        entered.set()
        await release.wait()
        return False

    monkeypatch.setattr(calls, "save_call", save)
    status = asyncio.create_task(twilio_incoming.handle_status(Form(CallStatus="completed")))
    try:
        await entered.wait()
        marker = stores.client.records["CA_registration"]
        assert set(marker) == {"call_sid", "state", "state_updated_at"}
        assert marker["state"] == "ended"
        with pytest.raises(registration.RegistrationUnavailable):
            await registration.register_screening_call(call())
        # Existing orphan cleanup may remove the tombstone after two hours.
        from app.services.background_maintenance import _delete_if_still_stale, _call_is_over
        assert _call_is_over(marker, marker["state_updated_at"])
        assert _delete_if_still_stale(registration.cache._get_ref("CA_registration"), marker["state_updated_at"] + 7201)
        monkeypatch.setattr(registration, "REGISTRATION_TIMEOUT_SECONDS", 0.02)
        assert await registration.register_screening_call(call()) is None
        assert stores.client.records["CA_registration"]["registration_disposition"] == "forward"
        release.set()
        assert (await status).status_code == 500
    finally:
        release.set()
        await status


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", ["", "../active_calls", "a/b", ".", "a" * 81])
async def test_invalid_ids_never_touch_authority(monkeypatch, sid):
    from fastapi import HTTPException
    get_ref = AsyncMock(side_effect=AssertionError("must not construct reference"))
    monkeypatch.setattr(registration.cache, "_get_ref", get_ref)
    with pytest.raises(HTTPException):
        await twilio_incoming.handle_status(Form(CallSid=sid, CallStatus="completed"))
    with pytest.raises(HTTPException):
        await registration.register_screening_call(call(call_sid=sid))
    get_ref.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["receipt", "authority", "unbound"])
async def test_registration_unavailable_returns_owner_forward_without_streaming(monkeypatch, cause):
    registration_stores(monkeypatch)
    incoming(monkeypatch, contractor=cause != "unbound")
    if cause == "receipt":
        monkeypatch.setattr(registration, "get_firestore_client", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    elif cause == "authority":
        monkeypatch.setattr(registration.cache, "_get_ref", lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    post = AsyncMock()
    monkeypatch.setattr(twilio_incoming, "_post_routing_tasks", post)
    response = await twilio_incoming.handle_incoming_call(Form())
    xml = ET.fromstring(response.body)
    assert xml.find(".//Number").text == "+15550000001"
    assert xml.find(".//Stream") is None and xml.find(".//ConversationRelay") is None
    post.assert_not_called()


@pytest.mark.parametrize("transcript", [None, "plain", {"version": 1}])
def test_receipt_is_redacted_from_call_api_documents(monkeypatch, transcript):
    raw = {"call_sid": "CA_test", registration.RECEIPT_FIELD: {"nonce": "private", "token_digest": "digest"}}
    if transcript is not None:
        raw["transcript"] = transcript
    before = deepcopy(raw)
    monkeypatch.setattr(calls, "_decrypt_transcript", lambda _: "decoded")
    result = calls._maybe_decrypt_call_doc(raw)
    assert registration.RECEIPT_FIELD not in result and raw == before


@pytest.mark.asyncio
async def test_unsigned_incoming_is_rejected_before_registration(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    app = FastAPI()
    app.include_router(twilio_incoming.router)
    register = AsyncMock(side_effect=AssertionError("unsigned request reached registration"))
    monkeypatch.setattr(registration, "register_screening_call", register)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/webhooks/twilio/incoming", data=Form().data)
    assert response.status_code == 403
    register.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_notification_does_not_delay_history(monkeypatch):
    from app.services.routing import Route
    entered, release, saved = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def notify(**kwargs):
        entered.set()
        await release.wait()

    async def persist(*args):
        saved.set()
        return True

    monkeypatch.setattr("app.services.push_notification.get_device_token", AsyncMock(return_value="synthetic"))
    monkeypatch.setattr("app.services.push_notification.send_regular_push", notify)
    monkeypatch.setattr(calls, "save_call", persist)
    task = asyncio.create_task(twilio_incoming._post_routing_tasks(
        "CA_registration", "+15550000002", "Synthetic", 0, {}, Route.AI_SCREENING,
        {}, "opaque_conference", contractor_id="tenant-a", ws_token="original-token",
    ))
    try:
        async with asyncio.timeout(1):
            await entered.wait()
            await saved.wait()
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_timestamp", [None, 123.0])
async def test_status_first_history_has_retention_age_without_refreshing_existing_age(monkeypatch, existing_timestamp):
    from types import SimpleNamespace
    stores = registration_stores(monkeypatch)
    if existing_timestamp is not None:
        stores.documents["CA_registration"] = {"timestamp": existing_timestamp}

    class Transaction:
        def set(self, ref, data, merge):
            ref.set(data, merge=merge)

    def transactional(function):
        def execute(transaction):
            with stores.lock:
                return function(transaction)
        return execute

    monkeypatch.setattr(calls.firestore, "transactional", transactional)
    monkeypatch.setattr(calls, "get_firestore_client", lambda: SimpleNamespace(
        collection=stores.database.collection, transaction=Transaction,
    ))
    assert await calls.save_call("CA_registration", {"ended_at": 456.0, "call_status": "completed"})
    assert stores.documents["CA_registration"]["timestamp"] == (existing_timestamp or 456.0)
    with pytest.raises(registration.RegistrationRejected):
        await registration.register_screening_call(call())
    assert stores.client.records == {}


@pytest.mark.asyncio
async def test_missing_status_id_does_not_construct_root_reference(monkeypatch):
    from fastapi import HTTPException
    request = Form(CallStatus="completed")
    del request.data["CallSid"]
    reference = AsyncMock(side_effect=AssertionError("root reference forbidden"))
    monkeypatch.setattr(registration.cache, "_get_ref", reference)
    with pytest.raises(HTTPException):
        await twilio_incoming.handle_status(request)
    reference.assert_not_called()


@pytest.mark.asyncio
async def test_sent_write_after_timeout_never_conflicts_with_confirmed_forward(monkeypatch):
    stores = registration_stores(monkeypatch)
    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    original = stores.client.headers
    count = 0

    def delayed(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            assert release.wait(2)
            try:
                return original(*args, **kwargs)
            finally:
                settled.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(stores.client, "headers", delayed)
    monkeypatch.setattr(registration, "REGISTRATION_TIMEOUT_SECONDS", 0.2)
    task = asyncio.create_task(registration.register_screening_call(call()))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert await task is None
        release.set()
        assert await asyncio.to_thread(settled.wait, 1)
        result = await registration.register_screening_call(call(ws_token="retry"))
        assert result is None
        assert stores.client.records["CA_registration"]["registration_disposition"] == "forward"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_lost_write_ack_adopts_stream_without_revoking_duplicate(monkeypatch, cancel):
    stores = registration_stores(monkeypatch)
    original = stores.client.headers
    published, release = threading.Event(), threading.Event()

    def lose_ack(*args, **kwargs):
        result = original(*args, **kwargs)
        published.set()
        assert release.wait(2)
        raise RuntimeError("synthetic lost acknowledgement")

    monkeypatch.setattr(stores.client, "headers", lose_ack)
    first = asyncio.create_task(registration.register_screening_call(call()))
    try:
        assert await asyncio.to_thread(published.wait, 1)
        duplicate = await registration.register_screening_call(call(ws_token="retry"))
        assert duplicate.record["ws_token"] == "original-token"
        if cancel:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
        else:
            release.set()
            winner = await first
            assert winner.record == duplicate.record
            assert winner.created and not duplicate.created
        assert stores.client.records["CA_registration"]["state"] == "screening"
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", [None, "", "../active_calls", "a/b"])
async def test_incoming_invalid_id_never_reaches_datastores(monkeypatch, sid):
    stores = registration_stores(monkeypatch)
    lookup = AsyncMock(side_effect=AssertionError("invalid ID reached datastore"))
    monkeypatch.setattr("app.db.contractors.get_contractor_by_twilio_number", lookup)
    request = Form(CallSid=sid)
    if sid is None:
        del request.data["CallSid"]
    response = await twilio_incoming.handle_incoming_call(request)
    assert response.status_code == 400
    assert stores.documents == {} and stores.client.records == {}
    lookup.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_engine,second_engine", [
    ("elevenlabs", "relay"), ("relay", "elevenlabs"), ("gemini", "relay"),
])
async def test_same_screening_retry_keeps_original_engine_and_identity(monkeypatch, first_engine, second_engine):
    from app.db import contractors, contacts
    from tests.unit.test_call_lifecycle_regressions import _AsyncioProxy, _settle
    stores = registration_stores(monkeypatch)
    incoming(monkeypatch, first_engine)
    proxy = _AsyncioProxy()
    monkeypatch.setattr(twilio_incoming, "asyncio", proxy)
    post = AsyncMock()
    monkeypatch.setattr(twilio_incoming, "_post_routing_tasks", post)
    try:
        first = await twilio_incoming.handle_incoming_call(Form())
        original = deepcopy(stores.client.records)
        contractors.get_contractor_by_twilio_number.return_value["voice_engine"] = second_engine
        contacts.get_contact.return_value = {"name": "Changed Contact"}
        second = await twilio_incoming.handle_incoming_call(Form())
        await asyncio.gather(*proxy.tasks)
        tag = "ConversationRelay" if first_engine == "relay" else "Stream"
        assert ET.fromstring(first.body).find(f".//{tag}") is not None
        assert ET.fromstring(second.body).find(f".//{tag}") is not None
        assert stream_token(first) == stream_token(second)
        assert stores.client.records == original
        assert original["CA_registration"]["registration_voice_engine"] == first_engine
        if first_engine == "relay":
            assert b"Changed Contact" not in second.body
        post.assert_awaited_once()
    finally:
        await _settle(proxy.tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [
    {"ended_at": 123.0}, {"call_status": "completed"}, {"call_status": "busy"},
    {"call_status": "no-answer"}, {"call_status": "canceled"}, {"call_status": "failed"},
])
@pytest.mark.parametrize("live_remains", [False, True])
async def test_incoming_known_terminal_history_never_forwards_or_streams(monkeypatch, terminal, live_remains):
    stores = registration_stores(monkeypatch)
    incoming(monkeypatch)
    assert await registration.register_screening_call(call())
    stores.documents["CA_registration"].update(terminal)
    if not live_remains:
        stores.client.records.clear()
    original = deepcopy(stores.client.records)
    post = AsyncMock()
    monkeypatch.setattr(twilio_incoming, "_post_routing_tasks", post)
    response = await twilio_incoming.handle_incoming_call(Form())
    xml = ET.fromstring(response.body)
    assert xml.find(".//Hangup") is not None
    assert xml.find(".//Dial") is None and xml.find(".//Connect") is None
    assert stores.client.records == original
    post.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tenant", "caller", "accepted", "owner", "engine"])
async def test_incoming_conflicting_authority_never_grants_replacement_token(monkeypatch, change):
    stores = registration_stores(monkeypatch)
    incoming(monkeypatch)
    assert await registration.register_screening_call(call())
    key, value = {
        "tenant": ("contractor_id", "tenant-b"),
        "caller": ("caller_phone", "+15550000003"),
        "accepted": ("accepted", True), "owner": ("owner_action", "decline"),
        "engine": ("registration_voice_engine", "relay"),
    }[change]
    stores.client.records["CA_registration"][key] = value
    original = deepcopy(stores.client.records)
    response = await twilio_incoming.handle_incoming_call(Form())
    xml = ET.fromstring(response.body)
    assert xml.find(".//Hangup") is not None
    assert xml.find(".//Dial") is None and xml.find(".//Connect") is None
    assert stores.client.records == original


@pytest.mark.asyncio
@pytest.mark.parametrize("known_conflict", [None, "terminal", "owner", "tenant", "caller", "accepted"])
async def test_receipt_read_outage_preserves_owner_forward_unless_authority_proves_conflict(monkeypatch, known_conflict):
    from app.db import contractors
    stores = registration_stores(monkeypatch)
    incoming(monkeypatch)
    contractors.get_contractor_by_twilio_number.return_value["owner_phone"] = "+15550000009"
    assert await registration.register_screening_call(call())
    if known_conflict:
        key, value = {
            "terminal": ("state", "ended"), "owner": ("owner_action", "decline"),
            "tenant": ("contractor_id", "tenant-b"), "caller": ("caller_phone", "+15550000003"),
            "accepted": ("accepted", True),
        }[known_conflict]
        stores.client.records["CA_registration"][key] = value
    original = deepcopy(stores.client.records)
    documents = deepcopy(stores.documents)

    def failed_read(*args, **kwargs):
        raise RuntimeError("synthetic receipt lookup outage")

    document_type = type(stores.database.collection("calls").document("CA_registration"))
    monkeypatch.setattr(document_type, "get", failed_read)
    post = AsyncMock()
    monkeypatch.setattr(twilio_incoming, "_post_routing_tasks", post)
    response = await twilio_incoming.handle_incoming_call(Form())
    xml = ET.fromstring(response.body)
    if known_conflict:
        assert xml.find(".//Hangup") is not None and xml.find(".//Dial") is None
    else:
        assert xml.find(".//Number").text == "+15550000009"
        assert xml.find(".//Hangup") is None
    assert xml.find(".//Connect") is None
    assert stores.client.records == original and stores.documents == documents
    post.assert_not_called()


def _competing_authority(disposition):
    record = call(ws_token="preexisting-token").to_dict()
    if disposition in {"ended", "forward"}:
        record = {
            "call_sid": "CA_registration", "state": "ended",
            "state_updated_at": record["state_updated_at"],
        }
        if disposition == "forward":
            record.update(contractor_id="tenant-a", registration_disposition="forward")
    else:
        key, value = {
            "accepted": ("accepted", True), "owner": ("owner_action", "decline"),
            "tenant": ("contractor_id", "tenant-b"), "direct": ("state", "pickup_ringing"),
        }[disposition]
        record[key] = value
    return record


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["ended", "accepted", "owner", "tenant", "direct", "forward"])
@pytest.mark.parametrize("etag_conflict", [False, True])
async def test_first_observed_disposition_survives_unavailable_readback(monkeypatch, disposition, etag_conflict):
    """Real SDK retries must retain positive evidence, including a forward fence."""
    stores = registration_stores(monkeypatch)
    incoming(monkeypatch)
    existing = _competing_authority(disposition)
    if etag_conflict:
        def intervening_write(records):
            records["CA_registration"] = deepcopy(existing)
        stores.client.before_write = intervening_write
    else:
        stores.client.records["CA_registration"] = deepcopy(existing)
    original_read, original_write = stores.client.headers_and_body, stores.client.headers
    reads, write_attempts = 0, 0

    def only_first_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads > 1:
            raise RuntimeError("synthetic resolver readback outage after known disposition")
        return original_read(*args, **kwargs)

    def count_write(*args, **kwargs):
        nonlocal write_attempts
        write_attempts += 1
        return original_write(*args, **kwargs)

    monkeypatch.setattr(stores.client, "headers_and_body", only_first_read)
    monkeypatch.setattr(stores.client, "headers", count_write)
    post = AsyncMock()
    monkeypatch.setattr(twilio_incoming, "_post_routing_tasks", post)
    assert stores.documents == {}  # This request, not a prior retry, owns the receipt.
    response = await twilio_incoming.handle_incoming_call(Form())
    receipt = stores.documents["CA_registration"][registration.RECEIPT_FIELD]
    assert receipt["nonce"] and len(receipt["token_digest"]) == 64
    assert stores.documents["CA_registration"]["contractor_id"] == "tenant-a"
    xml = ET.fromstring(response.body)
    assert xml.find(".//Connect") is None
    if disposition == "forward":
        assert xml.find(".//Number").text == "+15550000001"
        assert xml.find(".//Hangup") is None
    else:
        assert xml.find(".//Hangup") is not None
        assert xml.find(".//Dial") is None
    assert stores.client.records == {"CA_registration": existing}
    assert reads == 1 and write_attempts == int(etag_conflict)
    post.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["stamp", "expired", "circuit", "direct", "spam"])
async def test_existing_early_routes_cannot_overwrite_registered_screening(monkeypatch, branch):
    """Characterize the scoped guarantee; these routes still return other TwiML."""
    from app.db import contractors
    from app.services.routing import Route
    from tests.unit.test_call_lifecycle_regressions import _AsyncioProxy, _settle
    stores = registration_stores(monkeypatch)
    incoming(monkeypatch)
    assert await registration.register_screening_call(call())
    original = deepcopy(stores.client.records)
    proxy = _AsyncioProxy()
    monkeypatch.setattr(twilio_incoming, "asyncio", proxy)
    monkeypatch.setattr(twilio_incoming, "_post_routing_tasks", AsyncMock())
    monkeypatch.setattr(twilio_incoming, "_handle_deleted_app", AsyncMock())
    ring = AsyncMock()
    monkeypatch.setattr(twilio_incoming, "_ring_contractor", ring)
    if branch == "stamp":
        monkeypatch.setattr(twilio_incoming, "_record_inbound_call_evidence", AsyncMock(return_value=False))
    elif branch == "expired":
        contractors.get_contractor_by_twilio_number.return_value["subscription_status"] = "expired"
        monkeypatch.setattr("app.services.push_notification.get_device_token", AsyncMock(return_value=None))
    elif branch == "circuit":
        monkeypatch.setattr("app.services.circuit_breaker.is_circuit_open", lambda: True)
    else:
        contractors.get_contractor_by_twilio_number.return_value["ring_through_contacts"] = True
        route = Route.WHITELIST_FORWARD if branch == "direct" else Route.SPAM_BLOCK
        monkeypatch.setattr("app.services.routing.determine_route", lambda _: route)
    try:
        response = await twilio_incoming.handle_incoming_call(Form())
        await asyncio.gather(*proxy.tasks)
        assert ET.fromstring(response.body).find(".//Connect") is None
        assert stores.client.records == original
        ring.assert_not_called()
    finally:
        await _settle(proxy.tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("screening_wins", [False, True])
async def test_competing_direct_initialization_and_screening_never_replace_winner(monkeypatch, screening_wins):
    stores = registration_stores(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def conference(*args):
        entered.set()
        await release.wait()

    stores.conference.side_effect = conference
    task = asyncio.create_task(registration.register_screening_call(call()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if screening_wins:
            release.set()
            registered = await task
            original = deepcopy(stores.client.records)
            assert not await twilio_incoming._prepare_direct_call(
                "CA_registration", "tenant-a", "different-conference", "+15550000002", "Synthetic",
            )
            assert registered.created
        else:
            assert await twilio_incoming._prepare_direct_call(
                "CA_registration", "tenant-a", "different-conference", "+15550000002", "Synthetic",
            )
            original = deepcopy(stores.client.records)
            release.set()
            with pytest.raises(registration.RegistrationRejected):
                await task
        assert stores.client.records == original
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


class _UnresolvedRoutingReplayGap(AssertionError):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned,current,redirect,expected", [
    ("gemini", "elevenlabs", False, "gemini"),
    ("elevenlabs", "gemini", False, "elevenlabs"),
    (None, "gemini", False, "gemini"),
    (None, "elevenlabs", False, "elevenlabs"),
    ("gemini", "elevenlabs", True, "elevenlabs"),
])
async def test_media_setup_uses_registered_engine_but_keeps_legacy_and_redirect_behavior(
    monkeypatch, pinned, current, redirect, expected,
):
    from types import SimpleNamespace
    from app.webhooks import media_stream
    from tests.unit.test_call_lifecycle_regressions import _SetupSocket
    stores = registration_stores(monkeypatch)
    active = call()
    record = active.to_dict()
    if pinned:
        record["registration_voice_engine"] = pinned
    if redirect:
        record["kevin_redirect_nonce"] = "synthetic-owner-redirect"
    stores.client.records["CA_registration"] = record
    monkeypatch.setattr(media_stream, "_init_firebase", lambda: None)
    monkeypatch.setattr(media_stream, "_resolve_active_call", AsyncMock(return_value=active))
    monkeypatch.setattr("app.db.contractors.get_contractor", AsyncMock(return_value={
        "contractor_id": "tenant-a", "voice_engine": current, "mode": "personal",
    }))
    monkeypatch.setattr("app.services.receptionist_context.load_customer_memory_context", AsyncMock(return_value={}))
    monkeypatch.setattr("app.services.owner_call_actions.confirm_fallback_stream", AsyncMock(return_value=True))
    monkeypatch.setattr(media_stream.settings, "gemini_api_key", "synthetic-key")
    selected = []

    def pipeline(engine):
        def construct(**kwargs):
            selected.append(engine)
            return SimpleNamespace(stop=AsyncMock())
        return construct

    monkeypatch.setattr(media_stream, "VoicePipeline", pipeline("elevenlabs"))
    monkeypatch.setattr("app.services.gemini_pipeline.GeminiPipeline", pipeline("gemini"))
    monkeypatch.setattr(media_stream, "_serve_pipeline_ingress", AsyncMock(return_value=True))
    await media_stream.media_stream_ws(_SetupSocket("media", "original-token"), "CA_registration")
    assert selected == [expected]


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True, raises=_UnresolvedRoutingReplayGap,
    reason="Caller-preserving outage forwarding cannot revoke an unacknowledged RTDB write; global route arbitration is a separate slice",
)
async def test_unknown_late_write_replay_should_keep_the_original_routing_disposition(monkeypatch):
    stores = registration_stores(monkeypatch)
    incoming(monkeypatch)
    monkeypatch.setattr(twilio_incoming, "_post_routing_tasks", AsyncMock())
    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    original = stores.client.headers
    attempts = 0

    def delayed(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            assert release.wait(2)
            try:
                return original(*args, **kwargs)
            finally:
                settled.set()
        raise RuntimeError("synthetic fence unacknowledged")

    monkeypatch.setattr(stores.client, "headers", delayed)
    monkeypatch.setattr(registration, "REGISTRATION_TIMEOUT_SECONDS", 0.2)
    task = asyncio.create_task(twilio_incoming.handle_incoming_call(Form()))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        first = await task
        assert ET.fromstring(first.body).find(".//Number").text == "+15550000001"
        release.set()
        assert await asyncio.to_thread(settled.wait, 1)
        second = await twilio_incoming.handle_incoming_call(Form())
        assert stores.client.records["CA_registration"]["contractor_id"] == "tenant-a"
        second_xml = ET.fromstring(second.body)
        if second_xml.find(".//Stream") is not None:
            assert stream_token(second) == stores.client.records["CA_registration"]["ws_token"]
            raise _UnresolvedRoutingReplayGap("Retry changed a caller-preserving Dial into Stream after late commit")
        assert second_xml.find(".//Number").text == "+15550000001"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
