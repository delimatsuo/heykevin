"""Admin API regression tests."""

import os
from types import SimpleNamespace

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest
from pydantic import ValidationError

from app.api import admin as admin_api

# Import before the autouse fixture swaps out firestore.transactional: this
# module binds `transactional` at import time, so a first import inside a test
# here would keep the stand-in and break later tests in the same process.
from app.services import integration_token_mutations  # noqa: F401


class _FakeDoc:
    def __init__(self, doc_id: str, data: dict):
        import datetime
        self.read_time = datetime.datetime.now(datetime.UTC)
        self.id = doc_id
        self._data = data
        self.exists = True

    def to_dict(self):
        return dict(self._data)


class _MissingDoc:
    exists = False
    id = ""

    def to_dict(self):
        return {}


class _FakeDocumentRef:
    def __init__(self, doc: _FakeDoc | None, doc_id="missing"):
        self.id = doc.id if doc is not None else doc_id
        self._doc = doc
        self._subcollections = {}

    def with_subcollections(self, subcollections: dict[str, list[_FakeDoc]]):
        self._subcollections = subcollections
        return self

    def get(self, transaction=None):
        if self._doc is not None:
            return self._doc
        import datetime
        missing = _MissingDoc()
        missing.id = self.id
        missing.read_time = datetime.datetime.now(datetime.UTC)
        return missing

    def update(self, updates: dict):
        if self._doc is None:
            raise RuntimeError("missing document")
        self._doc._data.update(updates)

    def collection(self, name: str):
        return _FakeCollection(self._subcollections.get(name, []))


class _FakeQuery:
    def __init__(self, docs: list[_FakeDoc]):
        self._docs = docs

    def where(self, field_path, op_string=None, value=None, **_kwargs):
        if field_path == "active" and op_string == "==" and value is True:
            return _FakeQuery([doc for doc in self._docs if doc.to_dict().get("active") is True])
        return self

    def order_by(self, *_args, **_kwargs):
        raise RuntimeError("Firestore missing composite index")

    def limit(self, value: int):
        return _FakeQuery(self._docs[:value])

    def stream(self):
        return list(self._docs)


class _FakeCollection:
    def __init__(self, docs: list[_FakeDoc], subcollections: dict[str, dict[str, list[_FakeDoc]]] | None = None):
        self._docs = docs
        self._subcollections = subcollections or {}

    def document(self, doc_id: str):
        for doc in self._docs:
            if doc.id == doc_id:
                return _FakeDocumentRef(doc).with_subcollections(self._subcollections.get(doc_id, {}))
        return _FakeDocumentRef(None, doc_id)

    def where(self, *args, **kwargs):
        return _FakeQuery(self._docs).where(*args, **kwargs)

    def stream(self):
        return list(self._docs)


class _FakeTransaction:
    def update(self, ref, updates):
        ref.update(updates)


class _FakeFirestore:
    def __init__(
        self,
        docs: list[_FakeDoc],
        subcollections: dict[str, dict[str, list[_FakeDoc]]] | None = None,
        collections: dict[str, list[_FakeDoc]] | None = None,
    ):
        self._docs = docs
        self._subcollections = subcollections or {}
        self._collections = {
            "contractors": _FakeCollection(self._docs, self._subcollections),
            "calls": _FakeCollection([]),
        }
        for name, collection_docs in (collections or {}).items():
            self._collections[name] = _FakeCollection(collection_docs)

    def jobber_transaction_factory(self, *, deadline, read_only):
        assert read_only is True or read_only is False
        class ReadTransaction:
            jobber_deadline = deadline
            id = b"admin-read-only"
            def _begin(self, retry_id=None): deadline.timeout()
            def _commit(self): deadline.timeout()
            def _rollback(self): pass
            def get(self, ref, *, retry, timeout):
                assert retry is None and 0 < timeout <= 2
                return iter([ref.get()])
        return ReadTransaction()

    def collection(self, name: str):
        return self._collections.setdefault(name, _FakeCollection([]))

    def transaction(self):
        return _FakeTransaction()


@pytest.fixture(autouse=True)
def _immediate_admin_transactions(monkeypatch):
    monkeypatch.setattr(admin_api.firestore, "transactional", lambda fn: fn)


def _admin_request():
    return SimpleNamespace(
        state=SimpleNamespace(is_admin=True),
        headers={"user-agent": "pytest"},
        client=SimpleNamespace(host="127.0.0.1"),
    )


@pytest.mark.asyncio
async def test_admin_contractors_does_not_require_created_at_composite_index(monkeypatch):
    fake_db = _FakeFirestore([
        _FakeDoc("old-active", {
            "active": True,
            "owner_name": "Old Active",
            "created_at": 100,
            "subscription_status": "trial",
        }),
        _FakeDoc("inactive", {
            "active": False,
            "owner_name": "Inactive",
            "created_at": 300,
        }),
        _FakeDoc("new-active", {
            "active": True,
            "owner_name": "New Active",
            "created_at": 200,
            "subscription_status": "active",
        }),
    ])
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)

    response = await admin_api.admin_list_contractors(_admin_request())

    assert response["count"] == 2
    assert [item["contractor_id"] for item in response["contractors"]] == [
        "new-active",
        "old-active",
    ]


@pytest.mark.asyncio
async def test_admin_contractors_filters_by_subscription_status(monkeypatch):
    fake_db = _FakeFirestore([
        _FakeDoc("trial-user", {"active": True, "subscription_status": "trial", "created_at": 2}),
        _FakeDoc("paid-user", {"active": True, "subscription_status": "active", "created_at": 1}),
    ])
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)

    response = await admin_api.admin_list_contractors(_admin_request(), status="trial")

    assert [item["contractor_id"] for item in response["contractors"]] == ["trial-user"]


@pytest.mark.asyncio
async def test_admin_contractor_detail_masks_secrets_and_summarizes_device(monkeypatch):
    fake_db = _FakeFirestore(
        [
            _FakeDoc("contractor-1", {
                "active": True,
                "owner_name": "A Contractor",
                "contractor_api_token": "secret-token",
                "api_token_hash": "secret-hash",
                "twilio_number": "+15550001111",
            }),
        ],
        subcollections={
            "contractor-1": {
                "devices": [
                    _FakeDoc("primary", {
                        "push_token": "push-token",
                        "voip_token": "voip-token",
                        "updated_at": 123,
                    }),
                ],
            },
        },
    )
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)

    response = await admin_api.admin_get_contractor_detail("contractor-1", _admin_request())

    contractor = response["contractor"]
    assert contractor["contractor_id"] == "contractor-1"
    assert "contractor_api_token" not in contractor
    assert "api_token_hash" not in contractor
    assert response["device"] == {
        "push_token_present": True,
        "voip_token_present": True,
        "updated_at": 123,
    }
    assert response["diagnostics"] == [{"severity": "info", "code": "no_recent_calls"}]


@pytest.mark.asyncio
async def test_admin_detail_exposes_safe_number_release_reconciliation_summary(monkeypatch):
    fake_db = _FakeFirestore([
        _FakeDoc("contractor-1", {
            "active": True,
            "number_release_claim": {
                "state": "needs_attention",
                "nonce": "private-nonce",
                "expected_twilio_number": "+15550001111",
                "claimed_at": 10,
                "deleting_at": 11,
                "held_at": 12,
            },
            "number_release_conflict": {
                "at": 13,
                "phase": "deleting",
                "kind": "profile_mutation",
                "fields": ["subscription_status"],
                "deferred_updates": {
                    "subscription_status": "active",
                    "subscription_original_transaction_id": "private-receipt",
                },
                "deferred_subscription_events": [{
                    "at": 13,
                    "updates": {
                        "subscription_status": "active",
                        "subscription_original_transaction_id": "private-receipt",
                    },
                }],
            },
        }),
    ])
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)

    response = await admin_api.admin_get_contractor_detail("contractor-1", _admin_request())

    summary = response["contractor"]["number_release_reconciliation"]
    assert summary["state"] == "needs_attention"
    assert summary["deferred_updates"] == {"subscription_status": "active"}
    assert summary["deferred_subscription_events"] == [{
        "at": 13,
        "updates": {"subscription_status": "active"},
    }]
    assert "number_release_claim" not in response["contractor"]
    assert "number_release_conflict" not in response["contractor"]
    assert "private-nonce" not in repr(summary)
    assert "private-receipt" not in repr(summary)


@pytest.mark.asyncio
async def test_admin_contractor_detail_includes_jobber_status_without_tokens(monkeypatch):
    fake_db = _FakeFirestore(
        [
            _FakeDoc("contractor-1", {
                "active": True,
                "owner_name": "A Contractor",
                "jobber_access_token": "jobber-access-secret",
                "jobber_refresh_token": "jobber-refresh-secret",
                "jobber_connected_at": 123.0,
                "jobber_lead_capture_enabled": True,
                "jobber_lead_capture_updated_at": 456.0,
            }),
        ],
    )
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)

    response = await admin_api.admin_get_contractor_detail("contractor-1", _admin_request())

    contractor = response["contractor"]
    assert "jobber_access_token" not in contractor
    assert "jobber_refresh_token" not in contractor
    assert response["jobber"] == {
        "connected": True,
        "connected_at": 123.0,
        "lead_capture_enabled": True,
        "lead_capture_updated_at": 456.0,
        "connection_status": "connected",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("stamp,expected", [(456, 456.0), (True, None), (float("nan"), None),
    (float("inf"), None), ("456", None), (0, None), (-1, None), (None, None)])
async def test_admin_jobber_capture_history_uses_safe_authoritative_timestamp(monkeypatch, paused, stamp, expected):
    contractor = {"active": True, "jobber_access_token": "fictional-access",
        "jobber_refresh_token": "fictional-refresh", "jobber_connected_at": 123.0,
        "jobber_lead_capture_enabled": True, "jobber_lead_capture_updated_at": stamp}
    if paused:
        contractor.update(jobber_refresh_outcome_unknown=True, jobber_reauthorization_required=True)
    db = _FakeFirestore([_FakeDoc("contractor-1", contractor)])
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: db)
    response = await admin_api.admin_get_contractor_detail("contractor-1", _admin_request())
    assert response["jobber"] == {
        "connection_status": "reauthorization_required" if paused else "connected",
        "connected": not paused, "lead_capture_enabled": not paused,
        "connected_at": None if paused else 123.0, "lead_capture_updated_at": expected}
    assert "jobber_access_token" not in response["contractor"]
    assert "jobber_refresh_token" not in response["contractor"]


@pytest.mark.asyncio
async def test_admin_list_calls_redacts_phone_and_hides_transcript(monkeypatch):
    fake_db = _FakeFirestore(
        [],
        collections={
            "calls": [
                _FakeDoc("old-call", {
                    "call_sid": "old-call",
                    "contractor_id": "contractor-1",
                    "timestamp": 100,
                    "caller_phone": "+15550001111",
                    "caller_name": "Old Caller",
                    "outcome": "ignored",
                    "route_taken": "ai_screening",
                    "duration_seconds": 42,
                    "summary": "private summary",
                    "transcript": "Caller: private",
                }),
                _FakeDoc("new-call", {
                    "call_sid": "new-call",
                    "contractor_id": "contractor-2",
                    "timestamp": 200,
                    "caller_phone": "+15550002222",
                    "caller_name": "New Caller",
                    "outcome": "picked_up",
                    "route": "whitelist_forward",
                    "duration_seconds": 7,
                }),
            ],
        },
    )
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)

    response = await admin_api.admin_list_calls(_admin_request())

    assert [item["call_sid"] for item in response["calls"]] == ["new-call", "old-call"]
    old_call = response["calls"][1]
    assert old_call["caller_phone"] == "***1111"
    assert old_call["has_summary"] is True
    assert old_call["has_transcript"] is True
    assert "summary" not in old_call
    assert "transcript" not in old_call


@pytest.mark.asyncio
async def test_admin_call_items_include_jobber_sync_metadata(monkeypatch):
    fake_db = _FakeFirestore(
        [],
        collections={
            "calls": [
                _FakeDoc("jobber-call", {
                    "call_sid": "jobber-call",
                    "contractor_id": "contractor-1",
                    "timestamp": 100,
                    "caller_phone": "+15550001111",
                    "jobber_sync_status": "succeeded",
                    "jobber_request_id": "request-1",
                    "jobber_request_url": "https://secure.getjobber.com/work_requests/31433685",
                    "jobber_synced_at": 123.0,
                    "jobber_sync_error": "ignored-after-success",
                }),
            ],
        },
    )
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)

    response = await admin_api.admin_list_calls(_admin_request())

    call = response["calls"][0]
    assert call["jobber_sync_status"] == "succeeded"
    assert call["jobber_request_id"] == "request-1"
    assert call["jobber_request_url"] == "https://secure.getjobber.com/work_requests/31433685"
    assert call["jobber_synced_at"] == 123.0
    assert call["jobber_sync_error"] == "ignored-after-success"


@pytest.mark.asyncio
async def test_admin_list_calls_uses_job_card_for_existing_call_summary(monkeypatch):
    fake_db = _FakeFirestore(
        [],
        collections={
            "calls": [
                _FakeDoc("call-with-job", {
                    "call_sid": "call-with-job",
                    "contractor_id": "contractor-1",
                    "timestamp": 100,
                    "caller_phone": "+15550001111",
                    "route_taken": "ai_screening",
                }),
            ],
            "jobs": [
                _FakeDoc("job-1", {
                    "call_sid": "call-with-job",
                    "call_type": "service_request",
                    "issue_description": "Caller needs toilet replacement pricing",
                    "urgency": "quote",
                }),
            ],
        },
    )
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)

    response = await admin_api.admin_list_calls(_admin_request())

    call = response["calls"][0]
    assert call["outcome"] == "service_request"
    assert call["has_summary"] is True
    assert "summary" not in call


@pytest.mark.asyncio
async def test_admin_list_contractor_calls_filters_by_contractor(monkeypatch):
    fake_db = _FakeFirestore(
        [],
        collections={
            "calls": [
                _FakeDoc("contractor-call", {
                    "contractor_id": "contractor-1",
                    "timestamp": 200,
                    "caller_phone": "+15550001111",
                }),
                _FakeDoc("other-call", {
                    "contractor_id": "contractor-2",
                    "timestamp": 300,
                    "caller_phone": "+15550002222",
                }),
            ],
        },
    )
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)

    response = await admin_api.admin_list_contractor_calls("contractor-1", _admin_request())

    assert [item["call_sid"] for item in response["calls"]] == ["contractor-call"]


@pytest.mark.asyncio
async def test_admin_number_inventory_reconciles_firestore_and_twilio(monkeypatch):
    fake_db = _FakeFirestore([
        _FakeDoc("has-number", {
            "active": True,
            "twilio_number": "+15550001111",
            "created_at": 1,
        }),
        _FakeDoc("missing-number", {
            "active": True,
            "twilio_number": "",
            "created_at": 2,
        }),
    ])
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(admin_api, "fetch_twilio_incoming_numbers", lambda: [
        {"phone_number": "+15550001111", "sid": "PN1"},
        {"phone_number": "+15550002222", "sid": "PN2"},
    ])

    response = await admin_api.admin_number_inventory(_admin_request())

    assert response["summary"]["assigned_numbers"] == 1
    assert response["summary"]["contractors_missing_numbers"] == 1
    assert response["summary"]["orphan_numbers"] == 1


@pytest.mark.asyncio
async def test_admin_audit_events_endpoint_returns_events(monkeypatch):
    async def _list_events(limit=100):
        return [{"id": "audit-1", "action": "extend_trial"}]

    monkeypatch.setattr(admin_api, "list_admin_audit_events", _list_events)

    response = await admin_api.admin_audit_events(_admin_request())

    assert response == {"events": [{"id": "audit-1", "action": "extend_trial"}]}


def test_extend_trial_requires_reason():
    with pytest.raises(ValidationError):
        admin_api.ExtendTrialRequest(days=7, reason="")


def test_revoke_requires_reason():
    with pytest.raises(ValidationError):
        admin_api.RevokeContractorRequest(reason="")


def test_test_account_mark_requires_reason():
    with pytest.raises(ValidationError):
        admin_api.SetTestAccountRequest(enabled=True, reason="")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_test_account_mark_is_written_and_audited(monkeypatch, enabled):
    fake_db = _FakeFirestore([
        _FakeDoc("contractor-1", {
            "active": True,
            "subscription_status": "active",
            "subscription_expires": 100,
            "subscription_test_account": not enabled,
        }),
    ])
    audit_events = []

    async def _write_audit_event(**kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(admin_api, "write_admin_audit_event", _write_audit_event)

    response = await admin_api.admin_set_test_account(
        "contractor-1",
        admin_api.SetTestAccountRequest(enabled=enabled, reason="owner's internal test account"),
        _admin_request(),
    )

    doc = fake_db.collection("contractors").document("contractor-1").get().to_dict()
    assert response == {"status": "ok", "subscription_test_account": enabled}
    assert doc["subscription_test_account"] is enabled
    # Nothing else about the subscription moves.
    assert doc["subscription_status"] == "active" and doc["subscription_expires"] == 100
    assert audit_events[0]["action"] == "set_test_account"
    assert audit_events[0]["target_id"] == "contractor-1"
    assert audit_events[0]["reason"] == "owner's internal test account"
    assert audit_events[0]["before"] == {"subscription_test_account": not enabled}
    assert audit_events[0]["after"] == {"subscription_test_account": enabled}


@pytest.mark.asyncio
async def test_only_the_global_admin_can_set_the_test_account_mark(monkeypatch):
    from fastapi import HTTPException

    fake_db = _FakeFirestore([_FakeDoc("contractor-1", {"active": True})])
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)
    tenant_request = SimpleNamespace(
        state=SimpleNamespace(is_admin=False, contractor_id="contractor-1"),
        headers={"user-agent": "pytest"},
        client=SimpleNamespace(host="127.0.0.1"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await admin_api.admin_set_test_account(
            "contractor-1",
            admin_api.SetTestAccountRequest(enabled=True, reason="exempt myself"),
            tenant_request,
        )

    assert exc_info.value.status_code == 403
    assert "subscription_test_account" not in fake_db.collection("contractors").document("contractor-1").get().to_dict()


@pytest.mark.asyncio
async def test_test_account_mark_on_a_missing_account_is_404(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: _FakeFirestore([]))

    with pytest.raises(HTTPException) as exc_info:
        await admin_api.admin_set_test_account(
            "missing",
            admin_api.SetTestAccountRequest(enabled=True, reason="internal account"),
            _admin_request(),
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_test_account_mark_respects_a_number_release_in_progress(monkeypatch):
    from fastapi import HTTPException
    from app.db import contractors

    fake_db = _FakeFirestore([
        _FakeDoc("contractor-1", {
            "active": True,
            contractors.NUMBER_RELEASE_CLAIM_FIELD: {"state": contractors.NUMBER_RELEASE_CLAIM_DELETING},
        }),
    ])
    audit_events = []

    async def _write_audit_event(**kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(admin_api, "write_admin_audit_event", _write_audit_event)

    with pytest.raises(HTTPException) as exc_info:
        await admin_api.admin_set_test_account(
            "contractor-1",
            admin_api.SetTestAccountRequest(enabled=True, reason="internal account"),
            _admin_request(),
        )

    doc = fake_db.collection("contractors").document("contractor-1").get().to_dict()
    assert exc_info.value.status_code == 409
    assert "subscription_test_account" not in doc
    assert audit_events == []


def test_contractor_list_shows_the_test_account_mark():
    marked = admin_api._contractor_list_item(_FakeDoc("c1", {"subscription_test_account": True}))
    unmarked = admin_api._contractor_list_item(_FakeDoc("c2", {"subscription_test_account": "yes"}))

    assert marked["subscription_test_account"] is True
    assert unmarked["subscription_test_account"] is False


@pytest.mark.asyncio
async def test_extend_trial_writes_audit_event(monkeypatch):
    fake_db = _FakeFirestore([
        _FakeDoc("contractor-1", {
            "active": True,
            "subscription_status": "expired",
            "subscription_expires": 100,
        }),
    ])
    audit_events = []

    async def _write_audit_event(**kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(admin_api, "write_admin_audit_event", _write_audit_event)

    response = await admin_api.admin_extend_trial(
        "contractor-1",
        admin_api.ExtendTrialRequest(days=7, reason="customer support request"),
        _admin_request(),
    )

    doc = fake_db.collection("contractors").document("contractor-1").get()
    assert response["status"] == "ok"
    assert doc.to_dict()["subscription_status"] == "trial"
    assert audit_events[0]["action"] == "extend_trial"
    assert audit_events[0]["target_id"] == "contractor-1"
    assert audit_events[0]["reason"] == "customer support request"
    assert audit_events[0]["before"]["subscription_status"] == "expired"
    assert audit_events[0]["after"]["subscription_status"] == "trial"


@pytest.mark.asyncio
async def test_revoke_writes_audit_event(monkeypatch):
    fake_db = _FakeFirestore([
        _FakeDoc("contractor-1", {
            "active": True,
            "subscription_status": "trial",
            "subscription_expires": 200,
        }),
    ])
    audit_events = []

    async def _write_audit_event(**kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(admin_api, "write_admin_audit_event", _write_audit_event)

    response = await admin_api.admin_revoke_contractor(
        "contractor-1",
        admin_api.RevokeContractorRequest(reason="fraud report"),
        _admin_request(),
    )

    doc = fake_db.collection("contractors").document("contractor-1").get()
    assert response == {"status": "ok", "contractor_id": "contractor-1"}
    assert doc.to_dict()["subscription_status"] == "expired"
    assert audit_events[0]["action"] == "revoke_contractor"
    assert audit_events[0]["target_id"] == "contractor-1"
    assert audit_events[0]["reason"] == "fraud report"
    assert audit_events[0]["before"]["subscription_status"] == "trial"
    assert audit_events[0]["after"]["subscription_status"] == "expired"


class _UnformattableJobberError(Exception):
    def __str__(self):
        raise AssertionError("Status-reader exceptions must never be formatted")


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [TimeoutError, ConnectionError, _UnformattableJobberError])
async def test_admin_detail_preserves_support_context_when_jobber_status_unavailable(monkeypatch, caplog, error_type):
    import json
    from app.services import integration_token_mutations as mutations
    marker = "FICTIONAL_CREDENTIAL_MUST_NOT_LEAK"
    fake_db = _FakeFirestore(
        [_FakeDoc("contractor-1", {
            "active": True, "owner_name": "Fictional Owner", "business_name": "Fictional Business",
            "subscription_status": "expired", "contractor_api_token": marker, "api_token_hash": marker,
            "jobber_access_token": marker, "jobber_refresh_token": marker,
            "jobber_connected": True, "jobber_connected_at": 123.0,
            "jobber_lead_capture_enabled": True, "jobber_lead_capture_updated_at": 456.0,
        })],
        subcollections={"contractor-1": {"devices": [_FakeDoc("primary", {
            "push_token": marker, "voip_token": marker, "updated_at": 789,
        })]}},
        collections={"calls": [
            _FakeDoc("older", {"contractor_id": "contractor-1", "timestamp": 10,
                "caller_name": "Older caller", "summary": marker, "transcript": marker}),
            _FakeDoc("newer", {"contractor_id": "contractor-1", "timestamp": 20,
                "caller_name": "Newer caller", "outcome": "message", "duration": 42}),
            _FakeDoc("other", {"contractor_id": "contractor-2", "timestamp": 30}),
        ]},
    )
    status_reads = []
    async def unavailable(contractor_id, *, db):
        status_reads.append((contractor_id, db))
        raise error_type(marker)
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(mutations, "load_jobber_connection_status", unavailable)
    response = await admin_api.admin_get_contractor_detail("contractor-1", _admin_request())
    assert status_reads == [("contractor-1", fake_db)]
    assert set(response) == {"contractor", "device", "recent_calls", "diagnostics", "audit_events", "jobber"}
    assert response["contractor"]["contractor_id"] == "contractor-1"
    assert response["contractor"]["owner_name"] == "Fictional Owner"
    assert response["contractor"]["business_name"] == "Fictional Business"
    assert response["device"] == {"push_token_present": True, "voip_token_present": True, "updated_at": 789}
    assert response["recent_calls"] == [
        {"call_sid": "newer", "contractor_id": "contractor-1", "timestamp": 20,
         "caller_phone": "[REDACTED]", "caller_name": "Newer caller", "outcome": "message", "route": "",
         "duration_seconds": 42, "has_summary": False, "has_transcript": False},
        {"call_sid": "older", "contractor_id": "contractor-1", "timestamp": 10,
         "caller_phone": "[REDACTED]", "caller_name": "Older caller", "outcome": "", "route": "",
         "duration_seconds": 0, "has_summary": True, "has_transcript": True},
    ]
    assert response["diagnostics"] == [
        {"severity": "critical", "code": "missing_twilio_number"},
        {"severity": "warning", "code": "subscription_expired"},
    ]
    assert response["audit_events"] == []
    assert response["jobber"] == {"connected": False, "connection_status": "unavailable",
        "connected_at": None, "lead_capture_enabled": False, "lead_capture_updated_at": None}
    assert marker not in json.dumps(response) and marker not in caplog.text


@pytest.mark.asyncio
async def test_admin_detail_status_missing_contractor_keeps_404(monkeypatch):
    from fastapi import HTTPException
    from app.services import integration_token_mutations as mutations
    fake_db = _FakeFirestore([_FakeDoc("contractor-1", {"active": True})])
    status_reads = []
    async def missing(contractor_id, *, db):
        status_reads.append((contractor_id, db))
        raise mutations.IntegrationTokenContractorNotFound("FICTIONAL_PRIVATE_MISSING_REASON")
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(mutations, "load_jobber_connection_status", missing)
    with pytest.raises(HTTPException) as error:
        await admin_api.admin_get_contractor_detail("contractor-1", _admin_request())
    assert error.value.status_code == 404 and error.value.detail == "Contractor not found"
    assert status_reads == [("contractor-1", fake_db)]


@pytest.mark.asyncio
async def test_admin_detail_initial_missing_skips_status_read(monkeypatch):
    from fastapi import HTTPException
    from app.services import integration_token_mutations as mutations
    async def forbidden(*args, **kwargs): pytest.fail("Missing profile must skip Jobber read")
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: _FakeFirestore([]))
    monkeypatch.setattr(mutations, "load_jobber_connection_status", forbidden)
    with pytest.raises(HTTPException) as error:
        await admin_api.admin_get_contractor_detail("missing", _admin_request())
    assert error.value.status_code == 404 and error.value.detail == "Contractor not found"


@pytest.mark.asyncio
async def test_admin_detail_unauthorized_never_fetches_or_reads_status(monkeypatch):
    from fastapi import HTTPException
    from app.services import integration_token_mutations as mutations
    def no_database(): pytest.fail("Unauthorized request must not fetch database")
    async def no_status(*args, **kwargs): pytest.fail("Unauthorized request must not read Jobber")
    monkeypatch.setattr(admin_api, "get_firestore_client", no_database)
    monkeypatch.setattr(mutations, "load_jobber_connection_status", no_status)
    request = _admin_request(); request.state.is_admin = False
    with pytest.raises(HTTPException) as error:
        await admin_api.admin_get_contractor_detail("contractor-1", request)
    assert error.value.status_code == 403 and error.value.detail == "Admin access required"


@pytest.mark.asyncio
async def test_admin_detail_jobber_cancellation_propagates(monkeypatch):
    import asyncio
    from app.services import integration_token_mutations as mutations
    fake_db = _FakeFirestore([_FakeDoc("contractor-1", {"active": True})])
    status_reads = []
    async def cancelled(contractor_id, *, db):
        status_reads.append((contractor_id, db))
        raise asyncio.CancelledError()
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(mutations, "load_jobber_connection_status", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await admin_api.admin_get_contractor_detail("contractor-1", _admin_request())
    assert status_reads == [("contractor-1", fake_db)]


@pytest.mark.asyncio
async def test_admin_detail_initial_fetch_failure_still_propagates(monkeypatch):
    from app.services import integration_token_mutations as mutations
    failure = ConnectionError("fictional initial profile read failure")
    fake_db = _FakeFirestore([_FakeDoc("contractor-1", {"active": True})])
    def failed_get(self): raise failure
    async def no_status(*args, **kwargs): pytest.fail("Failed profile fetch must skip Jobber read")
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(_FakeDocumentRef, "get", failed_get)
    monkeypatch.setattr(mutations, "load_jobber_connection_status", no_status)
    with pytest.raises(ConnectionError) as error:
        await admin_api.admin_get_contractor_detail("contractor-1", _admin_request())
    assert error.value is failure
