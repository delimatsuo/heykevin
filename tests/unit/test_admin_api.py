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


class _FakeDoc:
    def __init__(self, doc_id: str, data: dict):
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
    def __init__(self, doc: _FakeDoc | None):
        self._doc = doc
        self._subcollections = {}

    def with_subcollections(self, subcollections: dict[str, list[_FakeDoc]]):
        self._subcollections = subcollections
        return self

    def get(self):
        return self._doc or _MissingDoc()

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
        return _FakeDocumentRef(None)

    def where(self, *args, **kwargs):
        return _FakeQuery(self._docs).where(*args, **kwargs)

    def stream(self):
        return list(self._docs)


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

    def collection(self, name: str):
        return self._collections[name]


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
    }


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
async def test_admin_call_items_accept_summary_present_flag(monkeypatch):
    fake_db = _FakeFirestore(
        [],
        collections={
            "calls": [
                _FakeDoc("summary-call", {
                    "call_sid": "summary-call",
                    "contractor_id": "contractor-1",
                    "timestamp": 100,
                    "caller_phone": "+15550001111",
                    "summary_present": True,
                }),
            ],
        },
    )
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)

    response = await admin_api.admin_list_calls(_admin_request())

    call = response["calls"][0]
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
