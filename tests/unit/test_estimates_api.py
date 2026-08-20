import asyncio
import hashlib
import json
import os
import time
from typing import Optional

from fastapi import HTTPException
import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.api import estimates
from app.config import settings
from app.services import estimate_notifications, estimate_worker
from app.services.gated_actions import ActionKey


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
                if op == "array_contains" and val not in (data.get(field) or []):
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


class _StreamingRequest:
    def __init__(self, body: bytes, content_type: str = "video/mp4", query_params=None):
        self.body = body
        self.headers = {"content-type": content_type, "content-length": str(len(body))}
        self.query_params = query_params or {}

    async def stream(self):
        yield self.body


@pytest.fixture
def setup_env(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(estimates, "get_firestore_client", lambda: db)
    monkeypatch.setattr(estimates, "transactional", lambda fn: fn)
    monkeypatch.setattr(estimate_worker, "transactional", lambda fn: fn)
    monkeypatch.setattr(settings, "vcard_hmac_secret", "secret-test-key-32bytes-length-1234567")
    monkeypatch.setattr(settings, "estimate_media_bucket", "test-bucket")
    monkeypatch.setattr(settings, "cloud_run_url", "https://api.example.com")

    # Contractor with approved SMS
    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "twilio_number": "+15559999999",
            "owner_phone": "+15550000000",
            "business_name": "Acme Services",
            "services": [{"name": "Inspection", "price_min": 50, "price_max": 100}],
            "gated_actions": {ActionKey.ESTIMATE_RESULT_SMS.value: True},
            "sms_compliance_status": "approved",
        }

    monkeypatch.setattr(estimates, "get_contractor", fake_get_contractor)
    return db


def _seed_estimate(db: FakeDB, token: str, status: str = "pending") -> str:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db.collection("estimates").document(token_hash).set({
        "token_hash": token_hash,
        "contractor_id": "c1",
        "caller_phone": "+15551234567",
        "call_sid": "CA12345",
        "created_at": time.time(),
        "expires_at": time.time() + 3600,
        "status": status,
        "upload_count": 0,
        "attempts": 0,
        "lease_expires_at": 0,
        "media_object_path": None,
        "media_id": None,
        "notified_at": None,
        "result": None,
    })
    return token_hash


# 9. Video upload returns 202 before the analysis task completes (ordering asserted via an analyzer that blocks on an event).
@pytest.mark.asyncio
async def test_video_upload_returns_202_before_analysis_completes(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-video-202"
    token_hash = _seed_estimate(db, token)

    analysis_started = asyncio.Event()
    analysis_unblock = asyncio.Event()
    analysis_finished = asyncio.Event()

    async def blocking_analyze_media(**kwargs):
        analysis_started.set()
        await analysis_unblock.wait()
        analysis_finished.set()
        return {
            "diagnosis": "Broken pipe",
            "matched_services": [],
            "estimate_min": 100,
            "estimate_max": 200,
            "confidence": "high",
        }

    monkeypatch.setattr(estimates, "analyze_media", blocking_analyze_media)
    monkeypatch.setattr(estimates, "archive_media", lambda *a, **kw: f"{token_hash}/media123.mp4")

    request = _StreamingRequest(body=b"fake-video-data", content_type="video/mp4")
    response = await estimates.upload_and_analyze(token, request=request)

    # Assert 202 is returned BEFORE analysis completes
    assert response.status_code == 202
    assert json.loads(response.body) == {"status": "processing"}
    assert not analysis_finished.is_set()

    # Unblock and wait for background task to finish
    await analysis_started.wait()
    analysis_unblock.set()
    await analysis_finished.wait()


# 10. Video GCS failure → 503, analyzer never called, status not processing.
@pytest.mark.asyncio
async def test_video_gcs_failure_returns_503_and_analyzer_never_called(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-gcs-fail"
    token_hash = _seed_estimate(db, token, status="pending")

    def fail_archive(*args, **kwargs):
        raise RuntimeError("GCS storage connection failed")

    analyzer_called = False

    async def fail_analyzer(**kwargs):
        nonlocal analyzer_called
        analyzer_called = True
        return {}

    monkeypatch.setattr(estimates, "archive_media", fail_archive)
    monkeypatch.setattr(estimates, "analyze_media", fail_analyzer)

    request = _StreamingRequest(body=b"fake-video-bytes", content_type="video/mp4")
    with pytest.raises(HTTPException) as exc_info:
        await estimates.upload_and_analyze(token, request=request)

    assert exc_info.value.status_code == 503
    assert not analyzer_called
    doc = db.collection("estimates").document(token_hash).get().to_dict()
    assert doc["status"] == "pending"


# 11. Background failure (analyzer raises) → status failed, caller failure SMS sent, owner SMS sent and contains the watch URL.
@pytest.mark.asyncio
async def test_background_analysis_failure_marks_failed_and_sends_sms_with_watch_url(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-analysis-fail"
    token_hash = _seed_estimate(db, token)

    sms_sent = []

    async def capture_send_sms(to, msg, **kwargs):
        sms_sent.append({"to": to, "msg": msg, "kwargs": kwargs})

    async def failing_analyzer(**kwargs):
        raise RuntimeError("Gemini Files API failed to process video")

    monkeypatch.setattr(estimate_notifications, "send_sms", capture_send_sms)
    monkeypatch.setattr(estimates, "analyze_media", failing_analyzer)
    monkeypatch.setattr(estimates, "archive_media", lambda *a, **kw: f"{token_hash}/med_fail.mp4")

    request = _StreamingRequest(body=b"fake-video-data", content_type="video/mp4")
    resp = await estimates.upload_and_analyze(token, request=request)
    assert resp.status_code == 202

    # Give background task time to run and handle failure
    await asyncio.sleep(0.1)

    doc = db.collection("estimates").document(token_hash).get().to_dict()
    assert doc["status"] == "failed"
    assert doc["notified_at"] is not None

    # Caller failure SMS and Owner failure SMS with watch URL
    assert len(sms_sent) == 2
    caller_sms = [s for s in sms_sent if s["to"] == "+15551234567"][0]
    owner_sms = [s for s in sms_sent if s["to"] == "+15550000000"][0]

    assert "couldn't process this media" in caller_sms["msg"]
    assert "AI ESTIMATE FAILED" in owner_sms["msg"]
    assert "Watch the caller's video:" in owner_sms["msg"]
    assert "/api/estimates/media/" in owner_sms["msg"]


# 12. Success → status complete, result stored, both SMS sent, owner SMS contains the watch URL.
@pytest.mark.asyncio
async def test_video_analysis_success_marks_complete_and_sends_sms_with_watch_url(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-success"
    token_hash = _seed_estimate(db, token)

    sms_sent = []

    async def capture_send_sms(to, msg, **kwargs):
        sms_sent.append({"to": to, "msg": msg, "kwargs": kwargs})

    async def success_analyzer(**kwargs):
        return {
            "diagnosis": "Main pipe leak",
            "matched_services": [{"name": "Inspection", "price_min": 50, "price_max": 100}],
            "estimate_min": 50,
            "estimate_max": 100,
            "requires_manual_investigation": False,
            "confidence": "high",
        }

    monkeypatch.setattr(estimate_notifications, "send_sms", capture_send_sms)
    monkeypatch.setattr(estimates, "analyze_media", success_analyzer)
    monkeypatch.setattr(estimates, "archive_media", lambda *a, **kw: f"{token_hash}/med_succ.mp4")

    request = _StreamingRequest(body=b"fake-video-data", content_type="video/mp4")
    resp = await estimates.upload_and_analyze(token, request=request)
    assert resp.status_code == 202

    await asyncio.sleep(0.1)

    doc = db.collection("estimates").document(token_hash).get().to_dict()
    assert doc["status"] == "complete"
    assert doc["result"]["diagnosis"] == "Main pipe leak"
    assert doc["notified_at"] is not None

    assert len(sms_sent) == 2
    caller_sms = [s for s in sms_sent if s["to"] == "+15551234567"][0]
    owner_sms = [s for s in sms_sent if s["to"] == "+15550000000"][0]

    assert "AI Diagnosis: Main pipe leak" in caller_sms["msg"]
    assert "AI ESTIMATE SENT" in owner_sms["msg"]
    assert "Watch the caller's video:" in owner_sms["msg"]
    assert "/api/estimates/media/" in owner_sms["msg"]


# 13. Gate denial still blocks the upload (regression: existing behavior).
@pytest.mark.asyncio
async def test_estimate_upload_gate_denial_blocks_upload(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-gate-denied"
    _seed_estimate(db, token)

    async def fake_get_contractor_disabled(_cid):
        return {
            "contractor_id": "c1",
            "gated_actions": {ActionKey.ESTIMATE_RESULT_SMS.value: False},
            "sms_compliance_status": "approved",
        }

    monkeypatch.setattr(estimates, "get_contractor", fake_get_contractor_disabled)

    request = _StreamingRequest(body=b"fake-video-data", content_type="video/mp4")
    with pytest.raises(HTTPException) as exc_info:
        await estimates.upload_and_analyze(token, request=request)

    assert exc_info.value.status_code == 403


# 14. Photo path unchanged: synchronous result, archived, no Files API call.
@pytest.mark.asyncio
async def test_photo_upload_synchronous_result_archived_no_files_api(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-photo-sync"
    token_hash = _seed_estimate(db, token)

    archived_paths = []
    sms_sent = []

    def fake_archive(token_hash, media_id, bytes_data, content_type):
        path = f"{token_hash}/{media_id}.jpg"
        archived_paths.append(path)
        return path

    async def fake_analyze_media(**kwargs):
        assert kwargs["media_type"] == "image/jpeg"
        return {
            "diagnosis": "Surface scratch",
            "matched_services": [],
            "estimate_min": 30,
            "estimate_max": 60,
            "confidence": "high",
        }

    async def capture_send_sms(to, msg, **kwargs):
        sms_sent.append({"to": to, "msg": msg})

    monkeypatch.setattr(estimates, "archive_media", fake_archive)
    monkeypatch.setattr(estimates, "analyze_media", fake_analyze_media)
    monkeypatch.setattr(estimate_notifications, "send_sms", capture_send_sms)

    request = _StreamingRequest(body=b"fake-photo-data", content_type="image/jpeg")
    result = await estimates.upload_and_analyze(token, request=request)

    assert result["status"] == "complete"
    assert result["result"]["diagnosis"] == "Surface scratch"
    assert len(archived_paths) == 1
    assert len(sms_sent) == 2

    doc = db.collection("estimates").document(token_hash).get().to_dict()
    assert doc["status"] == "complete"


# 15. Valid signature → 302 with a GCS URL. Invalid → 403. Unknown id → 404.
@pytest.mark.asyncio
async def test_media_redirect_valid_sig_302_invalid_403_unknown_404(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-media-redirect"
    token_hash = _seed_estimate(db, token)

    media_id = "med_test_123"
    object_path = f"{token_hash}/{media_id}.mp4"
    db.collection("estimates").document(token_hash).update({
        "media_id": media_id,
        "media_object_path": object_path,
    })

    monkeypatch.setattr(
        estimates,
        "gcs_redirect_url",
        lambda path: f"https://storage.googleapis.com/test-bucket/{path}?signed=true",
    )

    watch_url = estimates.make_watch_url(media_id)
    # Parse e and s params
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(watch_url).query)
    expires = int(qs["e"][0])
    sig = qs["s"][0]

    # Valid signature -> 302 redirect
    response = await estimates.get_estimate_media_redirect(media_id, e=expires, s=sig)
    assert response.status_code == 302
    assert response.headers["location"] == f"https://storage.googleapis.com/test-bucket/{object_path}?signed=true"

    # Invalid signature -> 403
    with pytest.raises(HTTPException) as exc_403:
        await estimates.get_estimate_media_redirect(media_id, e=expires, s="tampered-signature")
    assert exc_403.value.status_code == 403

    # Unknown media_id with validly signed other id -> 404
    other_media_id = "unknown_media_999"
    other_watch = estimates.make_watch_url(other_media_id)
    other_qs = parse_qs(urlparse(other_watch).query)
    with pytest.raises(HTTPException) as exc_404:
        await estimates.get_estimate_media_redirect(
            other_media_id,
            e=int(other_qs["e"][0]),
            s=other_qs["s"][0],
        )
    assert exc_404.value.status_code == 404


# 16. Video upload persists the claim (processing, attempts=1, lease_expires_at) before the 202 is returned.
@pytest.mark.asyncio
async def test_video_upload_persists_claim_before_202_returned(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-claim-persisted"
    token_hash = _seed_estimate(db, token)

    monkeypatch.setattr(estimates, "archive_media", lambda *a, **kw: f"{token_hash}/media.mp4")
    monkeypatch.setattr(estimates, "analyze_media", lambda **kw: asyncio.sleep(10))

    request = _StreamingRequest(body=b"fake-video-bytes", content_type="video/mp4")
    resp = await estimates.upload_and_analyze(token, request=request)

    assert resp.status_code == 202

    doc = db.collection("estimates").document(token_hash).get().to_dict()
    assert doc["status"] == "processing"
    assert doc["attempts"] == 1
    assert doc["lease_expires_at"] > time.time()
    assert doc["media_object_path"] == f"{token_hash}/media.mp4"


# 19. Second upload while processing → 409, no new task, no object overwrite (first attempt's media_id/object path unchanged).
@pytest.mark.asyncio
async def test_second_upload_while_processing_returns_409_and_starts_nothing(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-concurrent"
    token_hash = _seed_estimate(db, token)

    first_media_id = "first_attempt_media_id"
    first_object = f"{token_hash}/{first_media_id}.mp4"
    db.collection("estimates").document(token_hash).update({
        "status": "processing",
        "attempts": 1,
        "lease_expires_at": time.time() + 300,
        "media_id": first_media_id,
        "media_object_path": first_object,
    })

    archive_called = False

    def fail_archive(*args, **kwargs):
        nonlocal archive_called
        archive_called = True
        return "second.mp4"

    monkeypatch.setattr(estimates, "archive_media", fail_archive)

    request = _StreamingRequest(body=b"second-upload-data", content_type="video/mp4")
    resp = await estimates.upload_and_analyze(token, request=request)

    assert resp.status_code == 409
    assert json.loads(resp.body) == {"status": "processing"}

    doc = db.collection("estimates").document(token_hash).get().to_dict()
    assert doc["status"] == "processing"
    assert doc["media_id"] == first_media_id
    assert doc["media_object_path"] == first_object
    # Review round 3, item 3: the conflict must be detected before any GCS
    # write — a valid token must not be able to spam objects into the bucket.
    assert archive_called is False


# 20. Re-upload after failed → fresh media_id and object; the stored result/watch pair always refers to the same attempt.
@pytest.mark.asyncio
async def test_reupload_after_failed_creates_fresh_media_id_and_object(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-reupload-failed"
    token_hash = _seed_estimate(db, token, status="failed")

    old_media_id = "old_failed_media_id"
    db.collection("estimates").document(token_hash).update({
        "media_id": old_media_id,
        "media_object_path": f"{token_hash}/{old_media_id}.mp4",
    })

    created_paths = []

    def track_archive(token_hash, media_id, bytes_data, content_type):
        p = f"{token_hash}/{media_id}.mp4"
        created_paths.append((media_id, p))
        return p

    async def fast_analyze(**kwargs):
        return {"diagnosis": "Fixed on retry", "estimate_min": 10, "estimate_max": 20, "confidence": "high"}

    monkeypatch.setattr(estimates, "archive_media", track_archive)
    monkeypatch.setattr(estimates, "analyze_media", fast_analyze)
    monkeypatch.setattr(estimate_notifications, "send_sms", lambda *a, **kw: None)

    request = _StreamingRequest(body=b"retry-video-data", content_type="video/mp4")
    resp = await estimates.upload_and_analyze(token, request=request)

    assert resp.status_code == 202
    assert len(created_paths) == 1
    new_media_id, new_path = created_paths[0]

    assert new_media_id != old_media_id

    await asyncio.sleep(0.1)

    doc = db.collection("estimates").document(token_hash).get().to_dict()
    assert doc["status"] == "complete"
    assert doc["media_id"] == new_media_id
    assert doc["media_object_path"] == new_path


# 21. ?description= reaches analyze_media(text_description=...), is stored on the doc, and never appears in log output.
@pytest.mark.asyncio
async def test_description_parameter_passed_to_analyzer_and_stored_never_logged(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-desc"
    token_hash = _seed_estimate(db, token)

    captured_description = ""
    logged_messages = []

    async def capture_analyzer(**kwargs):
        nonlocal captured_description
        captured_description = kwargs.get("text_description", "")
        return {"diagnosis": "Diagnosed with text", "confidence": "high"}

    def capture_log(msg, *args, **kwargs):
        logged_messages.append(str(msg) + " " + str(args) + " " + str(kwargs))

    monkeypatch.setattr(estimates, "analyze_media", capture_analyzer)
    monkeypatch.setattr(estimates, "archive_media", lambda *a, **kw: f"{token_hash}/media.mp4")
    monkeypatch.setattr(estimate_notifications, "send_sms", lambda *a, **kw: None)
    monkeypatch.setattr(estimates.logger, "info", capture_log)

    sensitive_text = "SECRET_CALLER_DESCRIPTION_TEXT_12345"
    request = _StreamingRequest(
        body=b"video-bytes",
        content_type="video/mp4",
        query_params={"description": sensitive_text},
    )

    resp = await estimates.upload_and_analyze(token, request=request, description=sensitive_text)
    assert resp.status_code == 202

    await asyncio.sleep(0.1)

    assert captured_description == sensitive_text
    doc = db.collection("estimates").document(token_hash).get().to_dict()
    assert doc["description"] == sensitive_text

    # Assert sensitive description never appears in log output
    all_logs = "\n".join(logged_messages)
    assert sensitive_text not in all_logs


# --- Review round 3 (PR #190 triage items 1-4) ------------------------------


# Item 2: a photo submitted while a video attempt is processing must not
# clobber that attempt's media identity. The handler-start snapshot is stale
# here on purpose, so the cheap pre-check passes and only the atomic claim
# stands between the photo and the running attempt.
@pytest.mark.asyncio
async def test_photo_during_video_processing_hits_the_claim_and_409s(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-photo-race"
    token_hash = _seed_estimate(db, token)

    video_media_id = "video_attempt_media"
    video_object = f"{token_hash}/{video_media_id}.mp4"
    db.collection("estimates").document(token_hash).update({
        "status": "processing",
        "attempts": 1,
        "lease_expires_at": time.time() + 300,
        "media_id": video_media_id,
        "media_object_path": video_object,
    })

    # Stale snapshot: the handler believes the estimate is still pending.
    stale = db.collection("estimates").document(token_hash).get().to_dict().copy()
    stale["status"] = "pending"

    async def stale_get(_token):
        return stale

    monkeypatch.setattr(estimates, "_get_estimate_doc", stale_get)
    monkeypatch.setattr(estimates, "archive_media", lambda *a, **kw: f"{token_hash}/photo.jpg")

    analyzer_called = False

    async def no_analyze(**kwargs):
        nonlocal analyzer_called
        analyzer_called = True
        return {}

    monkeypatch.setattr(estimates, "analyze_media", no_analyze)

    request = _StreamingRequest(body=b"photo-bytes", content_type="image/jpeg")
    resp = await estimates.upload_and_analyze(token, request=request)

    assert resp.status_code == 409
    assert analyzer_called is False
    doc = db.collection("estimates").document(token_hash).get().to_dict()
    assert doc["media_id"] == video_media_id
    assert doc["media_object_path"] == video_object


# Item 4: a watch link texted before a re-upload was signed for 90 days; the
# media history must keep it resolvable after media_id moves on.
@pytest.mark.asyncio
async def test_superseded_watch_link_still_resolves_via_media_history(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-history"
    token_hash = _seed_estimate(db, token)

    old_id, new_id = "attempt_one_media", "attempt_two_media"
    old_path = f"{token_hash}/{old_id}.mp4"
    new_path = f"{token_hash}/{new_id}.mp4"
    db.collection("estimates").document(token_hash).update({
        "media_id": new_id,
        "media_object_path": new_path,
        "media_ids": [old_id, new_id],
        "media_paths": {old_id: old_path, new_id: new_path},
    })

    monkeypatch.setattr(
        estimates,
        "gcs_redirect_url",
        lambda path: f"https://storage.googleapis.com/test-bucket/{path}?signed=true",
    )

    from urllib.parse import parse_qs, urlparse

    for media_id, expected_path in ((old_id, old_path), (new_id, new_path)):
        watch_url = estimates.make_watch_url(media_id)
        qs = parse_qs(urlparse(watch_url).query)
        response = await estimates.get_estimate_media_redirect(
            media_id, e=int(qs["e"][0]), s=qs["s"][0]
        )
        assert response.status_code == 302
        assert expected_path in response.headers["location"]


# Item 4 regression: uploads claimed through _claim_upload record their
# attempt in the history so future links never dangle.
@pytest.mark.asyncio
async def test_claim_appends_media_history(setup_env, monkeypatch):
    db = setup_env
    token = "test-token-history-append"
    token_hash = _seed_estimate(db, token)

    monkeypatch.setattr(estimates, "archive_media", lambda *a, **kw: f"{token_hash}/vid.mp4")

    started = asyncio.Event()

    async def hold_analyzer(**kwargs):
        started.set()
        return {"diagnosis": "x", "confidence": "high"}

    monkeypatch.setattr(estimates, "analyze_media", hold_analyzer)
    monkeypatch.setattr(estimates, "send_sms", lambda *a, **kw: None)

    request = _StreamingRequest(body=b"video-bytes", content_type="video/mp4")
    resp = await estimates.upload_and_analyze(token, request=request)
    assert resp.status_code == 202
    await started.wait()

    doc = db.collection("estimates").document(token_hash).get().to_dict()
    assert doc["media_id"] in doc["media_ids"]
    assert doc["media_paths"][doc["media_id"]] == doc["media_object_path"]
