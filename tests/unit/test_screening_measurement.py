"""Unit tests for personal screening conversation measurement, country snapshot, and funnel aggregation."""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import asyncio
import datetime
import math
import threading
import time
from typing import Any, Dict, Optional
import pytest

from app.config import Settings
from app.db.contractors import create_contractor
from app.services import acquisition
from app.services import post_call
from app.services import post_call_handoff
from scripts.summarize_acquisition_funnel import (
    summarize_acquisition_funnel,
    validate_measurement_record,
)


@pytest.fixture(autouse=True)
def outbound_tripwire(monkeypatch):
    """Prevent real network and Firestore client construction during unit tests."""
    def _block_httpx(*args, **kwargs):
        raise RuntimeError("Real httpx network transport is forbidden in unit tests")

    def _block_requests(*args, **kwargs):
        raise RuntimeError("Real requests network call is forbidden in unit tests")

    def _block_firestore(*args, **kwargs):
        raise RuntimeError("Real google.cloud.firestore.Client construction is forbidden in unit tests")

    def _block_get_client(*args, **kwargs):
        raise RuntimeError("Real get_firestore_client is forbidden in unit tests")

    monkeypatch.setattr("httpx.AsyncHTTPTransport.handle_async_request", _block_httpx)
    import requests.sessions
    monkeypatch.setattr(requests.sessions.Session, "request", _block_requests)
    import google.cloud.firestore
    monkeypatch.setattr(google.cloud.firestore.Client, "__init__", _block_firestore)
    monkeypatch.setattr("app.db.firestore_client.get_firestore_client", _block_get_client)


class _InMemoryFirestore:
    def __init__(self):
        self._docs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def collection(self, name: str):
        return _Collection(self, name)

    def transaction(self):
        return _FakeTransaction(self)


class _Collection:
    def __init__(self, fs: _InMemoryFirestore, name: str):
        self._fs = fs
        self._name = name

    def document(self, doc_id: str):
        return _DocRef(self._fs, f"{self._name}/{doc_id}", doc_id)


class _Snapshot:
    def __init__(self, exists: bool, data: Optional[dict], doc_id: str = ""):
        self.exists = exists
        self._data = data or {}
        self.id = doc_id

    def to_dict(self):
        import json
        return json.loads(json.dumps(self._data))


class _DocRef:
    def __init__(self, fs: _InMemoryFirestore, path: str, doc_id: str):
        self._fs = fs
        self._path = path
        self.id = doc_id

    def get(self, transaction=None):
        with self._fs._lock:
            data = self._fs._docs.get(self._path)
            return _Snapshot(data is not None, data, doc_id=self.id)

    def update(self, value: dict):
        with self._fs._lock:
            if self._path not in self._fs._docs:
                raise KeyError(f"Document {self._path} does not exist")
            target = self._fs._docs[self._path]
            for k, v in value.items():
                if "." in k:
                    parts = k.split(".")
                    curr = target
                    for p in parts[:-1]:
                        if p not in curr or not isinstance(curr[p], dict):
                            curr[p] = {}
                        curr = curr[p]
                    curr[parts[-1]] = v
                else:
                    target[k] = v


class _FakeTransaction:
    def __init__(self, fs: _InMemoryFirestore):
        self._fs = fs

    def update(self, doc_ref: _DocRef, value: dict):
        doc_ref.update(value)


@pytest.fixture
def fake_db(monkeypatch):
    db = _InMemoryFirestore()
    monkeypatch.setattr(acquisition, "get_firestore_client", lambda: db)
    def _fake_transactional(fn):
        def _wrapped(transaction, *args, **kwargs):
            with db._lock:
                return fn(transaction, *args, **kwargs)
        return _wrapped
    monkeypatch.setattr("google.cloud.firestore.transactional", _fake_transactional)
    return db


# =========================================================================
# 1. Country snapshot creation & validation probes
# =========================================================================

@pytest.mark.asyncio
async def test_country_snapshot_at_signup_creation(monkeypatch):
    """Verify country snapshot is captured in acquisition_measurement on eligible account creation."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    captured_doc = {}
    class _FakeCollectionRef:
        def add(self, data):
            captured_doc.update(data)
            class _FakeDocRef:
                id = "cnt_ca_test"
            return None, _FakeDocRef()

    class _FakeDb:
        def collection(self, name):
            return _FakeCollectionRef()

    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: _FakeDb())

    # Canadian phone number -> normalized effective_country CA
    cid = await create_contractor({
        "business_name": "Canada Services",
        "owner_name": "Test Owner",
        "owner_phone": "+14165550199",
        "country_code": "CA",
        "declared_onboarding_intent": "personal",
    })

    assert cid == "cnt_ca_test"
    acq = captured_doc.get("acquisition_measurement")
    assert acq is not None
    assert acq["account_country_at_signup"] == "CA"
    assert acq["declared_onboarding_intent"] == "personal"


@pytest.mark.asyncio
async def test_country_snapshot_disabled_no_map(monkeypatch):
    """Verify no map is created when acquisition measurement is disabled."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", False)

    captured_doc = {}
    class _FakeCollectionRef:
        def add(self, data):
            captured_doc.update(data)
            class _FakeDocRef:
                id = "cnt_disabled_test"
            return None, _FakeDocRef()

    class _FakeDb:
        def collection(self, name):
            return _FakeCollectionRef()

    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: _FakeDb())

    cid = await create_contractor({
        "business_name": "Disabled Co",
        "owner_name": "Test Owner",
        "owner_phone": "+14155550123",
        "declared_onboarding_intent": "personal",
    })

    assert cid == "cnt_disabled_test"
    assert "acquisition_measurement" not in captured_doc


def test_country_enum_validation():
    """Verify reducer accepts allowed country enums and rejects invalid or malformed types."""
    valid_countries = ["US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT"]
    base_record = {
        "schema_version": 1,
        "cohort": 1788960000.0,
        "created_at": 1788960000.0,
        "declared_onboarding_intent": "personal",
        "attempts": 0,
        "attribution_status": None,
    }

    # Missing / None country is valid (legacy unknown)
    assert validate_measurement_record(dict(base_record)) == base_record
    assert validate_measurement_record(dict(base_record, account_country_at_signup=None)) == dict(base_record, account_country_at_signup=None)

    # Valid countries
    for c in valid_countries:
        rec = dict(base_record, account_country_at_signup=c)
        assert validate_measurement_record(rec) == rec

    # Invalid country codes and types
    invalid_cases = ["us", "XX", "JP", "USA", True, False, 123, []]
    for bad in invalid_cases:
        with pytest.raises(ValueError) as excinfo:
            validate_measurement_record(dict(base_record, account_country_at_signup=bad))
        assert "account_country_at_signup" not in str(excinfo.value) or "valid enum" in str(excinfo.value)


# =========================================================================
# 2. Reducer grouping, conversion types, keyword grouping & as-of attribution
# =========================================================================

def test_reducer_grouping_by_country_conversion_and_keyword():
    """Verify funnel reducer groups by account country, conversion type, and optional keyword."""
    cohort_start = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    cohort_end = datetime.datetime(2026, 9, 30, 0, 0, 0, tzinfo=datetime.timezone.utc)
    as_of = datetime.datetime(2026, 9, 30, 12, 0, 0, tzinfo=datetime.timezone.utc)

    ts_1 = datetime.datetime(2026, 9, 10, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    ts_2 = datetime.datetime(2026, 9, 11, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    ts_3 = datetime.datetime(2026, 9, 12, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()

    records = [
        # Record 1: US, Personal, Apple Ads, Download, Keyword 555
        {
            "schema_version": 1,
            "cohort": ts_1,
            "created_at": ts_1,
            "account_country_at_signup": "US",
            "declared_onboarding_intent": "personal",
            "attempts": 1,
            "last_attempt_at": ts_1 + 10,
            "attribution_status": "recorded",
            "attribution": True,
            "org_id": 987654,
            "campaign_id": 100,
            "ad_group_id": 200,
            "keyword_id": 555,
            "conversion_type": "Download",
            "source": "apple_ads",
            "attribution_recorded_at": ts_1 + 20,
            "first_screening_conversation_observed_at": ts_1 + 100,
        },
        # Record 2: GB, Personal, Apple Ads, Redownload, Keyword 777
        {
            "schema_version": 1,
            "cohort": ts_2,
            "created_at": ts_2,
            "account_country_at_signup": "GB",
            "declared_onboarding_intent": "personal",
            "attempts": 1,
            "last_attempt_at": ts_2 + 10,
            "attribution_status": "recorded",
            "attribution": True,
            "org_id": 987654,
            "campaign_id": 100,
            "ad_group_id": 200,
            "keyword_id": 777,
            "conversion_type": "Redownload",
            "source": "apple_ads",
            "attribution_recorded_at": ts_2 + 20,
            "first_screening_conversation_observed_at": ts_2 + 100,
        },
        # Record 3: Legacy record (no country, no conversion type)
        {
            "schema_version": 1,
            "cohort": ts_3,
            "created_at": ts_3,
            "declared_onboarding_intent": "business",
            "attempts": 0,
            "attribution_status": None,
        },
    ]

    report = summarize_acquisition_funnel(
        records=records,
        cohort_start=cohort_start,
        cohort_end=cohort_end,
        as_of=as_of,
        by_keyword=True,
    )

    assert report["input_validation_complete"] is True
    assert report["source_population_complete"] is None
    assert report["input_uniqueness_verified"] is False
    assert report["input_record_count"] == 3
    assert report["totals"]["accounts_created"] == 3
    assert report["totals"]["screening_conversation_observed"] == 2

    # Verify group breakdown
    assert len(report["groups"]) == 3
    g_gb = next(g for g in report["groups"] if g["account_country_at_signup"] == "GB")
    assert g_gb["conversion_type"] == "Redownload"
    assert g_gb["keyword_id"] == 777
    assert g_gb["screening_conversation_observed"] == 1

    g_us = next(g for g in report["groups"] if g["account_country_at_signup"] == "US")
    assert g_us["conversion_type"] == "Download"
    assert g_us["keyword_id"] == 555
    assert g_us["screening_conversation_observed"] == 1

    g_legacy = next(g for g in report["groups"] if g["account_country_at_signup"] == "unknown")
    assert g_legacy["conversion_type"] == "unknown"
    assert g_legacy["keyword_id"] is None
    assert g_legacy["screening_conversation_observed"] == 0


def test_future_as_of_attribution_and_conversion_type_stays_unknown():
    """Verify that attribution occurring after as_of leaves source and conversion_type as unknown."""
    cohort_start = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    cohort_end = datetime.datetime(2026, 9, 15, 0, 0, 0, tzinfo=datetime.timezone.utc)
    as_of = datetime.datetime(2026, 9, 20, 0, 0, 0, tzinfo=datetime.timezone.utc)
    as_of_ts = as_of.timestamp()

    created_ts = datetime.datetime(2026, 9, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()

    records = [
        {
            "schema_version": 1,
            "cohort": created_ts,
            "created_at": created_ts,
            "account_country_at_signup": "CA",
            "declared_onboarding_intent": "personal",
            "attempts": 1,
            "last_attempt_at": as_of_ts + 10,
            "attribution_status": "recorded",
            "attribution": True,
            "org_id": 987654,
            "campaign_id": 111,
            "ad_group_id": 222,
            "conversion_type": "Download",
            "source": "apple_ads",
            "attribution_recorded_at": as_of_ts + 100,  # after as_of
            "first_screening_conversation_observed_at": created_ts + 50,  # before as_of
        }
    ]

    report = summarize_acquisition_funnel(
        records=records,
        cohort_start=cohort_start,
        cohort_end=cohort_end,
        as_of=as_of,
    )

    assert report["totals"]["accounts_created"] == 1
    assert report["totals"]["attribution_recorded"] == 0
    assert report["totals"]["screening_conversation_observed"] == 1

    group = report["groups"][0]
    assert group["account_country_at_signup"] == "CA"
    assert group["source"] == "unknown"
    assert group["conversion_type"] == "unknown"
    assert group["campaign_id"] is None


# =========================================================================
# 3. Screening conversation qualification & transcript parsing
# =========================================================================

def test_transcript_parsing_greeting_vs_caller_then_kevin():
    """Verify screening conversation parser requires Caller line followed by Kevin line."""
    # 1. Greeting only (Kevin line with no Caller) -> False
    assert acquisition.parse_qualifying_screening_conversation("Kevin: Hello, thanks for calling.") is False

    # 2. Greeting then Caller line, but no subsequent Kevin response -> False
    assert acquisition.parse_qualifying_screening_conversation(
        "Kevin: Hello, how can I help?\nCaller: I need an estimate."
    ) is False

    # 3. Greeting then Caller line then Kevin response -> True
    assert acquisition.parse_qualifying_screening_conversation(
        "Kevin: Hello, how can I help?\nCaller: I need an estimate.\nKevin: I'd be happy to help with that."
    ) is True

    # 4. Caller line first then Kevin line -> True
    assert acquisition.parse_qualifying_screening_conversation(
        "Caller: Is this Acme Plumbing?\nKevin: Yes, this is Kevin with Acme Plumbing."
    ) is True

    # 5. Caller line with only whitespace -> False
    assert acquisition.parse_qualifying_screening_conversation(
        "Kevin: Hello\nCaller:    \nKevin: I can't hear you."
    ) is False

    # 6. Kevin line with only whitespace -> False
    assert acquisition.parse_qualifying_screening_conversation(
        "Caller: Hello\nKevin:   "
    ) is False

    # 7. Exact case sensitivity check
    assert acquisition.parse_qualifying_screening_conversation(
        "caller: Hi\nkevin: Hello"
    ) is False


def test_transcript_overlong_bounds():
    """Verify transcript exceeding 64 KiB UTF-8 is rejected."""
    valid_short = "Caller: Hi\nKevin: Hello"
    assert acquisition.parse_qualifying_screening_conversation(valid_short) is True

    padding = "X" * (65 * 1024)
    overlong = f"Caller: Hi\nKevin: Hello\n{padding}"
    assert acquisition.parse_qualifying_screening_conversation(overlong) is False


def test_qualify_exact_route_and_status(monkeypatch):
    """Verify qualifier requires exact completed status and exact ai_screening route."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960500.0
    valid_call = {
        "call_status": "completed",
        "route_taken": "ai_screening",
        "transcript": "Caller: Hello\nKevin: Hi there",
        "timestamp": 1788960000.0,
        "ended_at": 1788960050.0,
    }

    # Valid completed + ai_screening
    ok, start, obs = acquisition.qualify_screening_conversation(valid_call, observed_at=now)
    assert ok is True
    assert start == 1788960000.0
    assert obs == now

    # Ineligible statuses
    for bad_status in ["in-progress", "failed", "busy", "no-answer", "canceled", "COMPLETED"]:
        bad_call = dict(valid_call, call_status=bad_status)
        assert acquisition.qualify_screening_conversation(bad_call, observed_at=now) == (False, None, None)

    # Ineligible routes
    for bad_route in ["voip_direct", "emergency", "after_hours", "ai_Screening", ""]:
        bad_call = dict(valid_call, route_taken=bad_route)
        assert acquisition.qualify_screening_conversation(bad_call, observed_at=now) == (False, None, None)


def test_timestamp_order_and_bounds_qualification(monkeypatch):
    """Verify qualification rejects corrupted, out-of-order, or future timestamps."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960500.0
    base_call = {
        "call_status": "completed",
        "route_taken": "ai_screening",
        "transcript": "Caller: Hello\nKevin: Hi",
        "timestamp": 1788960000.0,
        "ended_at": 1788960050.0,
    }

    # timestamp > ended_at -> rejected
    assert acquisition.qualify_screening_conversation(
        dict(base_call, timestamp=1788960100.0, ended_at=1788960050.0),
        observed_at=now,
    ) == (False, None, None)

    # ended_at > observed_at -> rejected
    assert acquisition.qualify_screening_conversation(
        dict(base_call, ended_at=now + 50.0),
        observed_at=now,
    ) == (False, None, None)

    # observed_at in future relative to clock -> rejected
    assert acquisition.qualify_screening_conversation(
        base_call,
        observed_at=time.time() + 1000.0,
    ) == (False, None, None)

    # Boolean timestamps -> rejected
    assert acquisition.qualify_screening_conversation(
        dict(base_call, timestamp=True),
        observed_at=now,
    ) == (False, None, None)


# =========================================================================
# 4. Zero-DB when disabled & Scheduler and Writer Probes
# =========================================================================

def test_screening_measurement_disabled_zero_db(monkeypatch):
    """Verify zero database calls or tasks when acquisition measurement is disabled."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", False)

    db_called = False
    def tripwire_db():
        nonlocal db_called
        db_called = True
        raise RuntimeError("DB accessed when disabled")

    monkeypatch.setattr(acquisition, "get_firestore_client", tripwire_db)

    call_record = {
        "call_status": "completed",
        "route_taken": "ai_screening",
        "transcript": "Caller: Hi\nKevin: Hello",
        "timestamp": 1788960000.0,
        "ended_at": 1788960050.0,
    }

    # Scheduler returns immediately
    acquisition.schedule_screening_conversation_measurement("cnt_1", call_record)
    assert len(acquisition._pending_screening_tasks) == 0
    assert db_called is False


@pytest.mark.asyncio
async def test_transactional_writer_prior_cohort_and_immutable_first_write(fake_db, monkeypatch):
    """Verify writer rejects prior-cohort calls and never overwrites an existing milestone."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    created_ts = 1788960000.0
    doc_path = "contractors/cnt_writer_test"
    fake_db._docs[doc_path] = {
        "active": True,
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": created_ts,
            "created_at": created_ts,
            "account_country_at_signup": "US",
            "declared_onboarding_intent": "personal",
            "attempts": 0,
            "first_screening_conversation_observed_at": None,
        },
    }

    # Prior-cohort call start (< created_ts) -> rejected
    await acquisition.record_screening_conversation_measurement(
        "cnt_writer_test",
        call_start=created_ts - 50.0,
        observed_at=created_ts + 10.0,
    )
    assert fake_db._docs[doc_path]["acquisition_measurement"]["first_screening_conversation_observed_at"] is None

    # First qualifying call -> recorded
    await acquisition.record_screening_conversation_measurement(
        "cnt_writer_test",
        call_start=created_ts + 100.0,
        observed_at=created_ts + 150.0,
    )
    assert fake_db._docs[doc_path]["acquisition_measurement"]["first_screening_conversation_observed_at"] == created_ts + 150.0

    # Second qualifying call -> first write is immutable
    await acquisition.record_screening_conversation_measurement(
        "cnt_writer_test",
        call_start=created_ts + 200.0,
        observed_at=created_ts + 250.0,
    )
    assert fake_db._docs[doc_path]["acquisition_measurement"]["first_screening_conversation_observed_at"] == created_ts + 150.0


@pytest.mark.asyncio
async def test_transactional_writer_inactive_or_malformed_map(fake_db, monkeypatch):
    """Verify writer rejects inactive contractors and corrupted measurement maps."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    created_ts = 1788960000.0

    # Inactive contractor
    fake_db._docs["contractors/cnt_inactive"] = {
        "active": False,
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": created_ts,
            "created_at": created_ts,
            "attempts": 0,
        },
    }
    await acquisition.record_screening_conversation_measurement(
        "cnt_inactive",
        call_start=created_ts + 10.0,
        observed_at=created_ts + 20.0,
    )
    assert "first_screening_conversation_observed_at" not in fake_db._docs["contractors/cnt_inactive"]["acquisition_measurement"]

    # Corrupted schema version
    fake_db._docs["contractors/cnt_bad_schema"] = {
        "active": True,
        "acquisition_measurement": {
            "schema_version": 999,
            "cohort": created_ts,
            "created_at": created_ts,
            "attempts": 0,
        },
    }
    await acquisition.record_screening_conversation_measurement(
        "cnt_bad_schema",
        call_start=created_ts + 10.0,
        observed_at=created_ts + 20.0,
    )
    assert "first_screening_conversation_observed_at" not in fake_db._docs["contractors/cnt_bad_schema"]["acquisition_measurement"]


# =========================================================================
# 5. Post-call handoff wiring, tenant mismatch, and partial delivery probes
# =========================================================================

@pytest.mark.asyncio
async def test_handoff_hook_partial_delivery_and_tenant_mismatch(monkeypatch):
    """Verify post_call_handoff schedules when call_record succeeds (even if SMS fails), and rejects tenant mismatches."""
    scheduled_calls = []

    def fake_schedule(contractor_id, call_record):
        scheduled_calls.append((contractor_id, call_record))

    monkeypatch.setattr(
        "app.services.acquisition.schedule_screening_conversation_measurement",
        fake_schedule,
    )

    now = time.time()
    monkeypatch.setattr(post_call_handoff.handoff_db, "claim_handoff", lambda sid: asyncio.sleep(0, result=True))
    monkeypatch.setattr(post_call_handoff.handoff_db, "finish_handoff", lambda sid, res: asyncio.sleep(0, result=True))
    monkeypatch.setattr(post_call_handoff.call_db, "save_call", lambda sid, updates: asyncio.sleep(0, result=True))
    monkeypatch.setattr(post_call_handoff, "_mirror_status", lambda *a, **kw: asyncio.sleep(0, result=True))

    # 1. Partial delivery allowed: call_record completed, owner_sms failed -> MUST schedule
    monkeypatch.setattr(
        post_call_handoff.handoff_db,
        "get_handoff",
        lambda sid: asyncio.sleep(0, result={"contractor_id": "cnt_1", "status": "pending", "created_at": now}),
    )
    durable_call = {
        "call_sid": "CA_partial",
        "contractor_id": "cnt_1",
        "call_status": "completed",
        "route_taken": "ai_screening",
        "transcript": "Caller: Need help\nKevin: On my way",
        "caller_phone": "+15555550100",
        "timestamp": now - 30,
        "ended_at": now - 10,
    }
    monkeypatch.setattr(post_call_handoff.call_db, "get_call", lambda sid: asyncio.sleep(0, result=durable_call))

    async def fake_partial_process(*args, **kwargs):
        return post_call.PostCallResult(
            status="partial",
            completed_effects=("summary", "call_record"),
            failed_effects=("owner_sms",),  # SMS failed, call_record completed
        )

    monkeypatch.setattr(post_call_handoff, "process_post_call", fake_partial_process)

    status = await post_call_handoff.run_post_call_handoff(
        "CA_partial",
        transcript_lines=["Caller: Need help", "Kevin: On my way"],
        caller_phone="+15555550100",
        contractor_phone="+15555550101",
        twilio_number="+15555550102",
        contractor={"contractor_id": "cnt_1"},
    )
    assert status == "needs_attention"
    assert len(scheduled_calls) == 1
    assert scheduled_calls[0][0] == "cnt_1"
    assert scheduled_calls[0][1] == durable_call

    scheduled_calls.clear()

    # 2. Failed call_record effect -> MUST NOT schedule
    async def fake_failed_call_record(*args, **kwargs):
        return post_call.PostCallResult(
            status="partial",
            completed_effects=("summary", "owner_sms"),
            failed_effects=("call_record",),
        )

    monkeypatch.setattr(post_call_handoff, "process_post_call", fake_failed_call_record)
    await post_call_handoff.run_post_call_handoff(
        "CA_partial",
        transcript_lines=["Caller: Need help", "Kevin: On my way"],
        caller_phone="+15555550100",
        contractor_phone="+15555550101",
        twilio_number="+15555550102",
        contractor={"contractor_id": "cnt_1"},
    )
    assert len(scheduled_calls) == 0

    # 3. Tenant mismatch between handoff and call_record -> quarantined, not scheduled
    monkeypatch.setattr(
        post_call_handoff.handoff_db,
        "get_handoff",
        lambda sid: asyncio.sleep(0, result={"contractor_id": "cnt_A", "status": "pending", "created_at": now}),
    )
    mismatch_call = dict(durable_call, contractor_id="cnt_B")
    monkeypatch.setattr(post_call_handoff.call_db, "get_call", lambda sid: asyncio.sleep(0, result=mismatch_call))
    monkeypatch.setattr(post_call_handoff.handoff_db, "quarantine_pending_handoff", lambda sid, code: asyncio.sleep(0, result=True))

    mismatch_status = await post_call_handoff.run_post_call_handoff(
        "CA_mismatch",
        transcript_lines=["Caller: Need help", "Kevin: On my way"],
        caller_phone="+15555550100",
        contractor_phone="+15555550101",
        twilio_number="+15555550102",
        contractor={"contractor_id": "cnt_A"},
    )
    assert mismatch_status == "needs_attention"
    assert len(scheduled_calls) == 0


@pytest.mark.asyncio
async def test_held_writer_does_not_delay_handoff(monkeypatch):
    """Verify held background writer task does not block or delay synchronous handoff completion."""
    writer_entered = asyncio.Event()
    writer_release = asyncio.Event()

    async def fake_held_writer(contractor_id, call_start, observed_at):
        writer_entered.set()
        await writer_release.wait()

    monkeypatch.setattr(acquisition, "record_screening_conversation_measurement", fake_held_writer)
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = time.time()
    call_record = {
        "call_sid": "CA_held_test",
        "contractor_id": "cnt_held",
        "call_status": "completed",
        "route_taken": "ai_screening",
        "transcript": "Caller: Hi\nKevin: Hello",
        "caller_phone": "+15555550100",
        "timestamp": now - 30,
        "ended_at": now - 10,
    }

    monkeypatch.setattr(post_call_handoff.handoff_db, "get_handoff", lambda sid: asyncio.sleep(0, result={"contractor_id": "cnt_held", "status": "pending", "created_at": now}))
    monkeypatch.setattr(post_call_handoff.call_db, "get_call", lambda sid: asyncio.sleep(0, result=call_record))
    monkeypatch.setattr(post_call_handoff.handoff_db, "claim_handoff", lambda sid: asyncio.sleep(0, result=True))
    monkeypatch.setattr(post_call_handoff.handoff_db, "finish_handoff", lambda sid, res: asyncio.sleep(0, result=True))
    monkeypatch.setattr(post_call_handoff.call_db, "save_call", lambda sid, updates: asyncio.sleep(0, result=True))
    monkeypatch.setattr(post_call_handoff, "_mirror_status", lambda *a, **kw: asyncio.sleep(0, result=True))

    async def fake_process(*args, **kwargs):
        return post_call.PostCallResult(
            status="complete",
            completed_effects=("summary", "call_record"),
            failed_effects=(),
        )

    monkeypatch.setattr(post_call_handoff, "process_post_call", fake_process)

    try:
        # run_post_call_handoff should return immediately without waiting for writer_release
        task = asyncio.create_task(
            post_call_handoff.run_post_call_handoff(
                "CA_held_test",
                transcript_lines=["Caller: Hi", "Kevin: Hello"],
                caller_phone="+15555550100",
                contractor_phone="+15555550101",
                twilio_number="+15555550102",
                contractor={"contractor_id": "cnt_held"},
            )
        )
        status = await asyncio.wait_for(task, timeout=1.0)
        assert status == "completed"

        # Background task should be entered
        await asyncio.wait_for(writer_entered.wait(), timeout=1.0)
        assert len(acquisition._pending_screening_tasks) == 1
    finally:
        # Guarantee cleanup via finally
        writer_release.set()
        if acquisition._pending_screening_tasks:
            await asyncio.gather(*list(acquisition._pending_screening_tasks), return_exceptions=True)
        assert len(acquisition._pending_screening_tasks) == 0


# =========================================================================
# 6. Additional tenant, transcript qualification, lifecycle & loop probes
# =========================================================================

@pytest.mark.asyncio
async def test_screening_conversation_rejects_missing_mismatched_and_whitespace_contractor_ids(monkeypatch):
    """Verify scheduler and writer drop tasks with missing, whitespace, nonstring, or mismatched contractor IDs."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960500.0
    valid_call = {
        "call_status": "completed",
        "route_taken": "ai_screening",
        "transcript": "Caller: Hi\nKevin: Hello",
        "timestamp": 1788960000.0,
        "ended_at": 1788960050.0,
        "contractor_id": "cnt_valid",
    }

    # 1. Non-string or whitespace contractor_id argument -> no task
    bad_cids = [None, 12345, True, False, " cnt_valid", "cnt_valid ", "   ", ""]
    for bad_cid in bad_cids:
        acquisition.schedule_screening_conversation_measurement(bad_cid, valid_call, observed_at=now)
        assert len(acquisition._pending_screening_tasks) == 0

    # 2. Contractor ID mismatch between argument and call_record -> no task
    mismatched_call = dict(valid_call, contractor_id="cnt_other")
    acquisition.schedule_screening_conversation_measurement("cnt_valid", mismatched_call, observed_at=now)
    assert len(acquisition._pending_screening_tasks) == 0

    # 3. Nonstring or whitespace contractor_id inside call_record -> no task
    for bad_inner_cid in [None, 12345, True, " cnt_valid", "cnt_valid "]:
        bad_inner_call = dict(valid_call, contractor_id=bad_inner_cid)
        acquisition.schedule_screening_conversation_measurement("cnt_valid", bad_inner_call, observed_at=now)
        assert len(acquisition._pending_screening_tasks) == 0

    # 4. Valid matching contractor_id -> 1 task enqueued
    release_event = asyncio.Event()
    async def fake_held(contractor_id, call_start, observed_at):
        await release_event.wait()

    monkeypatch.setattr(acquisition, "record_screening_conversation_measurement", fake_held)

    try:
        acquisition.schedule_screening_conversation_measurement("cnt_valid", valid_call, observed_at=now)
        assert len(acquisition._pending_screening_tasks) == 1
    finally:
        release_event.set()
        if acquisition._pending_screening_tasks:
            await asyncio.gather(*list(acquisition._pending_screening_tasks), return_exceptions=True)
        assert len(acquisition._pending_screening_tasks) == 0


@pytest.mark.asyncio
async def test_convincing_inline_transcript_with_durable_empty_or_greeting_only_not_counted(monkeypatch):
    """Verify scheduler evaluates durable decrypted transcript, not inline transcript lines."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = time.time()

    async def fake_claim(sid):
        return True
    async def fake_finish(sid, res):
        return True
    async def fake_save(sid, updates):
        return True

    monkeypatch.setattr(post_call_handoff.handoff_db, "claim_handoff", fake_claim)
    monkeypatch.setattr(post_call_handoff.handoff_db, "finish_handoff", fake_finish)
    monkeypatch.setattr(post_call_handoff.call_db, "save_call", fake_save)
    monkeypatch.setattr(post_call_handoff, "_mirror_status", lambda *a, **kw: asyncio.sleep(0, result=True))

    async def fake_process(*args, **kwargs):
        return post_call.PostCallResult(
            status="complete",
            completed_effects=("summary", "call_record"),
            failed_effects=(),
        )
    monkeypatch.setattr(post_call_handoff, "process_post_call", fake_process)

    convincing_inline = [
        "Caller: Hello, I need emergency plumbing service.",
        "Kevin: We can dispatch a technician right away. What is your address?",
        "Caller: 123 Main Street.",
        "Kevin: Got it, technician is on the way.",
    ]

    invalid_durable_transcripts = [
        "",                                                # empty
        "   ",                                             # whitespace
        "Kevin: Thanks for calling Acme Plumbing.",        # greeting only
        "Kevin: Hello\nCaller: Hi",                        # greeting then caller, no Kevin response
    ]

    for durable_t in invalid_durable_transcripts:
        durable_call = {
            "call_sid": "CA_convincing_test",
            "contractor_id": "cnt_test",
            "call_status": "completed",
            "route_taken": "ai_screening",
            "transcript": durable_t,
            "caller_phone": "+15555550100",
            "timestamp": now - 30,
            "ended_at": now - 10,
        }
        monkeypatch.setattr(post_call_handoff.handoff_db, "get_handoff", lambda sid: asyncio.sleep(0, result={"contractor_id": "cnt_test", "status": "pending", "created_at": now}))
        monkeypatch.setattr(post_call_handoff.call_db, "get_call", lambda sid: asyncio.sleep(0, result=durable_call))

        status = await post_call_handoff.run_post_call_handoff(
            "CA_convincing_test",
            transcript_lines=convincing_inline,
            caller_phone="+15555550100",
            contractor_phone="+15555550101",
            twilio_number="+15555550102",
            contractor={"contractor_id": "cnt_test"},
        )
        assert status == "completed"
        # Zero tasks queued because durable record transcript failed qualification
        assert len(acquisition._pending_screening_tasks) == 0


@pytest.mark.asyncio
async def test_screening_scheduler_cancellation_and_exception_cleanup(monkeypatch):
    """Verify screening scheduler done callback safely removes tasks on exception and cancellation."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    # 1. Exception cleanup
    async def fake_throw(contractor_id, call_start, observed_at):
        raise RuntimeError("DB pool failure")

    monkeypatch.setattr(acquisition, "record_screening_conversation_measurement", fake_throw)
    call_rec = {
        "contractor_id": "cnt_throw",
        "call_status": "completed",
        "route_taken": "ai_screening",
        "transcript": "Caller: Hi\nKevin: Hello",
        "timestamp": 1788960000.0,
        "ended_at": 1788960050.0,
    }
    acquisition.schedule_screening_conversation_measurement("cnt_throw", call_rec, observed_at=1788960100.0)
    await asyncio.gather(*list(acquisition._pending_screening_tasks), return_exceptions=True)
    assert len(acquisition._pending_screening_tasks) == 0

    # 2. Cancellation cleanup
    release_event = asyncio.Event()
    async def fake_held(contractor_id, call_start, observed_at):
        await release_event.wait()

    monkeypatch.setattr(acquisition, "record_screening_conversation_measurement", fake_held)
    call_cancel = dict(call_rec, contractor_id="cnt_cancel")
    acquisition.schedule_screening_conversation_measurement("cnt_cancel", call_cancel, observed_at=1788960100.0)
    assert len(acquisition._pending_screening_tasks) == 1
    task = next(iter(acquisition._pending_screening_tasks))
    task.cancel()
    release_event.set()
    await asyncio.gather(*list(acquisition._pending_screening_tasks), return_exceptions=True)
    assert len(acquisition._pending_screening_tasks) == 0


def test_screening_scheduler_no_running_loop(monkeypatch):
    """Verify schedule_screening_conversation_measurement safely handles absent event loop without error."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    def _no_loop():
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(asyncio, "get_running_loop", _no_loop)

    call_rec = {
        "contractor_id": "cnt_no_loop",
        "call_status": "completed",
        "route_taken": "ai_screening",
        "transcript": "Caller: Hi\nKevin: Hello",
        "timestamp": 1788960000.0,
        "ended_at": 1788960050.0,
    }
    # Must close coroutine and return without raising
    acquisition.schedule_screening_conversation_measurement("cnt_no_loop", call_rec, observed_at=1788960100.0)
    assert len(acquisition._pending_screening_tasks) == 0


@pytest.mark.asyncio
async def test_screening_writer_rejects_future_observation_and_whitespace_ids_before_db(monkeypatch):
    """Verify screening writer rejects future observations and whitespace IDs before touching DB."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    def tripwire_db():
        raise RuntimeError("DB accessed unexpectedly")

    monkeypatch.setattr(acquisition, "get_firestore_client", tripwire_db)

    now = time.time()
    # 1. Future observed_at (> now) -> rejected before DB
    await acquisition.record_screening_conversation_measurement("cnt_1", now - 50.0, observed_at=now + 1000.0)

    # 2. Whitespace contractor_id -> rejected before DB
    await acquisition.record_screening_conversation_measurement(" cnt_1", now - 50.0, observed_at=now)
    await acquisition.record_screening_conversation_measurement("cnt_1 ", now - 50.0, observed_at=now)
    await acquisition.record_screening_conversation_measurement("   ", now - 50.0, observed_at=now)

    # 3. Non-string contractor_id -> rejected before DB
    await acquisition.record_screening_conversation_measurement(12345, now - 50.0, observed_at=now)
    await acquisition.record_screening_conversation_measurement(True, now - 50.0, observed_at=now)
