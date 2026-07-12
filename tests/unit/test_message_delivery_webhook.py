"""Authenticated Twilio status callbacks stay payload-free and replay-safe."""

import asyncio
import logging
import os
from types import SimpleNamespace
from contextlib import asynccontextmanager
import time

from fastapi import FastAPI
import httpx
import pytest
from twilio.request_validator import RequestValidator


os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

from app.db import message_delivery_receipts as receipt_db
from app.services import message_delivery
from app.services import post_call_handoff
from app.webhooks import twilio_message_status


MESSAGE_SID = "SM" + ("a" * 32)


@asynccontextmanager
async def _signed_client(monkeypatch, handler):
    monkeypatch.setattr(
        twilio_message_status.message_delivery,
        "handle_provider_status",
        handler,
    )
    monkeypatch.setattr(
        "app.middleware.twilio_verify.settings.twilio_auth_token",
        "test-auth-token",
    )
    app = FastAPI()
    app.include_router(twilio_message_status.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
    ) as client:
        yield client


def _signature(path: str, params: dict[str, str]) -> str:
    return RequestValidator("test-auth-token").compute_signature(
        f"https://testserver{path}",
        params,
    )


@pytest.mark.asyncio
async def test_valid_callback_passes_only_allowlisted_fields(monkeypatch, caplog):
    received = []

    async def handle_provider_status(**kwargs):
        received.append(kwargs)
        return "updated"

    path = "/webhooks/twilio/message-status/receipt-test"
    private_text = "private callback payload and destination"
    params = {
        "MessageSid": MESSAGE_SID,
        "MessageStatus": "delivered",
        "ErrorCode": "",
        "Body": private_text,
        "To": "+15555550102",
        "ChannelStatusMessage": private_text,
    }
    caplog.set_level(logging.INFO)

    async with _signed_client(monkeypatch, handle_provider_status) as client:
        response = await client.post(
            path,
            data=params,
            headers={"X-Twilio-Signature": _signature(path, params)},
        )

    assert response.status_code == 204
    assert received == [
        {
            "receipt_id": "receipt-test",
            "provider_message_sid": MESSAGE_SID,
            "provider_status": "delivered",
            "provider_error_code": "",
        }
    ]
    assert private_text not in caplog.text
    assert "+15555550102" not in caplog.text


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected_without_logging_callback_url(
    monkeypatch,
    caplog,
):
    async def unexpected_handler(**_kwargs):
        pytest.fail("invalid signature must not reach the handler")

    path = "/webhooks/twilio/message-status/private-receipt-token"
    caplog.set_level(logging.WARNING)

    async with _signed_client(monkeypatch, unexpected_handler) as client:
        response = await client.post(
            path,
            data={"MessageSid": MESSAGE_SID, "MessageStatus": "delivered"},
            headers={"X-Twilio-Signature": "invalid"},
        )

    assert response.status_code == 403
    assert "private-receipt-token" not in caplog.text


@pytest.mark.asyncio
async def test_storage_failure_returns_retriable_status(monkeypatch):
    async def handle_provider_status(**_kwargs):
        return "error"

    path = "/webhooks/twilio/message-status/receipt-test"
    params = {"MessageSid": MESSAGE_SID, "MessageStatus": "sent"}

    async with _signed_client(monkeypatch, handle_provider_status) as client:
        response = await client.post(
            path,
            data=params,
            headers={"X-Twilio-Signature": _signature(path, params)},
        )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_failed_delivery_mirrors_safe_call_status_without_provider_payload(
    monkeypatch,
    caplog,
):
    projected = []
    private_text = "private carrier payload"

    async def record_provider_update(*_args, **_kwargs):
        return receipt_db.ReceiptUpdate(
            "updated",
            {
                "receipt_id": "receipt-test",
                "call_sid": "CA_test",
                "effect": "owner_sms",
                "channel": "sms",
                "status": "failed",
                "provider_status": "undelivered",
                "provider_error_code": 30001,
                "failure_code": "provider_delivery_failed",
                "updated_at": 20.0,
            },
        )

    async def project_receipt_to_call(receipt_id):
        projected.append(receipt_id)
        return True

    monkeypatch.setattr(
        message_delivery.receipt_db,
        "record_provider_update",
        record_provider_update,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "project_receipt_to_call",
        project_receipt_to_call,
    )
    caplog.set_level(logging.INFO, logger="app.services.message_delivery")

    outcome = await message_delivery.handle_provider_status(
        receipt_id="receipt-test",
        provider_message_sid=MESSAGE_SID,
        provider_status="undelivered",
        provider_error_code="30001",
    )

    assert outcome == "updated"
    assert projected == ["receipt-test"]
    assert "message_delivery event=terminal_failure" in caplog.text
    assert private_text not in caplog.text
    assert MESSAGE_SID not in caplog.text


@pytest.mark.asyncio
async def test_unexpected_receipt_storage_exception_is_retriable(monkeypatch):
    async def record_provider_update(*_args, **_kwargs):
        raise RuntimeError("private storage failure")

    monkeypatch.setattr(
        message_delivery.receipt_db,
        "record_provider_update",
        record_provider_update,
    )

    outcome = await message_delivery.handle_provider_status(
        receipt_id="receipt-test",
        provider_message_sid=MESSAGE_SID,
        provider_status="sent",
    )

    assert outcome == "error"


@pytest.mark.asyncio
async def test_reconciliation_fetches_old_pending_message_without_resending(
    monkeypatch,
):
    events = []
    updates = []
    reconciliation_marks = []
    clients = []

    async def list_candidates(**_kwargs):
        return [
            receipt_db.ReconciliationCandidate(
                receipt_id="receipt-test",
                provider_message_sid=MESSAGE_SID,
            )
        ]

    async def claim_reconciliation(receipt_id):
        assert receipt_id == "receipt-test"
        return receipt_db.ReconciliationCandidate(
            receipt_id="receipt-test",
            provider_message_sid=MESSAGE_SID,
            lease_token="lease-test",
        )

    async def handle_provider_status(**kwargs):
        updates.append(kwargs)
        return "updated"

    async def mark_reconciled(receipt_id, *, lease_token):
        reconciliation_marks.append((receipt_id, lease_token))
        return True

    class MessageResource:
        def fetch(self):
            events.append("fetch")
            return SimpleNamespace(status="delivered", error_code=None)

    class Messages:
        def __call__(self, message_sid):
            assert message_sid == MESSAGE_SID
            return MessageResource()

        def create(self, **_kwargs):
            pytest.fail("reconciliation must never resend")

    class Client:
        messages = Messages()

    monkeypatch.setattr(
        message_delivery.receipt_db,
        "list_reconciliation_candidates",
        list_candidates,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "claim_reconciliation",
        claim_reconciliation,
    )
    monkeypatch.setattr(message_delivery, "handle_provider_status", handle_provider_status)
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "mark_reconciled",
        mark_reconciled,
    )
    def client_constructor(*_args, **kwargs):
        clients.append(kwargs["http_client"])
        return Client()

    monkeypatch.setattr(message_delivery, "Client", client_constructor)

    reconciled = await message_delivery.reconcile_pending_receipts_once()

    assert reconciled == 1
    assert events == ["fetch"]
    assert reconciliation_marks == [("receipt-test", "lease-test")]
    assert clients[0].timeout == message_delivery.TWILIO_HTTP_TIMEOUT_SECONDS
    assert updates == [
        {
            "receipt_id": "receipt-test",
            "provider_message_sid": MESSAGE_SID,
            "provider_status": "delivered",
            "provider_error_code": None,
        }
    ]


@pytest.mark.asyncio
async def test_reconciliation_emits_payload_free_warning_for_stale_pending(
    monkeypatch,
    caplog,
):
    private_text = "private message and destination"

    async def list_candidates(**_kwargs):
        return [
            receipt_db.ReconciliationCandidate(
                receipt_id="receipt-test",
                provider_message_sid=MESSAGE_SID,
            )
        ]

    async def claim_reconciliation(_receipt_id):
        return receipt_db.ReconciliationCandidate(
            receipt_id="receipt-test",
            provider_message_sid=MESSAGE_SID,
            lease_token="lease-test",
        )

    async def handle_provider_status(**_kwargs):
        return "ignored"

    async def mark_reconciled(_receipt_id, *, lease_token):
        assert lease_token == "lease-test"
        return True

    class MessageResource:
        def fetch(self):
            return SimpleNamespace(status="sent", error_code=None)

    class Messages:
        def __call__(self, _message_sid):
            return MessageResource()

        def create(self, **_kwargs):
            pytest.fail("reconciliation must never resend")

    class Client:
        messages = Messages()

    monkeypatch.setattr(
        message_delivery.receipt_db,
        "list_reconciliation_candidates",
        list_candidates,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "claim_reconciliation",
        claim_reconciliation,
    )
    monkeypatch.setattr(message_delivery, "handle_provider_status", handle_provider_status)
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "mark_reconciled",
        mark_reconciled,
    )
    monkeypatch.setattr(message_delivery, "Client", lambda *_args, **_kwargs: Client())
    caplog.set_level(logging.WARNING, logger="app.services.message_delivery")

    reconciled = await message_delivery.reconcile_pending_receipts_once()

    assert reconciled == 1
    assert "message_delivery event=reconciliation_pending" in caplog.text
    assert "provider_status=sent" in caplog.text
    assert MESSAGE_SID not in caplog.text
    assert private_text not in caplog.text


@pytest.mark.asyncio
async def test_reconciliation_quarantines_missing_provider_identifier(monkeypatch):
    marked = []
    projected = []

    async def list_candidates(**_kwargs):
        return [
            receipt_db.ReconciliationCandidate(
                receipt_id="receipt-test",
                provider_message_sid="",
            )
        ]

    async def claim_reconciliation(_receipt_id):
        return receipt_db.ReconciliationCandidate(
            receipt_id="receipt-test",
            provider_message_sid="",
            lease_token="lease-test",
        )

    async def mark_missing_provider_id(receipt_id, *, lease_token):
        marked.append((receipt_id, lease_token))
        return True

    async def project_receipt_to_call(receipt_id):
        projected.append(receipt_id)
        return True

    monkeypatch.setattr(
        message_delivery.receipt_db,
        "list_reconciliation_candidates",
        list_candidates,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "claim_reconciliation",
        claim_reconciliation,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "mark_missing_provider_id",
        mark_missing_provider_id,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "project_receipt_to_call",
        project_receipt_to_call,
    )

    reconciled = await message_delivery.reconcile_pending_receipts_once()

    assert reconciled == 1
    assert marked == [("receipt-test", "lease-test")]
    assert projected == ["receipt-test"]


@pytest.mark.asyncio
async def test_competing_reconciliation_workers_fetch_only_once(monkeypatch):
    claims = 0
    fetches = []

    async def list_candidates(**_kwargs):
        return [
            receipt_db.ReconciliationCandidate(
                receipt_id="receipt-test",
                provider_message_sid=MESSAGE_SID,
            )
        ]

    async def claim_reconciliation(_receipt_id):
        nonlocal claims
        claims += 1
        if claims > 1:
            return None
        return receipt_db.ReconciliationCandidate(
            receipt_id="receipt-test",
            provider_message_sid=MESSAGE_SID,
            lease_token="lease-test",
        )

    async def handle_provider_status(**_kwargs):
        return "updated"

    async def mark_reconciled(_receipt_id, *, lease_token):
        assert lease_token == "lease-test"
        return True

    class MessageResource:
        def fetch(self):
            fetches.append("fetch")
            return SimpleNamespace(status="delivered", error_code=None)

    class Messages:
        def __call__(self, _message_sid):
            return MessageResource()

        def create(self, **_kwargs):
            pytest.fail("reconciliation must never resend")

    class Client:
        messages = Messages()

    monkeypatch.setattr(
        message_delivery.receipt_db,
        "list_reconciliation_candidates",
        list_candidates,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "claim_reconciliation",
        claim_reconciliation,
    )
    monkeypatch.setattr(message_delivery, "handle_provider_status", handle_provider_status)
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "mark_reconciled",
        mark_reconciled,
    )
    monkeypatch.setattr(message_delivery, "Client", lambda *_args, **_kwargs: Client())

    results = await asyncio.gather(
        message_delivery.reconcile_pending_receipts_once(),
        message_delivery.reconcile_pending_receipts_once(),
    )

    assert sorted(results) == [0, 1]
    assert fetches == ["fetch"]


@pytest.mark.asyncio
async def test_reconciliation_fetch_timeout_releases_lease_without_resend(
    monkeypatch,
    caplog,
):
    marks = []

    async def list_candidates(**_kwargs):
        return [
            receipt_db.ReconciliationCandidate(
                receipt_id="receipt-test",
                provider_message_sid=MESSAGE_SID,
            )
        ]

    async def claim_reconciliation(_receipt_id):
        return receipt_db.ReconciliationCandidate(
            receipt_id="receipt-test",
            provider_message_sid=MESSAGE_SID,
            lease_token="lease-test",
        )

    async def mark_reconciled(receipt_id, *, lease_token):
        marks.append((receipt_id, lease_token))
        return True

    class MessageResource:
        def fetch(self):
            time.sleep(0.05)
            return SimpleNamespace(status="sent", error_code=None)

    class Messages:
        def __call__(self, _message_sid):
            return MessageResource()

        def create(self, **_kwargs):
            pytest.fail("reconciliation must never resend")

    class Client:
        messages = Messages()

    monkeypatch.setattr(
        message_delivery.receipt_db,
        "list_reconciliation_candidates",
        list_candidates,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "claim_reconciliation",
        claim_reconciliation,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "mark_reconciled",
        mark_reconciled,
    )
    monkeypatch.setattr(message_delivery, "Client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(message_delivery, "RECONCILIATION_FETCH_TIMEOUT_SECONDS", 0.001)
    caplog.set_level(logging.ERROR, logger="app.services.message_delivery")

    reconciled = await message_delivery.reconcile_pending_receipts_once()

    assert reconciled == 0
    assert marks == [("receipt-test", "lease-test")]
    assert "exception_type=TimeoutError" in caplog.text
    assert MESSAGE_SID not in caplog.text


@pytest.mark.asyncio
async def test_terminal_projection_failure_remains_repairable(monkeypatch):
    projection_results = iter([False, True])
    projected = []

    async def record_provider_update(*_args, **_kwargs):
        return receipt_db.ReceiptUpdate(
            "updated",
            {
                "receipt_id": "receipt-test",
                "call_sid": "CA_test",
                "effect": "owner_sms",
                "status": "delivered",
                "provider_status": "delivered",
            },
        )

    async def project_receipt_to_call(receipt_id):
        projected.append(receipt_id)
        return next(projection_results)

    async def list_pending_projection_ids(**_kwargs):
        return ["receipt-test"]

    monkeypatch.setattr(
        message_delivery.receipt_db,
        "record_provider_update",
        record_provider_update,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "project_receipt_to_call",
        project_receipt_to_call,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "list_pending_projection_ids",
        list_pending_projection_ids,
    )

    callback_outcome = await message_delivery.handle_provider_status(
        receipt_id="receipt-test",
        provider_message_sid=MESSAGE_SID,
        provider_status="delivered",
    )
    repaired = await message_delivery.repair_pending_call_projections_once()

    assert callback_outcome == "error"
    assert repaired == 1
    assert projected == ["receipt-test", "receipt-test"]


@pytest.mark.asyncio
async def test_failed_projection_is_deferred_so_backlog_can_advance(monkeypatch):
    deferred = []

    async def list_pending_projection_ids(**_kwargs):
        return ["receipt-test"]

    async def project_receipt_to_call(_receipt_id):
        return False

    async def defer_projection(receipt_id):
        deferred.append(receipt_id)
        return True

    monkeypatch.setattr(
        message_delivery.receipt_db,
        "list_pending_projection_ids",
        list_pending_projection_ids,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "project_receipt_to_call",
        project_receipt_to_call,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "defer_projection",
        defer_projection,
    )

    repaired = await message_delivery.repair_pending_call_projections_once()

    assert repaired == 0
    assert deferred == ["receipt-test"]


@pytest.mark.asyncio
async def test_callback_retries_call_mirror_after_receipt_update(monkeypatch):
    updates = iter(
        [
            receipt_db.ReceiptUpdate(
                "updated",
                {
                    "receipt_id": "receipt-test",
                    "call_sid": "CA_test",
                    "effect": "owner_sms",
                    "status": "delivered",
                    "provider_status": "delivered",
                    "failure_code": "",
                    "updated_at": 20.0,
                },
            ),
            receipt_db.ReceiptUpdate(
                "ignored",
                {
                    "receipt_id": "receipt-test",
                    "call_sid": "CA_test",
                    "effect": "owner_sms",
                    "status": "delivered",
                    "provider_status": "delivered",
                    "failure_code": "",
                    "updated_at": 20.0,
                },
            ),
        ]
    )
    projection_results = iter([False, True])

    async def record_provider_update(*_args, **_kwargs):
        return next(updates)

    async def project_receipt_to_call(receipt_id):
        assert receipt_id == "receipt-test"
        return next(projection_results)

    monkeypatch.setattr(
        message_delivery.receipt_db,
        "record_provider_update",
        record_provider_update,
    )
    monkeypatch.setattr(
        message_delivery.receipt_db,
        "project_receipt_to_call",
        project_receipt_to_call,
    )

    first = await message_delivery.handle_provider_status(
        receipt_id="receipt-test",
        provider_message_sid=MESSAGE_SID,
        provider_status="delivered",
    )
    second = await message_delivery.handle_provider_status(
        receipt_id="receipt-test",
        provider_message_sid=MESSAGE_SID,
        provider_status="delivered",
    )

    assert first == "error"
    assert second == "ignored"


@pytest.mark.asyncio
async def test_post_call_operations_iteration_runs_handoffs_and_reconciliation(
    monkeypatch,
):
    events = []

    async def run_pending_post_calls_once():
        events.append("handoffs")

    async def reconcile_pending_receipts_once():
        events.append("receipts")
        return 1

    async def repair_pending_call_projections_once():
        events.append("projections")
        return 1

    monkeypatch.setattr(
        post_call_handoff,
        "run_pending_post_calls_once",
        run_pending_post_calls_once,
    )
    monkeypatch.setattr(
        post_call_handoff,
        "reconcile_pending_receipts_once",
        reconcile_pending_receipts_once,
    )
    monkeypatch.setattr(
        post_call_handoff,
        "repair_pending_call_projections_once",
        repair_pending_call_projections_once,
    )

    await post_call_handoff.run_post_call_operations_once()

    assert events == ["handoffs", "receipts", "projections"]


@pytest.mark.asyncio
async def test_post_call_operations_reconciles_when_handoff_scan_fails(monkeypatch):
    events = []

    async def run_pending_post_calls_once():
        events.append("handoffs")
        raise RuntimeError("private handoff storage failure")

    async def reconcile_pending_receipts_once():
        events.append("receipts")
        return 1

    async def repair_pending_call_projections_once():
        events.append("projections")
        return 1

    monkeypatch.setattr(
        post_call_handoff,
        "run_pending_post_calls_once",
        run_pending_post_calls_once,
    )
    monkeypatch.setattr(
        post_call_handoff,
        "reconcile_pending_receipts_once",
        reconcile_pending_receipts_once,
    )
    monkeypatch.setattr(
        post_call_handoff,
        "repair_pending_call_projections_once",
        repair_pending_call_projections_once,
    )

    await post_call_handoff.run_post_call_operations_once()

    assert events == ["handoffs", "receipts", "projections"]


@pytest.mark.asyncio
async def test_receipt_reconciliation_is_not_blocked_by_slow_handoffs(monkeypatch):
    receipt_started = asyncio.Event()

    async def run_pending_post_calls_once():
        await receipt_started.wait()

    async def reconcile_pending_receipts_once():
        receipt_started.set()
        return 1

    async def repair_pending_call_projections_once():
        return 0

    monkeypatch.setattr(
        post_call_handoff,
        "run_pending_post_calls_once",
        run_pending_post_calls_once,
    )
    monkeypatch.setattr(
        post_call_handoff,
        "reconcile_pending_receipts_once",
        reconcile_pending_receipts_once,
    )
    monkeypatch.setattr(
        post_call_handoff,
        "repair_pending_call_projections_once",
        repair_pending_call_projections_once,
    )

    await asyncio.wait_for(
        post_call_handoff.run_post_call_operations_once(),
        timeout=0.1,
    )

    assert receipt_started.is_set()
