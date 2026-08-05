"""Operational visibility and audited resolution for post-call handoffs."""

import logging
import os
from types import SimpleNamespace
from typing import get_args

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.api import admin as admin_api
from app.db import post_call_handoffs as handoff_db
from app.services import post_call, post_call_handoff


def _admin_request():
    return SimpleNamespace(
        state=SimpleNamespace(is_admin=True),
        headers={"user-agent": "pytest"},
        client=SimpleNamespace(host="test-client"),
    )


def test_acknowledged_handoffs_are_terminal():
    claimed, updates = handoff_db._claim_transition(
        {"status": "acknowledged", "attempts": 1},
        now=100.0,
        lease_seconds=60,
    )

    assert claimed is False
    assert updates == {}


def test_admin_and_storage_status_enums_do_not_drift():
    assert set(get_args(admin_api.PostCallHandoffStatus)) == set(
        handoff_db.VALID_HANDOFF_STATUSES
    )
    assert set(get_args(admin_api.PostCallResolution)) == set(
        handoff_db.ACKNOWLEDGEMENT_RESOLUTIONS
    )


def test_acknowledgement_only_transitions_attention_records():
    accepted, updates = handoff_db._acknowledge_transition(
        {"status": "needs_attention"},
        resolution="customer_contacted_manually",
        now=100.0,
    )

    assert accepted is True
    assert updates["status"] == "acknowledged"
    assert updates["resolution"] == "customer_contacted_manually"
    assert updates["acknowledged_at"] == 100.0
    assert updates["expires_at"].tzinfo is not None

    for status in ("pending", "in_progress", "completed", "acknowledged"):
        assert handoff_db._acknowledge_transition(
            {"status": status},
            resolution="no_action_required",
            now=100.0,
        ) == (False, {})


def test_handoff_summary_is_allowlisted_and_payload_free():
    summary = handoff_db.safe_handoff_summary(
        "CA_test",
        {
            "status": "needs_attention",
            "contractor_id": "contractor-test",
            "failure_code": "partial_delivery",
            "attempts": 1,
            "created_at": 10.0,
            "finished_at": 20.0,
            "completed_effects": ["call_record", "owner_sms", "private-effect"],
            "failed_effects": ["summary_push", "private-effect"],
            "transcript": "private transcript",
            "caller_phone": "private-caller-number",
            "caller_language": "private-language",
            "provider_response": "private provider payload",
        },
    )

    assert summary == {
        "call_sid": "CA_test",
        "contractor_id": "contractor-test",
        "status": "needs_attention",
        "failure_code": "partial_delivery",
        "attempts": 1,
        "created_at": 10.0,
        "started_at": None,
        "finished_at": 20.0,
        "acknowledged_at": None,
        "resolution": "",
        "completed_effects": ["call_record", "owner_sms"],
        "failed_effects": ["summary_push"],
    }
    rendered = repr(summary)
    assert "private" not in rendered
    assert "transcript" not in rendered
    assert "caller_phone" not in rendered


def test_handoff_summary_tolerates_malformed_storage_values():
    summary = handoff_db.safe_handoff_summary(
        "invalid call/id",
        {
            "contractor_id": "invalid contractor/id",
            "status": ["needs_attention"],
            "failure_code": {"private": "value"},
            "attempts": True,
            "completed_effects": "owner_sms",
        },
    )

    assert summary["call_sid"] == "invalid_call_id"
    assert summary["contractor_id"] == "invalid_contractor_id"
    assert summary["status"] == "unknown"
    assert summary["failure_code"] == "unknown"
    assert summary["attempts"] == 0
    assert summary["completed_effects"] == []


def test_only_completed_or_acknowledged_records_receive_retention_deadline():
    complete = post_call.PostCallResult(
        status="complete",
        completed_effects=("call_record",),
        failed_effects=(),
    )
    partial = post_call.PostCallResult(
        status="partial",
        completed_effects=("call_record",),
        failed_effects=("owner_sms",),
    )

    complete_updates = handoff_db._finish_updates(complete, now=100.0)
    partial_updates = handoff_db._finish_updates(partial, now=100.0)

    assert complete_updates["status"] == "completed"
    assert complete_updates["expires_at"].tzinfo is not None
    assert partial_updates["status"] == "needs_attention"
    assert "expires_at" not in partial_updates


@pytest.mark.asyncio
async def test_list_handoffs_queries_and_sorts_safe_summaries(monkeypatch):
    class Document:
        def __init__(self, doc_id, data):
            self.id = doc_id
            self._data = data

        def to_dict(self):
            return dict(self._data)

    class Query:
        def __init__(self, docs):
            self.docs = docs
            self.requested_limit = None

        def where(self, *, filter):
            assert filter is not None
            return self

        def limit(self, value):
            self.requested_limit = value
            return self

        def stream(self):
            assert self.requested_limit == 2
            return self.docs[: self.requested_limit]

    query = Query(
        [
            Document(
                "CA_old",
                {
                    "status": "needs_attention",
                    "finished_at": 10.0,
                    "transcript": "private transcript",
                },
            ),
            Document(
                "CA_new",
                {
                    "status": "needs_attention",
                    "finished_at": 20.0,
                    "caller_phone": "private-caller-number",
                },
            ),
        ]
    )

    class Firestore:
        def collection(self, name):
            assert name == handoff_db.COLLECTION
            return query

    monkeypatch.setattr(handoff_db, "get_firestore_client", Firestore)

    records = await handoff_db.list_handoffs("needs_attention", limit=2)

    assert [record["call_sid"] for record in records] == ["CA_new", "CA_old"]
    assert "private" not in repr(records)


@pytest.mark.asyncio
async def test_admin_lists_payload_free_attention_records(monkeypatch):
    records = [
        {
            "call_sid": "CA_test",
            "contractor_id": "contractor-test",
            "status": "needs_attention",
            "failure_code": "processing_timeout",
            "attempts": 1,
            "completed_effects": ["call_record"],
            "failed_effects": ["owner_sms"],
        }
    ]

    async def list_handoffs(status, *, limit):
        assert status == "needs_attention"
        assert limit == 25
        return records

    monkeypatch.setattr(admin_api.handoff_db, "list_handoffs", list_handoffs)

    response = await admin_api.admin_list_post_call_handoffs(
        _admin_request(),
        status="needs_attention",
        limit=25,
    )

    assert response == {
        "status": "needs_attention",
        "count": 1,
        "limit": 25,
        "has_more": False,
        "handoffs": records,
    }


@pytest.mark.asyncio
async def test_admin_handoff_queue_rejects_non_admin_before_read(monkeypatch):
    async def unexpected_read(*_args, **_kwargs):
        pytest.fail("non-admin request must not query post-call operations")

    monkeypatch.setattr(admin_api.handoff_db, "list_handoffs", unexpected_read)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=False))

    with pytest.raises(HTTPException) as error:
        await admin_api.admin_list_post_call_handoffs(request)

    assert error.value.status_code == 403


def test_acknowledgement_resolution_is_closed_set():
    with pytest.raises(ValidationError):
        admin_api.AcknowledgePostCallHandoffRequest(resolution="retry_everything")


@pytest.mark.asyncio
async def test_admin_acknowledges_without_retry_and_writes_audit(monkeypatch):
    acknowledgements = []
    mirrored = []
    audits = []

    async def get_handoff(call_sid):
        assert call_sid == "CA_test"
        return {
            "status": "needs_attention",
            "failure_code": "partial_delivery",
            "completed_effects": ["call_record"],
            "failed_effects": ["owner_sms"],
        }

    async def acknowledge(call_sid, resolution):
        acknowledgements.append((call_sid, resolution))
        return True

    async def save_call(call_sid, updates):
        mirrored.append((call_sid, updates))
        return True

    async def write_audit(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(admin_api.handoff_db, "get_handoff", get_handoff)
    monkeypatch.setattr(admin_api.handoff_db, "acknowledge_handoff", acknowledge)
    monkeypatch.setattr(admin_api.call_db, "save_call", save_call)
    monkeypatch.setattr(admin_api, "write_admin_audit_event", write_audit)
    monkeypatch.setattr(admin_api.time, "time", lambda: 200.0)

    response = await admin_api.admin_acknowledge_post_call_handoff(
        "CA_test",
        admin_api.AcknowledgePostCallHandoffRequest(
            resolution="customer_contacted_manually"
        ),
        _admin_request(),
    )

    assert acknowledgements == [("CA_test", "customer_contacted_manually")]
    assert mirrored == [
        (
            "CA_test",
            {
                "post_call_status": "acknowledged",
                "post_call_failure_code": "partial_delivery",
                "post_call_completed_effects": ["call_record"],
                "post_call_failed_effects": ["owner_sms"],
                "post_call_resolution": "customer_contacted_manually",
                "post_call_acknowledged_at": 200.0,
            },
        )
    ]
    assert audits[0]["action"] == "acknowledge_post_call_handoff"
    assert audits[0]["reason"] == "customer_contacted_manually"
    assert audits[0]["before"] == {
        "status": "needs_attention",
        "failure_code": "partial_delivery",
    }
    assert audits[0]["after"] == {
        "status": "acknowledged",
        "resolution": "customer_contacted_manually",
    }
    assert response == {
        "status": "acknowledged",
        "call_sid": "CA_test",
        "resolution": "customer_contacted_manually",
        "call_status_mirrored": True,
    }


@pytest.mark.asyncio
async def test_admin_rejects_acknowledgement_of_non_attention_handoff(monkeypatch):
    async def get_handoff(_call_sid):
        return {"status": "completed"}

    monkeypatch.setattr(admin_api.handoff_db, "get_handoff", get_handoff)

    with pytest.raises(HTTPException) as error:
        await admin_api.admin_acknowledge_post_call_handoff(
            "CA_test",
            admin_api.AcknowledgePostCallHandoffRequest(
                resolution="no_action_required"
            ),
            _admin_request(),
        )

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_partial_delivery_emits_attention_level_telemetry(monkeypatch, caplog):
    async def claim(_call_sid):
        return True

    async def process(**_kwargs):
        return post_call.PostCallResult(
            status="partial",
            completed_effects=("call_record",),
            failed_effects=("owner_sms",),
        )

    async def finish(_call_sid, _result):
        return True

    async def save_call(_call_sid, _updates):
        return True

    monkeypatch.setattr(post_call_handoff.handoff_db, "claim_handoff", claim)
    monkeypatch.setattr(post_call_handoff.handoff_db, "finish_handoff", finish)
    monkeypatch.setattr(post_call_handoff, "process_post_call", process)
    monkeypatch.setattr(post_call_handoff.call_db, "save_call", save_call)
    caplog.set_level(logging.INFO, logger="app.services.post_call_handoff")

    status = await post_call_handoff.run_post_call_handoff(
        "CA_test",
        transcript_lines=["Caller: routine request"],
        contractor={"contractor_id": "contractor-test"},
    )

    assert status == "needs_attention"
    finished = [
        record
        for record in caplog.records
        if "event=finished" in record.getMessage()
    ]
    assert len(finished) == 1
    assert finished[0].levelno == logging.ERROR
