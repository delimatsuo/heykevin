import os
import time
from typing import Optional

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.config import settings
from app.services import estimate_notifications, estimate_worker


class FakeDocSnapshot:
    def __init__(self, key: str, data: Optional[dict], doc_ref: "FakeDocRef"):
        self.id = key
        self.exists = data is not None
        self._data = dict(data) if data is not None else None
        self.reference = doc_ref

    def to_dict(self) -> Optional[dict]:
        return dict(self._data) if self._data is not None else None


class FakeDocRef:
    def __init__(self, collection: "FakeCollection", key: str):
        self.collection = collection
        self.id = key

    def get(self, transaction=None) -> FakeDocSnapshot:
        data = self.collection.docs.get(self.id)
        return FakeDocSnapshot(self.id, data, self)

    def set(self, data: dict) -> None:
        self.collection.docs[self.id] = dict(data)

    def update(self, data: dict) -> None:
        if self.id not in self.collection.docs:
            self.collection.docs[self.id] = {}
        self.collection.docs[self.id].update(data)


class FakeQuery:
    def __init__(self, collection: "FakeCollection", filters=None, limit_val=None):
        self.collection = collection
        self.filters = filters or []
        self.limit_val = limit_val

    def where(self, field: str, op: str, value: object) -> "FakeQuery":
        new_filters = list(self.filters)
        new_filters.append((field, op, value))
        return FakeQuery(self.collection, new_filters, self.limit_val)

    def limit(self, count: int) -> "FakeQuery":
        return FakeQuery(self.collection, self.filters, count)

    def stream(self):
        results = []
        for key, data in self.collection.docs.items():
            match = True
            for field, op, val in self.filters:
                if op == "==" and data.get(field) != val:
                    match = False
                    break
            if match:
                results.append(FakeDocSnapshot(key, data, self.collection.document(key)))
        if self.limit_val is not None:
            results = results[:self.limit_val]
        return results

    def get(self):
        return self.stream()


class FakeCollection:
    def __init__(self, name: str):
        self.name = name
        self.docs: dict[str, dict] = {}

    def document(self, key: str) -> FakeDocRef:
        return FakeDocRef(self, key)

    def where(self, field: str, op: str, value: object) -> FakeQuery:
        return FakeQuery(self).where(field, op, value)

    def limit(self, count: int) -> FakeQuery:
        return FakeQuery(self).limit(count)

    def stream(self):
        return FakeQuery(self).stream()


class FakeTransaction:
    def __init__(self, db: "FakeDB"):
        self.db = db
        self._read_only = False
        self._id = b"fake-tx"

    def update(self, doc_ref: FakeDocRef, data: dict) -> None:
        doc_ref.update(data)


class FakeDB:
    def __init__(self):
        self.collections: dict[str, FakeCollection] = {}

    def collection(self, name: str) -> FakeCollection:
        if name not in self.collections:
            self.collections[name] = FakeCollection(name)
        return self.collections[name]

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)


# 17. estimate_worker_loop re-claims an estimate whose lease expired and completes it from the archived bytes;
# a doc already complete is left untouched and no SMS is re-sent (notified_at guard asserted).
@pytest.mark.asyncio
async def test_estimate_worker_loop_reclaims_expired_lease_and_completes_idempotently(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(estimate_worker, "get_firestore_client", lambda: db)
    monkeypatch.setattr(estimate_worker, "transactional", lambda fn: fn)
    monkeypatch.setattr(settings, "vcard_hmac_secret", "secret-test-key-32bytes-length-1234567")
    monkeypatch.setattr(settings, "estimate_media_bucket", "test-bucket")

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "twilio_number": "+15559999999",
            "owner_phone": "+15550000000",
            "business_name": "Acme Services",
            "services": [{"name": "Inspection", "price_min": 50, "price_max": 100}],
        }

    monkeypatch.setattr(estimate_worker, "get_contractor", fake_get_contractor)

    # 1. Seed a stranded processing doc whose lease expired 10 seconds ago
    token_hash_1 = "token_hash_stranded_1"
    media_id_1 = "media_id_stranded_1"
    object_path_1 = f"{token_hash_1}/{media_id_1}.mp4"
    now = time.time()

    db.collection("estimates").document(token_hash_1).set({
        "token_hash": token_hash_1,
        "contractor_id": "c1",
        "caller_phone": "+15551234567",
        "call_sid": "CA111",
        "status": "processing",
        "attempts": 1,
        "lease_expires_at": now - 10.0,
        "media_id": media_id_1,
        "media_object_path": object_path_1,
        "media_content_type": "video/mp4",
        "description": "Kitchen pipe leaking",
        "notified_at": None,
        "result": None,
    })

    # 2. Seed a doc that is already complete and notified_at is set
    token_hash_2 = "token_hash_complete_2"
    db.collection("estimates").document(token_hash_2).set({
        "token_hash": token_hash_2,
        "contractor_id": "c1",
        "caller_phone": "+15551234567",
        "call_sid": "CA222",
        "status": "complete",
        "attempts": 1,
        "lease_expires_at": now - 100.0,
        "media_id": "media_2",
        "media_object_path": f"{token_hash_2}/media_2.mp4",
        "media_content_type": "video/mp4",
        "notified_at": now - 90.0,
        "result": {"diagnosis": "Already fixed", "confidence": "high"},
    })

    sms_sent = []

    async def capture_sms(to, msg, **kwargs):
        sms_sent.append({"to": to, "msg": msg})

    async def fake_analyze_media(media_bytes, media_type, services_list, business_name, text_description=""):
        assert media_bytes == b"archived-gcs-bytes"
        assert text_description == "Kitchen pipe leaking"
        return {
            "diagnosis": "Kitchen pipe leaking recovered",
            "matched_services": [],
            "estimate_min": 100,
            "estimate_max": 200,
            "confidence": "high",
        }

    monkeypatch.setattr(estimate_worker, "read_media", lambda path: b"archived-gcs-bytes")
    monkeypatch.setattr(estimate_worker, "analyze_media", fake_analyze_media)
    monkeypatch.setattr(estimate_notifications, "send_sms", capture_sms)

    # Run one sweep of the recovery worker
    await estimate_worker.run_pending_estimates_once(now=now)

    # Assert doc 1 was re-claimed (attempts=2), completed, and notified
    doc1 = db.collection("estimates").document(token_hash_1).get().to_dict()
    assert doc1["status"] == "complete"
    assert doc1["attempts"] == 2
    assert doc1["result"]["diagnosis"] == "Kitchen pipe leaking recovered"
    assert doc1["notified_at"] is not None

    # Assert doc 2 was completely untouched
    doc2 = db.collection("estimates").document(token_hash_2).get().to_dict()
    assert doc2["status"] == "complete"
    assert doc2["result"]["diagnosis"] == "Already fixed"

    # Assert exactly 2 SMS were sent (1 caller + 1 owner for doc1, 0 for doc2)
    assert len(sms_sent) == 2
    assert any("Kitchen pipe leaking recovered" in s["msg"] for s in sms_sent)


# 18. After MAX_ANALYSIS_ATTEMPTS expired leases → failed, failure SMS to caller and owner sent exactly once.
@pytest.mark.asyncio
async def test_estimate_worker_loop_marks_failed_after_max_attempts(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(estimate_worker, "get_firestore_client", lambda: db)
    monkeypatch.setattr(estimate_worker, "transactional", lambda fn: fn)
    monkeypatch.setattr(settings, "vcard_hmac_secret", "secret-test-key-32bytes-length-1234567")
    monkeypatch.setattr(settings, "estimate_media_bucket", "test-bucket")

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "twilio_number": "+15559999999",
            "owner_phone": "+15550000000",
            "business_name": "Acme Services",
        }

    monkeypatch.setattr(estimate_worker, "get_contractor", fake_get_contractor)

    token_hash = "token_max_attempts"
    media_id = "media_max_attempts"
    now = time.time()

    # Seed doc with attempts = 3 (MAX_ANALYSIS_ATTEMPTS) and expired lease
    db.collection("estimates").document(token_hash).set({
        "token_hash": token_hash,
        "contractor_id": "c1",
        "caller_phone": "+15551234567",
        "call_sid": "CA333",
        "status": "processing",
        "attempts": 3,
        "lease_expires_at": now - 10.0,
        "media_id": media_id,
        "media_object_path": f"{token_hash}/{media_id}.mp4",
        "media_content_type": "video/mp4",
        "notified_at": None,
        "result": None,
    })

    sms_sent = []

    async def capture_sms(to, msg, **kwargs):
        sms_sent.append({"to": to, "msg": msg})

    analyzer_called = False

    async def fail_analyzer(**kwargs):
        nonlocal analyzer_called
        analyzer_called = True

    monkeypatch.setattr(estimate_worker, "analyze_media", fail_analyzer)
    monkeypatch.setattr(estimate_notifications, "send_sms", capture_sms)

    # First sweep: should transition to failed and send SMS
    await estimate_worker.run_pending_estimates_once(now=now)

    doc = db.collection("estimates").document(token_hash).get().to_dict()
    assert doc["status"] == "failed"
    assert not analyzer_called
    assert doc["notified_at"] is not None

    assert len(sms_sent) == 2
    caller_sms = [s for s in sms_sent if s["to"] == "+15551234567"][0]
    owner_sms = [s for s in sms_sent if s["to"] == "+15550000000"][0]

    assert "couldn't process this media" in caller_sms["msg"]
    assert "AI ESTIMATE FAILED" in owner_sms["msg"]
    assert "Watch the caller's video:" in owner_sms["msg"]

    # Second sweep: should NOT send any additional SMS (idempotency)
    await estimate_worker.run_pending_estimates_once(now=now + 50)
    assert len(sms_sent) == 2


# --- Direct claim-function coverage (added in review) -----------------------
# The loop-level tests exercise the status guard: an already-terminal doc is
# untouched. The notified_at branch inside the claims never evaluates on those
# paths, so a mutation forcing should_notify=True survived the suite — the
# exact guard §4.2 names was unverified. These test the claim functions
# directly with the state that branch exists for.


def _claims_db_with(doc_id: str, data: dict):
    db = FakeDB()
    collection = db.collection("estimates")
    doc_ref = collection.document(doc_id)
    doc_ref.set(data)
    return db, doc_ref


def test_notification_claim_complete_denies_resend_when_already_notified(monkeypatch):
    monkeypatch.setattr(estimate_worker, "transactional", lambda fn: fn)
    db, doc_ref = _claims_db_with("tok1", {
        "status": "processing",
        "media_id": "m1",
        "notified_at": 123.0,
    })

    accepted, should_notify, _ = estimate_worker.claim_notification_and_complete(
        db, doc_ref, "m1", {"diagnosis": "x"}, 999.0
    )

    assert accepted is True
    assert should_notify is False
    # Terminal transition still happens; only the notification right is denied.
    assert doc_ref.get().to_dict()["status"] == "complete"
    # The original notified_at is preserved, not overwritten.
    assert doc_ref.get().to_dict()["notified_at"] == 123.0


def test_notification_claim_fail_denies_resend_when_already_notified(monkeypatch):
    monkeypatch.setattr(estimate_worker, "transactional", lambda fn: fn)
    db, doc_ref = _claims_db_with("tok2", {
        "status": "processing",
        "media_id": "m2",
        "notified_at": 123.0,
    })

    accepted, should_notify, _ = estimate_worker.claim_notification_and_fail(
        db, doc_ref, "m2", 999.0
    )

    assert accepted is True
    assert should_notify is False
    assert doc_ref.get().to_dict()["status"] == "failed"


def test_reanalysis_claim_max_attempts_with_prior_notification_does_not_renotify(monkeypatch):
    monkeypatch.setattr(estimate_worker, "transactional", lambda fn: fn)
    db, doc_ref = _claims_db_with("tok3", {
        "status": "processing",
        "media_id": "m3",
        "attempts": estimate_worker.MAX_ANALYSIS_ATTEMPTS,
        "lease_expires_at": 10.0,
        "notified_at": 123.0,
    })

    action, _ = estimate_worker.claim_reanalysis_or_fail(db, doc_ref, now=999.0)

    assert action == "failed_no_notify"
    assert doc_ref.get().to_dict()["status"] == "failed"


def test_notification_claim_grants_exactly_one_winner(monkeypatch):
    """Two sequential claimants on the same doc: first wins, second is denied.

    The fake transaction cannot interleave, but the winner's write lands
    before the loser's read — the ordering real Firestore transactions
    guarantee. The loser must see accepted=False (terminal status), never a
    second should_notify=True.
    """
    monkeypatch.setattr(estimate_worker, "transactional", lambda fn: fn)
    db, doc_ref = _claims_db_with("tok4", {
        "status": "processing",
        "media_id": "m4",
        "notified_at": None,
    })

    first = estimate_worker.claim_notification_and_complete(db, doc_ref, "m4", {}, 1.0)
    second = estimate_worker.claim_notification_and_complete(db, doc_ref, "m4", {}, 2.0)

    assert first[0] is True and first[1] is True
    assert second[0] is False
    assert second[1] is False
