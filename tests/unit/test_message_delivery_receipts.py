"""Durable, payload-free Twilio message delivery receipt state."""

from datetime import datetime
import json
import os
from pathlib import Path

import pytest


os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.db import message_delivery_receipts as receipt_db


MESSAGE_SID = "SM" + ("a" * 32)
OTHER_MESSAGE_SID = "SM" + ("b" * 32)


def _pending_receipt(**overrides) -> dict:
    receipt = {
        "call_sid": "CA_test",
        "effect": "owner_sms",
        "channel": "sms",
        "status": "pending",
        "provider_status": "queued",
        "provider_message_sid": MESSAGE_SID,
        "provider_message_hash": receipt_db._provider_message_hash(MESSAGE_SID),
        "created_at": 10.0,
        "updated_at": 10.0,
        "next_reconcile_at": 20.0,
    }
    receipt.update(overrides)
    return receipt


def test_receipt_summary_is_allowlisted_and_payload_free():
    summary = receipt_db.safe_receipt_summary(
        "receipt_test",
        {
            **_pending_receipt(),
            "provider_error_code": 30001,
            "body": "private message body",
            "to": "private destination",
            "from": "private sender",
            "provider_response": "private provider response",
        },
    )

    assert summary == {
        "receipt_id": "receipt_test",
        "call_sid": "CA_test",
        "effect": "owner_sms",
        "channel": "sms",
        "status": "pending",
        "provider_status": "queued",
        "provider_error_code": 30001,
        "failure_code": "",
        "created_at": 10.0,
        "updated_at": 10.0,
        "next_reconcile_at": 20.0,
        "finished_at": None,
        "acknowledged_at": None,
        "last_reconciled_at": None,
        "call_projection_pending": False,
        "call_projection_version": 0,
        "call_projected_at": None,
        "call_projection_next_at": None,
        "call_projection_last_attempt_at": None,
        "resolution": "",
    }
    rendered = repr(summary)
    assert MESSAGE_SID not in rendered
    assert "private" not in rendered
    assert "body" not in rendered


def test_receipt_summary_fails_closed_on_malformed_storage_values():
    summary = receipt_db.safe_receipt_summary(
        "invalid receipt/id",
        {
            "call_sid": "invalid call/id",
            "effect": "private-effect",
            "channel": "email",
            "status": ["failed"],
            "provider_status": {"private": "value"},
            "provider_error_code": "private",
            "failure_code": "private-failure",
        },
    )

    assert summary["receipt_id"] == "invalid_receipt_id"
    assert summary["call_sid"] == "invalid_call_id"
    assert summary["effect"] == "unknown"
    assert summary["channel"] == "unknown"
    assert summary["status"] == "unknown"
    assert summary["provider_status"] == "unknown"
    assert summary["provider_error_code"] is None
    assert summary["failure_code"] == "unknown"


def test_queued_submission_records_provider_identity_without_exposing_hash():
    outcome, updates = receipt_db._provider_transition(
        _pending_receipt(
            provider_status="unknown",
            provider_message_sid="",
            provider_message_hash="",
        ),
        provider_status="queued",
        provider_message_sid=MESSAGE_SID,
        provider_error_code="",
        now=20.0,
    )

    assert outcome == "updated"
    assert updates["status"] == "pending"
    assert updates["provider_status"] == "queued"
    assert updates["provider_message_sid"] == MESSAGE_SID
    assert updates["provider_message_hash"] == receipt_db._provider_message_hash(
        MESSAGE_SID
    )
    assert updates["updated_at"] == 20.0


def test_out_of_order_nonterminal_status_is_ignored():
    outcome, updates = receipt_db._provider_transition(
        _pending_receipt(provider_status="sent"),
        provider_status="queued",
        provider_message_sid=MESSAGE_SID,
        provider_error_code="",
        now=20.0,
    )

    assert outcome == "ignored"
    assert updates == {}


def test_delivered_status_is_terminal_and_ttl_ready():
    outcome, updates = receipt_db._provider_transition(
        _pending_receipt(provider_status="sent"),
        provider_status="delivered",
        provider_message_sid=MESSAGE_SID,
        provider_error_code="",
        now=20.0,
    )

    assert outcome == "updated"
    assert updates["status"] == "delivered"
    assert updates["provider_status"] == "delivered"
    assert updates["finished_at"] == 20.0
    assert isinstance(updates["expires_at"], datetime)
    assert updates["expires_at"].tzinfo is not None
    assert updates["call_projection_pending"] is True
    assert updates["call_projection_version"] == 1


def test_undelivered_status_records_bounded_provider_error():
    outcome, updates = receipt_db._provider_transition(
        _pending_receipt(provider_status="sent"),
        provider_status="undelivered",
        provider_message_sid=MESSAGE_SID,
        provider_error_code="30001",
        now=20.0,
    )

    assert outcome == "updated"
    assert updates["status"] == "failed"
    assert updates["provider_status"] == "undelivered"
    assert updates["provider_error_code"] == 30001
    assert updates["failure_code"] == "provider_delivery_failed"
    assert isinstance(updates["expires_at"], datetime)


def test_duplicate_terminal_callback_is_idempotent():
    outcome, updates = receipt_db._provider_transition(
        _pending_receipt(status="delivered", provider_status="delivered"),
        provider_status="delivered",
        provider_message_sid=MESSAGE_SID,
        provider_error_code="",
        now=30.0,
    )

    assert outcome == "ignored"
    assert updates == {}


def test_conflicting_terminal_callback_fails_closed():
    outcome, updates = receipt_db._provider_transition(
        _pending_receipt(status="delivered", provider_status="delivered"),
        provider_status="undelivered",
        provider_message_sid=MESSAGE_SID,
        provider_error_code="30001",
        now=30.0,
    )

    assert outcome == "conflict"
    assert updates["status"] == "failed"
    assert updates["failure_code"] == "conflicting_terminal_status"


def test_provider_message_identity_mismatch_is_rejected():
    outcome, updates = receipt_db._provider_transition(
        _pending_receipt(),
        provider_status="delivered",
        provider_message_sid=OTHER_MESSAGE_SID,
        provider_error_code="",
        now=20.0,
    )

    assert outcome == "invalid"
    assert updates == {}


def test_invalid_provider_status_and_identifier_are_rejected():
    for status, message_sid in (
        ("private-status", MESSAGE_SID),
        ("delivered", "invalid-provider-id"),
    ):
        outcome, updates = receipt_db._provider_transition(
            _pending_receipt(),
            provider_status=status,
            provider_message_sid=message_sid,
            provider_error_code="",
            now=20.0,
        )
        assert outcome == "invalid"
        assert updates == {}


def test_malformed_stored_status_fails_closed_without_raising():
    outcome, updates = receipt_db._provider_transition(
        _pending_receipt(status=["pending"]),
        provider_status="delivered",
        provider_message_sid=MESSAGE_SID,
        provider_error_code="",
        now=20.0,
    )

    assert outcome == "invalid"
    assert updates == {}


def test_acknowledged_receipt_ignores_late_callbacks():
    outcome, updates = receipt_db._provider_transition(
        _pending_receipt(status="acknowledged", provider_status="undelivered"),
        provider_status="delivered",
        provider_message_sid=MESSAGE_SID,
        provider_error_code="",
        now=30.0,
    )

    assert outcome == "ignored"
    assert updates == {}


def test_acknowledgement_only_resolves_failed_receipts():
    accepted, updates = receipt_db._acknowledge_transition(
        _pending_receipt(status="failed", failure_code="provider_delivery_failed"),
        resolution="customer_contacted_manually",
        now=40.0,
    )

    assert accepted is True
    assert updates["status"] == "acknowledged"
    assert updates["resolution"] == "customer_contacted_manually"
    assert updates["acknowledged_at"] == 40.0
    assert isinstance(updates["expires_at"], datetime)

    for status in ("pending", "delivered", "acknowledged"):
        assert receipt_db._acknowledge_transition(
            _pending_receipt(status=status),
            resolution="no_action_required",
            now=40.0,
        ) == (False, {})


def test_submission_failure_becomes_terminal_attention():
    accepted, updates = receipt_db._submission_failure_transition(
        _pending_receipt(),
        now=20.0,
    )

    assert accepted is True
    assert updates["status"] == "failed"
    assert updates["provider_status"] == "failed"
    assert updates["failure_code"] == "submission_failed"
    assert isinstance(updates["expires_at"], datetime)


def test_reconciliation_claim_advances_due_time_and_rejects_competitor():
    accepted, updates = receipt_db._reconciliation_claim_transition(
        _pending_receipt(next_reconcile_at=90.0),
        lease_token="lease-one",
        now=100.0,
    )

    assert accepted is True
    assert updates == {
        "reconcile_lease_token": "lease-one",
        "reconcile_lease_expires_at": (
            100.0 + receipt_db.RECONCILIATION_LEASE_SECONDS
        ),
        "next_reconcile_at": 100.0 + receipt_db.RECONCILIATION_LEASE_SECONDS,
        "updated_at": 100.0,
    }

    accepted, updates = receipt_db._reconciliation_claim_transition(
        {**_pending_receipt(), **updates},
        lease_token="lease-two",
        now=101.0,
    )

    assert accepted is False
    assert updates == {}


def test_reconciliation_completion_requires_matching_lease():
    claimed = {
        **_pending_receipt(),
        "reconcile_lease_token": "lease-one",
        "reconcile_lease_expires_at": 150.0,
    }

    assert receipt_db._reconciliation_completion_updates(
        claimed,
        lease_token="lease-two",
        now=110.0,
    ) is None
    assert receipt_db._reconciliation_completion_updates(
        claimed,
        lease_token="lease-one",
        now=110.0,
    ) == receipt_db._reconciliation_schedule(110.0)


def test_missing_provider_id_transition_rejects_newly_bound_message():
    assert receipt_db._missing_provider_id_transition(
        _pending_receipt(),
        now=20.0,
    ) == (False, {})


def test_projection_update_is_versioned_and_due_immediately():
    assert receipt_db._projection_updates(
        {"call_projection_version": 4},
        now=20.0,
    ) == {
        "call_projection_pending": True,
        "call_projection_version": 5,
        "call_projection_next_at": 20.0,
    }


def test_reconciliation_composite_index_is_versioned():
    config = json.loads(Path("firestore.indexes.json").read_text())
    receipt_indexes = [
        index
        for index in config["indexes"]
        if index["collectionGroup"] == receipt_db.COLLECTION
    ]

    assert receipt_indexes == [
        {
            "collectionGroup": "message_delivery_receipts",
            "queryScope": "COLLECTION",
            "fields": [
                {"fieldPath": "status", "order": "ASCENDING"},
                {"fieldPath": "created_at", "order": "ASCENDING"},
            ],
        },
        {
            "collectionGroup": "message_delivery_receipts",
            "queryScope": "COLLECTION",
            "fields": [
                {"fieldPath": "status", "order": "ASCENDING"},
                {"fieldPath": "next_reconcile_at", "order": "ASCENDING"},
            ],
        },
        {
            "collectionGroup": "message_delivery_receipts",
            "queryScope": "COLLECTION",
            "fields": [
                {"fieldPath": "call_projection_pending", "order": "ASCENDING"},
                {"fieldPath": "call_projection_next_at", "order": "ASCENDING"},
            ],
        },
    ]


@pytest.mark.asyncio
async def test_reconciliation_query_orders_by_due_time_without_page_starvation(
    monkeypatch,
):
    events = []

    class Document:
        id = "receipt-test"

        def to_dict(self):
            return {
                "provider_message_sid": MESSAGE_SID,
                "next_reconcile_at": 90.0,
                "last_reconciled_at": 99.0,
            }

    class Query:
        def where(self, *, filter):
            events.append(("where", filter))
            return self

        def order_by(self, field, *, direction):
            events.append(("order_by", field, direction))
            return self

        def limit(self, value):
            events.append(("limit", value))
            return self

        def stream(self):
            return [Document()]

    class Database:
        def collection(self, name):
            events.append(("collection", name))
            return Query()

    monkeypatch.setattr(receipt_db, "get_firestore_client", Database)
    monkeypatch.setattr(receipt_db, "FieldFilter", lambda *args: args)

    candidates = await receipt_db.list_reconciliation_candidates(now=100.0, limit=20)

    assert candidates == [
        receipt_db.ReconciliationCandidate(
            receipt_id="receipt-test",
            provider_message_sid=MESSAGE_SID,
        )
    ]
    assert events == [
        ("collection", receipt_db.COLLECTION),
        ("where", ("status", "==", "pending")),
        ("where", ("next_reconcile_at", "<=", 100.0)),
        ("order_by", "next_reconcile_at", receipt_db.firestore.Query.ASCENDING),
        ("limit", 20),
    ]


def test_reconciliation_schedule_advances_retry_window():
    assert receipt_db._reconciliation_schedule(100.0) == {
        "last_reconciled_at": 100.0,
        "next_reconcile_at": 100.0 + receipt_db.RECONCILIATION_INTERVAL_SECONDS,
        "reconcile_lease_token": "",
        "reconcile_lease_expires_at": 0.0,
        "updated_at": 100.0,
    }


@pytest.mark.asyncio
async def test_receipt_registration_schedules_first_reconciliation(monkeypatch):
    created = []

    class Document:
        def create(self, payload):
            created.append(payload)

    class Collection:
        def document(self, receipt_id):
            assert receipt_id == "receipt-test"
            return Document()

    class Database:
        def collection(self, name):
            assert name == receipt_db.COLLECTION
            return Collection()

    monkeypatch.setattr(receipt_db, "get_firestore_client", Database)
    monkeypatch.setattr(receipt_db.secrets, "token_urlsafe", lambda _size: "receipt-test")
    monkeypatch.setattr(receipt_db.time, "time", lambda: 100.0)

    receipt_id = await receipt_db.create_receipt(
        call_sid="CA_test",
        effect="owner_sms",
        channel="sms",
    )

    assert receipt_id == "receipt-test"
    assert created[0]["created_at"] == 100.0
    assert created[0]["next_reconcile_at"] == (
        100.0 + receipt_db.RECONCILIATION_DELAY_SECONDS
    )


@pytest.mark.asyncio
async def test_admin_receipt_query_returns_oldest_page(monkeypatch):
    events = []

    class Document:
        def __init__(self, receipt_id, created_at):
            self.id = receipt_id
            self.created_at = created_at

        def to_dict(self):
            return {
                "call_sid": "CA_test",
                "effect": "owner_sms",
                "channel": "sms",
                "status": "failed",
                "provider_status": "undelivered",
                "failure_code": "provider_delivery_failed",
                "created_at": self.created_at,
                "updated_at": self.created_at,
            }

    class Query:
        def where(self, *, filter):
            events.append(("where", filter))
            return self

        def order_by(self, field, *, direction):
            events.append(("order_by", field, direction))
            return self

        def limit(self, value):
            events.append(("limit", value))
            return self

        def stream(self):
            return [Document("oldest", 10.0), Document("newer", 20.0)]

    class Database:
        def collection(self, name):
            events.append(("collection", name))
            return Query()

    monkeypatch.setattr(receipt_db, "get_firestore_client", Database)
    monkeypatch.setattr(receipt_db, "FieldFilter", lambda *args: args)

    receipts = await receipt_db.list_receipts("failed", limit=2)

    assert [receipt["receipt_id"] for receipt in receipts] == ["oldest", "newer"]
    assert events == [
        ("collection", receipt_db.COLLECTION),
        ("where", ("status", "==", "failed")),
        ("order_by", "created_at", receipt_db.firestore.Query.ASCENDING),
        ("limit", 2),
    ]


@pytest.mark.asyncio
async def test_projection_updates_call_and_clears_pending_in_one_transaction(
    monkeypatch,
):
    transaction_events = []
    receipt_data = _pending_receipt(
        status="delivered",
        provider_status="delivered",
        call_projection_pending=True,
        call_projection_version=2,
        updated_at=30.0,
    )

    class Snapshot:
        exists = True

        def to_dict(self):
            return receipt_data

    class ReceiptDocument:
        def get(self, *, transaction):
            assert transaction is tx
            return Snapshot()

    class CallDocument:
        pass

    receipt_document = ReceiptDocument()
    call_document = CallDocument()

    class Transaction:
        def set(self, document, updates, *, merge):
            transaction_events.append(("set", document, updates, merge))

        def update(self, document, updates):
            transaction_events.append(("update", document, updates))

    tx = Transaction()

    class Collection:
        def __init__(self, name):
            self.name = name

        def document(self, document_id):
            if self.name == receipt_db.COLLECTION:
                assert document_id == "receipt-test"
                return receipt_document
            assert self.name == receipt_db.CALLS_COLLECTION
            assert document_id == "CA_test"
            return call_document

    class Database:
        def collection(self, name):
            return Collection(name)

        def transaction(self):
            return tx

    monkeypatch.setattr(receipt_db, "get_firestore_client", Database)
    monkeypatch.setattr(receipt_db.firestore, "transactional", lambda function: function)
    monkeypatch.setattr(receipt_db.time, "time", lambda: 40.0)

    projected = await receipt_db.project_receipt_to_call("receipt-test")

    assert projected is True
    assert transaction_events[0] == (
        "set",
        call_document,
        {
            "post_call_delivery_owner_sms_status": "delivered",
            "post_call_delivery_owner_sms_provider_status": "delivered",
            "post_call_delivery_owner_sms_failure_code": "",
            "post_call_delivery_owner_sms_error_code": None,
            "post_call_delivery_owner_sms_updated_at": 30.0,
            "call_sid": "CA_test",
            "post_call_delivery_owner_sms_receipt_id": "receipt-test",
            "post_call_delivery_owner_sms_version": 2,
        },
        True,
    )
    assert transaction_events[1][0:2] == ("update", receipt_document)
    assert transaction_events[1][2]["call_projection_pending"] is False
    assert transaction_events[1][2]["call_projected_version"] == 2


@pytest.mark.asyncio
async def test_receipt_writes_fail_closed_when_client_initialization_fails(
    monkeypatch,
):
    def unavailable_client():
        raise RuntimeError("private storage configuration")

    monkeypatch.setattr(receipt_db, "get_firestore_client", unavailable_client)

    assert await receipt_db.create_receipt(
        call_sid="CA_test",
        effect="owner_sms",
        channel="sms",
    ) == ""
    assert (
        await receipt_db.record_provider_update(
            "receipt-test",
            provider_status="sent",
            provider_message_sid=MESSAGE_SID,
        )
    ).outcome == "error"
    assert await receipt_db.mark_submission_failed("receipt-test") is False
    assert await receipt_db.mark_reconciled("receipt-test") is False


def test_call_record_updates_are_effect_scoped_and_failure_latched():
    failed = receipt_db.call_record_delivery_updates(
        {
            "effect": "owner_sms",
            "status": "failed",
            "provider_status": "undelivered",
            "failure_code": "provider_delivery_failed",
            "provider_error_code": 30001,
            "updated_at": 20.0,
        }
    )
    delivered = receipt_db.call_record_delivery_updates(
        {
            "effect": "caller_confirmation",
            "status": "delivered",
            "provider_status": "delivered",
            "failure_code": "",
            "provider_error_code": None,
            "updated_at": 30.0,
        }
    )

    assert failed == {
        "post_call_delivery_owner_sms_status": "failed",
        "post_call_delivery_owner_sms_provider_status": "undelivered",
        "post_call_delivery_owner_sms_failure_code": "provider_delivery_failed",
        "post_call_delivery_owner_sms_error_code": 30001,
        "post_call_delivery_owner_sms_updated_at": 20.0,
    }
    assert delivered == {
        "post_call_delivery_caller_confirmation_status": "delivered",
        "post_call_delivery_caller_confirmation_provider_status": "delivered",
        "post_call_delivery_caller_confirmation_failure_code": "",
        "post_call_delivery_caller_confirmation_error_code": None,
        "post_call_delivery_caller_confirmation_updated_at": 30.0,
    }
    assert set(failed).isdisjoint(set(delivered))
    assert receipt_db.call_record_delivery_updates(
        {"effect": "private-effect", "status": "failed"}
    ) == {}
