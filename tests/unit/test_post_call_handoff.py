"""Durable, at-most-once post-call handoff behavior."""

import asyncio
import inspect
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.db import post_call_handoffs as handoff_db
from app.db import calls as call_db
from app.services import post_call, post_call_handoff
from app.webhooks import media_stream
import app.main as app_main


def test_pending_handoff_claims_once_with_a_lease():
    claimed, updates = handoff_db._claim_transition(
        {"status": "pending", "attempts": 1},
        now=100.0,
        lease_seconds=60,
    )

    assert claimed is True
    assert updates == {
        "status": "in_progress",
        "attempts": 2,
        "started_at": 100.0,
        "lease_expires_at": 160.0,
    }


def test_stale_claim_becomes_uncertain_instead_of_replaying():
    claimed, updates = handoff_db._claim_transition(
        {
            "status": "in_progress",
            "attempts": 1,
            "lease_expires_at": 99.0,
        },
        now=100.0,
        lease_seconds=60,
    )

    assert claimed is False
    assert updates == {
        "status": "needs_attention",
        "finished_at": 100.0,
        "lease_expires_at": 0,
        "failure_code": "lease_expired_uncertain",
    }


def test_active_claim_is_not_replayed_or_changed():
    claimed, updates = handoff_db._claim_transition(
        {
            "status": "in_progress",
            "attempts": 1,
            "lease_expires_at": 101.0,
        },
        now=100.0,
        lease_seconds=60,
    )

    assert claimed is False
    assert updates == {}


def test_terminal_handoff_is_not_claimed_again():
    for status in ("completed", "needs_attention"):
        claimed, updates = handoff_db._claim_transition(
            {"status": status, "attempts": 1},
            now=100.0,
            lease_seconds=60,
        )
        assert claimed is False
        assert updates == {}


@pytest.mark.parametrize(
    ("result_status", "expected"),
    [
        ("complete", ("completed", "")),
        ("partial", ("needs_attention", "partial_delivery")),
        ("failed", ("needs_attention", "processing_failed")),
        ("unexpected", ("needs_attention", "processing_failed")),
    ],
)
def test_terminal_outcome_uses_safe_failure_codes(result_status, expected):
    assert handoff_db.terminal_outcome(result_status) == expected


def test_terminal_transitions_do_not_overwrite_an_existing_terminal_state():
    completed_updates = {"status": "completed", "failure_code": ""}
    attention_updates = {
        "status": "needs_attention",
        "failure_code": "processing_timeout",
    }

    assert handoff_db._finish_transition(
        {"status": "in_progress"},
        completed_updates,
    ) == (True, completed_updates)
    assert handoff_db._finish_transition(
        {"status": "needs_attention"},
        completed_updates,
    ) == (False, {})
    assert handoff_db._attention_transition(
        {"status": "completed"},
        attention_updates,
    ) == (False, {})
    assert handoff_db._attention_transition(
        {"status": "in_progress"},
        attention_updates,
    ) == (True, attention_updates)


@pytest.mark.asyncio
@pytest.mark.parametrize("already_exists", [False, True])
async def test_enqueue_is_create_once_and_idempotent(monkeypatch, already_exists):
    payloads = []

    class Document:
        def create(self, payload):
            payloads.append(payload)
            if already_exists:
                raise handoff_db.AlreadyExists("already exists")

    class Collection:
        def document(self, call_sid):
            assert call_sid == "CA_test"
            return Document()

    class Firestore:
        def collection(self, name):
            assert name == handoff_db.COLLECTION
            return Collection()

    monkeypatch.setattr(handoff_db, "get_firestore_client", Firestore)

    enqueued = await handoff_db.enqueue_handoff(
        call_sid="CA_test",
        contractor_id="contractor-test",
        caller_language="es",
    )

    assert enqueued is True
    assert len(payloads) == 1
    assert payloads[0]["status"] == "pending"
    assert payloads[0]["contractor_id"] == "contractor-test"
    assert payloads[0]["caller_language"] == "es"
    assert "transcript" not in payloads[0]
    assert "caller_phone" not in payloads[0]


@pytest.mark.asyncio
async def test_enqueue_and_run_claims_and_processes_only_once(monkeypatch):
    claims = iter((True, False))
    processed = []
    finished = []
    mirrored = []

    async def enqueue(**_kwargs):
        return True

    async def claim(_call_sid):
        return next(claims)

    async def finish(call_sid, result):
        finished.append((call_sid, result.status))
        return True

    async def process(**kwargs):
        processed.append(kwargs["call_sid"])
        return post_call.PostCallResult(
            status="complete",
            completed_effects=("call_record",),
            failed_effects=(),
        )

    async def save_call(call_sid, updates):
        mirrored.append((call_sid, updates["post_call_status"]))
        return True

    monkeypatch.setattr(post_call_handoff.handoff_db, "enqueue_handoff", enqueue)
    monkeypatch.setattr(post_call_handoff.handoff_db, "claim_handoff", claim)
    monkeypatch.setattr(post_call_handoff.handoff_db, "finish_handoff", finish)
    monkeypatch.setattr(post_call_handoff, "process_post_call", process)
    monkeypatch.setattr(post_call_handoff.call_db, "save_call", save_call)

    kwargs = {
        "transcript_lines": ["Caller: routine request"],
        "caller_phone": "test-caller-number",
        "call_sid": "CA_test",
        "contractor": {"contractor_id": "contractor-test"},
        "caller_language": "en",
    }
    first = await post_call_handoff.enqueue_and_run_post_call(**kwargs)
    second = await post_call_handoff.enqueue_and_run_post_call(**kwargs)

    assert first == "completed"
    assert second == "deduplicated"
    assert processed == ["CA_test"]
    assert finished == [("CA_test", "complete")]
    assert mirrored == [("CA_test", "completed")]


@pytest.mark.asyncio
async def test_handoff_timeout_is_terminal_and_not_blindly_retried(monkeypatch):
    attention = []
    mirrored = []

    async def enqueue(**_kwargs):
        return True

    async def claim(_call_sid):
        return True

    async def process(**_kwargs):
        await asyncio.Event().wait()

    async def mark_attention(call_sid, failure_code):
        attention.append((call_sid, failure_code))
        return True

    async def save_call(call_sid, updates):
        mirrored.append((call_sid, updates))
        return True

    monkeypatch.setattr(post_call_handoff, "POST_CALL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(post_call_handoff.handoff_db, "enqueue_handoff", enqueue)
    monkeypatch.setattr(post_call_handoff.handoff_db, "claim_handoff", claim)
    monkeypatch.setattr(
        post_call_handoff.handoff_db,
        "mark_needs_attention",
        mark_attention,
    )
    monkeypatch.setattr(post_call_handoff, "process_post_call", process)
    monkeypatch.setattr(post_call_handoff.call_db, "save_call", save_call)

    result = await post_call_handoff.enqueue_and_run_post_call(
        transcript_lines=["Caller: routine request"],
        caller_phone="test-caller-number",
        call_sid="CA_test",
        contractor={"contractor_id": "contractor-test"},
    )

    assert result == "needs_attention"
    assert attention == [("CA_test", "processing_timeout")]
    assert mirrored[0][1]["post_call_status"] == "needs_attention"
    assert mirrored[0][1]["post_call_failure_code"] == "processing_timeout"


@pytest.mark.asyncio
async def test_missing_inline_contractor_context_stays_pending(monkeypatch):
    mirrored = []

    async def enqueue(**_kwargs):
        return True

    async def unexpected_claim(_call_sid):
        pytest.fail("missing tenant context must not run inline side effects")

    async def save_call(_call_sid, updates):
        mirrored.append(updates)
        return True

    monkeypatch.setattr(post_call_handoff.handoff_db, "enqueue_handoff", enqueue)
    monkeypatch.setattr(post_call_handoff.handoff_db, "claim_handoff", unexpected_claim)
    monkeypatch.setattr(post_call_handoff.call_db, "save_call", save_call)

    status = await post_call_handoff.enqueue_and_run_post_call(
        transcript_lines=["Caller: routine request"],
        caller_phone="test-caller-number",
        call_sid="CA_test",
        contractor={},
    )

    assert status == "pending"
    assert mirrored == [
        {
            "post_call_status": "pending",
            "post_call_failure_code": "",
            "post_call_completed_effects": [],
            "post_call_failed_effects": [],
        }
    ]


@pytest.mark.asyncio
async def test_hydration_rejects_cross_tenant_contractor_mismatch(monkeypatch):
    async def get_handoff(_call_sid):
        return {"contractor_id": "contractor-a"}

    async def get_call(_call_sid):
        return {
            "contractor_id": "contractor-b",
            "transcript": "Caller: routine request",
        }

    monkeypatch.setattr(post_call_handoff.handoff_db, "get_handoff", get_handoff)
    monkeypatch.setattr(post_call_handoff.call_db, "get_call", get_call)

    with pytest.raises(RuntimeError, match="contractor mismatch"):
        await post_call_handoff._hydrate_handoff("CA_test")


@pytest.mark.asyncio
async def test_worker_hydrates_pending_handoff_from_durable_records(monkeypatch):
    processed = []
    mirrored = []

    async def list_ids(status, *, limit):
        assert limit > 0
        return ["CA_test"] if status == "pending" else []

    async def claim(_call_sid):
        return True

    async def get_handoff(_call_sid):
        return {"contractor_id": "contractor-test", "caller_language": "es"}

    async def get_call(_call_sid):
        return {
            "transcript": "Caller: routine request\nKevin: thank you",
            "caller_phone": "test-caller-number",
        }

    async def get_contractor(_contractor_id):
        return {
            "contractor_id": "contractor-test",
            "owner_phone": "test-owner-number",
            "twilio_number": "test-twilio-number",
        }

    async def process(**kwargs):
        processed.append(kwargs)
        return post_call.PostCallResult(
            status="complete",
            completed_effects=("call_record",),
            failed_effects=(),
        )

    async def finish(_call_sid, _result):
        return True

    async def save_call(_call_sid, updates):
        mirrored.append(updates)
        return True

    monkeypatch.setattr(post_call_handoff.handoff_db, "list_handoff_ids", list_ids)
    monkeypatch.setattr(post_call_handoff.handoff_db, "claim_handoff", claim)
    monkeypatch.setattr(post_call_handoff.handoff_db, "get_handoff", get_handoff)
    monkeypatch.setattr(post_call_handoff.handoff_db, "finish_handoff", finish)
    monkeypatch.setattr(post_call_handoff.call_db, "get_call", get_call)
    monkeypatch.setattr(post_call_handoff.call_db, "save_call", save_call)
    monkeypatch.setattr("app.db.contractors.get_contractor", get_contractor)
    monkeypatch.setattr(post_call_handoff, "process_post_call", process)

    await post_call_handoff.run_pending_post_calls_once(limit=3)

    assert len(processed) == 1
    assert processed[0]["transcript_lines"] == [
        "Caller: routine request",
        "Kevin: thank you",
    ]
    assert processed[0]["caller_language"] == "es"
    assert processed[0]["contractor"]["contractor_id"] == "contractor-test"
    assert mirrored[0]["post_call_status"] == "completed"


@pytest.mark.asyncio
async def test_worker_mirrors_stale_uncertain_handoff_without_replaying(monkeypatch):
    mirrored = []

    async def list_ids(status, *, limit):
        assert limit > 0
        return ["CA_test"] if status == "in_progress" else []

    async def claim(_call_sid):
        return False

    async def get_handoff(_call_sid):
        return {
            "status": "needs_attention",
            "failure_code": "lease_expired_uncertain",
        }

    async def save_call(_call_sid, updates):
        mirrored.append(updates)
        return True

    monkeypatch.setattr(post_call_handoff.handoff_db, "list_handoff_ids", list_ids)
    monkeypatch.setattr(post_call_handoff.handoff_db, "claim_handoff", claim)
    monkeypatch.setattr(post_call_handoff.handoff_db, "get_handoff", get_handoff)
    monkeypatch.setattr(post_call_handoff.call_db, "save_call", save_call)

    await post_call_handoff.run_pending_post_calls_once(limit=3)

    assert mirrored == [
        {
            "post_call_status": "needs_attention",
            "post_call_failure_code": "lease_expired_uncertain",
            "post_call_completed_effects": [],
            "post_call_failed_effects": [],
        }
    ]


@pytest.mark.asyncio
async def test_finish_persistence_failure_is_quarantined(monkeypatch):
    attention = []
    mirrored = []

    async def claim(_call_sid):
        return True

    async def process(**_kwargs):
        return post_call.PostCallResult(
            status="complete",
            completed_effects=("owner_sms",),
            failed_effects=(),
        )

    async def finish(_call_sid, _result):
        return False

    async def mark_attention(call_sid, failure_code):
        attention.append((call_sid, failure_code))
        return True

    async def save_call(_call_sid, updates):
        mirrored.append(updates)
        return True

    monkeypatch.setattr(post_call_handoff.handoff_db, "claim_handoff", claim)
    monkeypatch.setattr(post_call_handoff.handoff_db, "finish_handoff", finish)
    monkeypatch.setattr(
        post_call_handoff.handoff_db,
        "mark_needs_attention",
        mark_attention,
    )
    monkeypatch.setattr(post_call_handoff, "process_post_call", process)
    monkeypatch.setattr(post_call_handoff.call_db, "save_call", save_call)

    status = await post_call_handoff.run_post_call_handoff(
        "CA_test",
        transcript_lines=["Caller: routine request"],
    )

    assert status == "needs_attention"
    assert attention == [("CA_test", "handoff_finish_failed")]
    assert mirrored[0]["post_call_failure_code"] == "handoff_finish_failed"


@pytest.mark.asyncio
async def test_late_cancellation_does_not_overwrite_persisted_terminal_state(
    monkeypatch,
):
    attention = []

    async def claim(_call_sid):
        return True

    async def process(**_kwargs):
        return post_call.PostCallResult(
            status="complete",
            completed_effects=("owner_sms",),
            failed_effects=(),
        )

    async def finish(_call_sid, _result):
        return True

    async def mark_attention(call_sid, failure_code):
        attention.append((call_sid, failure_code))
        return True

    async def cancel_during_mirror(_call_sid, _updates):
        raise asyncio.CancelledError

    monkeypatch.setattr(post_call_handoff.handoff_db, "claim_handoff", claim)
    monkeypatch.setattr(post_call_handoff.handoff_db, "finish_handoff", finish)
    monkeypatch.setattr(
        post_call_handoff.handoff_db,
        "mark_needs_attention",
        mark_attention,
    )
    monkeypatch.setattr(post_call_handoff, "process_post_call", process)
    monkeypatch.setattr(post_call_handoff.call_db, "save_call", cancel_during_mirror)

    with pytest.raises(asyncio.CancelledError):
        await post_call_handoff.run_post_call_handoff(
            "CA_test",
            transcript_lines=["Caller: routine request"],
        )

    assert attention == []


@pytest.mark.asyncio
async def test_save_call_returns_provider_write_outcome(monkeypatch):
    class Document:
        def __init__(self, should_fail):
            self.should_fail = should_fail

        def set(self, _data, *, merge):
            assert merge is True
            if self.should_fail:
                raise RuntimeError("write failed")

    class Collection:
        def __init__(self, should_fail):
            self.should_fail = should_fail

        def document(self, _call_sid):
            return Document(self.should_fail)

    class Firestore:
        def __init__(self, should_fail):
            self.should_fail = should_fail

        def collection(self, _name):
            return Collection(self.should_fail)

    monkeypatch.setattr(call_db, "get_firestore_client", lambda: Firestore(False))
    assert await call_db.save_call("CA_test", {"status": "completed"}) is True

    monkeypatch.setattr(call_db, "get_firestore_client", lambda: Firestore(True))
    assert await call_db.save_call("CA_test", {"status": "completed"}) is False


def test_media_stream_awaits_durable_handoff_without_detached_post_call_tasks():
    source = inspect.getsource(media_stream.media_stream_ws)

    assert "enqueue_and_run_post_call" in source
    assert "create_task(process_post_call" not in source
    assert "create_task(_post_call_extract" not in source
    assert "if transcript_saved and active_call" in source


def test_startup_launches_pending_handoff_worker():
    source = inspect.getsource(app_main.startup)

    assert "post_call_worker_loop" in source


@pytest.mark.asyncio
async def test_shutdown_cancels_post_call_worker(monkeypatch):
    async def wait_forever():
        await asyncio.Event().wait()

    task = asyncio.create_task(wait_forever())
    monkeypatch.setattr(app_main, "_post_call_worker_task", task)

    await app_main.shutdown()

    assert task.cancelled()
    assert app_main._post_call_worker_task is None
