"""Unit tests for acquisition measurement wiring, security redaction, subscription hooks, and strict offline reducer."""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import asyncio
import base64
import datetime
import json
import time
from typing import Any, Dict
import pytest
from pydantic import ValidationError

from app.api.contractors import ContractorCreate, _SENSITIVE_KEYS, _redact_contractor
from app.db.contractors import PROTECTED_FIELDS, create_contractor
from app.services import acquisition
from app.services import subscription
from app.services.subscription import (
    CrossContractorReceiptError,
    SubscriptionUpdateOutcome,
    SubscriptionUpdateResult,
    handle_appstore_notification,
    update_subscription_from_transaction,
)
from app.webhooks import twilio_incoming
from scripts.summarize_acquisition_funnel import summarize_acquisition_funnel, validate_measurement_record


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


def _unsigned_jws(payload: dict) -> str:
    header = {"alg": "ES256"}

    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode(header)}.{encode(payload)}.signature"


def test_contractor_create_declared_onboarding_intent_validation():
    """Verify ContractorCreate accepts personal/business and None, rejects others."""
    c1 = ContractorCreate(owner_name="Test", business_name="Test Co", declared_onboarding_intent="personal")
    assert c1.declared_onboarding_intent == "personal"

    c2 = ContractorCreate(owner_name="Test", business_name="Test Co", declared_onboarding_intent="business")
    assert c2.declared_onboarding_intent == "business"

    c3 = ContractorCreate(owner_name="Test", business_name="Test Co")
    assert c3.declared_onboarding_intent is None

    with pytest.raises(ValidationError):
        ContractorCreate(owner_name="Test", business_name="Test Co", declared_onboarding_intent="enterprise")


def test_acquisition_measurement_protected_and_redacted():
    """Verify acquisition_measurement is in PROTECTED_FIELDS and _SENSITIVE_KEYS."""
    assert "acquisition_measurement" in PROTECTED_FIELDS
    assert "acquisition_measurement" in _SENSITIVE_KEYS

    # Verify redaction removes the map
    doc = {
        "contractor_id": "cnt_1",
        "business_name": "Test Co",
        "acquisition_measurement": {"schema_version": 1},
    }
    redacted = _redact_contractor(doc)
    assert "acquisition_measurement" not in redacted


def test_validate_measurement_record_rejects_unknown_fields_without_echoing():
    """Verify reducer validator rejects any unexpected root key without echoing private values."""
    valid_rec = {
        "schema_version": 1,
        "cohort": 1788960000.0,
        "created_at": 1788960000.0,
        "declared_onboarding_intent": "personal",
        "attempts": 1,
        "last_attempt_at": 1788960010.0,
        "attribution_status": None,
    }
    assert validate_measurement_record(valid_rec) == valid_rec

    invalid_rec = dict(valid_rec)
    sentinel = "SECRET_USER_PII_12345"
    invalid_rec["private_phone_number"] = sentinel

    with pytest.raises(ValueError) as excinfo:
        validate_measurement_record(invalid_rec)

    # Must NOT echo the key or value in the exception string
    assert sentinel not in str(excinfo.value)
    assert "private_phone_number" not in str(excinfo.value)


def test_validate_measurement_record_requires_paired_fields():
    """Verify missing paired fields (e.g. tier or signed timestamp) are rejected."""
    # Missing signed date for positive price purchase
    bad_rec = {
        "schema_version": 1,
        "cohort": 1788960000.0,
        "created_at": 1788960000.0,
        "declared_onboarding_intent": "personal",
        "attempts": 0,
        "attribution_status": None,
        "first_positive_price_purchase_observed_at": 1788960100.0,
        "first_positive_price_purchase_tier": "personal",
        # missing first_positive_price_purchase_signed_at
    }
    with pytest.raises(ValueError, match="positive price purchase fields must be paired"):
        validate_measurement_record(bad_rec)


def test_summarize_acquisition_funnel_aggregates_synthetic_cohort():
    """Verify funnel reducer correctly aggregates counts for an immutable 2026 cohort."""
    cohort_start = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    cohort_end = datetime.datetime(2026, 9, 30, 0, 0, 0, tzinfo=datetime.timezone.utc)
    as_of = datetime.datetime(2026, 9, 30, 12, 0, 0, tzinfo=datetime.timezone.utc)

    # Exact epoch generated from timezone-aware datetime(2026, 9, 10)
    ts_sept10 = datetime.datetime(2026, 9, 10, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    ts_sept11 = datetime.datetime(2026, 9, 11, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()

    records = [
        {
            "schema_version": 1,
            "cohort": ts_sept10,
            "created_at": ts_sept10,
            "declared_onboarding_intent": "personal",
            "attempts": 1,
            "last_attempt_at": ts_sept10 + 10.0,
            "attribution_status": "recorded",
            "attribution": True,
            "org_id": 987654,
            "campaign_id": 111,
            "ad_group_id": 222,
            "source": "apple_ads",
            "attribution_recorded_at": ts_sept10 + 20.0,
            "first_inbound_observed_at": ts_sept10 + 100.0,
            "first_forwarded_observed_at": ts_sept10 + 200.0,
            "first_verified_entitlement_observed_at": ts_sept10 + 300.0,
            "first_verified_entitlement_tier": "personal",
            "first_positive_price_purchase_observed_at": ts_sept10 + 400.0,
            "first_positive_price_purchase_signed_at": ts_sept10 + 400.0,
            "first_positive_price_purchase_tier": "personal",
        },
        {
            "schema_version": 1,
            "cohort": ts_sept11,
            "created_at": ts_sept11,
            "declared_onboarding_intent": "business",
            "attempts": 1,
            "last_attempt_at": ts_sept11 + 10.0,
            "attribution_status": "unattributed",
            "attribution": False,
            "source": "unattributed",
            "attribution_recorded_at": ts_sept11 + 20.0,
            "first_forwarded_observed_at": ts_sept11 + 200.0,
        },
        {
            "schema_version": 1,
            "cohort": 1700000000.0,  # outside Sept 2026 cohort window
            "created_at": 1700000000.0,
            "declared_onboarding_intent": "personal",
            "attempts": 0,
            "attribution_status": None,
        },
    ]

    result = summarize_acquisition_funnel(
        records=records,
        cohort_start=cohort_start,
        cohort_end=cohort_end,
        as_of=as_of,
    )

    assert result["complete"] is True
    assert result["input_validation_complete"] is True
    assert result["source_population_complete"] is None
    assert result["input_uniqueness_verified"] is False
    assert result["input_record_count"] == len(records)
    assert result["totals"]["accounts_created"] == 2
    assert result["totals"]["attribution_recorded"] == 1
    assert result["totals"]["inbound_observed"] == 1
    assert result["totals"]["forwarding_confirmed"] == 2
    assert result["totals"]["screening_conversation_observed"] == 0
    assert result["totals"]["verified_entitlement_observed"] == 1
    assert result["totals"]["positive_price_purchase_observed"] == 1
    assert result["totals"]["paid_tiers"]["personal"] == 1
    assert result["totals"]["paid_tiers"]["business"] == 0


def test_summarize_acquisition_funnel_as_of_bounds_and_restore():
    """Verify events observed after as_of are not counted and paid restore signed before creation is supported."""
    cohort_start = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    cohort_end = datetime.datetime(2026, 9, 15, 0, 0, 0, tzinfo=datetime.timezone.utc)
    as_of = datetime.datetime(2026, 9, 20, 0, 0, 0, tzinfo=datetime.timezone.utc)
    as_of_ts = as_of.timestamp()

    created_ts = datetime.datetime(2026, 9, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    signed_before_creation = created_ts - 3600.0  # signed 1 hour before account creation (restore)

    records = [
        {
            "schema_version": 1,
            "cohort": created_ts,
            "created_at": created_ts,
            "declared_onboarding_intent": "personal",
            "attempts": 1,
            "last_attempt_at": as_of_ts + 100,
            "attribution_status": "recorded",
            "attribution": True,
            "org_id": 987654,
            "campaign_id": 111,
            "ad_group_id": 222,
            "source": "apple_ads",
            "attribution_recorded_at": as_of_ts + 200,  # after as_of -> stays unknown
            "first_positive_price_purchase_observed_at": created_ts + 50,  # observed before as_of
            "first_positive_price_purchase_signed_at": signed_before_creation,  # signed before creation
            "first_positive_price_purchase_tier": "personal",
        }
    ]

    result = summarize_acquisition_funnel(
        records=records,
        cohort_start=cohort_start,
        cohort_end=cohort_end,
        as_of=as_of,
    )

    assert result["totals"]["accounts_created"] == 1
    assert result["totals"]["attribution_recorded"] == 0
    assert result["totals"]["positive_price_purchase_observed"] == 1
    # Group source must be "unknown" because attribution was recorded after as_of
    assert result["groups"][0]["source"] == "unknown"
    assert result["groups"][0]["campaign_id"] is None


@pytest.mark.asyncio
async def test_create_contractor_initializes_unknown_intent_and_roundtrips_reducer(monkeypatch):
    """Verify real create_contractor strips intent from root, initializes map, and round-trips reducer."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    captured_doc = {}
    class _FakeCollectionRef:
        def add(self, data):
            captured_doc.update(data)
            class _FakeDocRef:
                id = "cnt_created_1"
            return None, _FakeDocRef()

    class _FakeDb:
        def collection(self, name):
            return _FakeCollectionRef()

    monkeypatch.setattr("app.db.contractors.get_firestore_client", lambda: _FakeDb())

    cid = await create_contractor({
        "business_name": "Test Co",
        "owner_name": "Test Owner",
        "owner_phone": "+14155550123",  # Valid North American reserved test phone
        "declared_onboarding_intent": None,  # omitted / unknown
    })

    assert cid == "cnt_created_1"
    assert "declared_onboarding_intent" not in captured_doc  # stripped from root

    acq = captured_doc.get("acquisition_measurement")
    assert acq is not None
    assert acq["account_country_at_signup"] == "US"
    assert acq["declared_onboarding_intent"] == "unknown"
    assert acq["attempts"] == 0
    assert acq["attribution_status"] is None

    # Validate with reducer
    validated = validate_measurement_record(acq)
    assert validated["account_country_at_signup"] == "US"
    assert validated["declared_onboarding_intent"] == "unknown"


@pytest.mark.parametrize("helper_name, helper_fn, legacy_field, measurement_attr", [
    ("inbound", twilio_incoming._record_inbound_call_evidence, "last_inbound_call_at", "record_inbound_call_measurement"),
    ("forwarding", twilio_incoming._record_forwarding_evidence, "forwarding_last_seen_at", "record_forwarded_call_measurement"),
])
@pytest.mark.asyncio
async def test_twilio_evidence_helpers_order_with_held_measurement(
    monkeypatch, helper_name, helper_fn, legacy_field, measurement_attr
):
    """Verify legacy timestamp update completes before optional measurement enters, even when measurement is held."""
    legacy_updates = []
    entered_event = asyncio.Event()
    release_event = asyncio.Event()

    async def fake_get_contractor(cid):
        return {"contractor_id": cid, legacy_field: 0}

    async def fake_update_contractor(cid, updates):
        legacy_updates.append((cid, updates))
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_contractor)

    async def fake_held_measurement(cid, seen_at):
        # Assert legacy update already completed before measurement entered
        assert len(legacy_updates) == 1
        assert legacy_updates[0] == (cid, {legacy_field: seen_at})
        entered_event.set()
        await release_event.wait()

    monkeypatch.setattr(f"app.services.acquisition.{measurement_attr}", fake_held_measurement)

    task = asyncio.create_task(helper_fn("cnt_1", 5000.0))
    try:
        await asyncio.wait_for(entered_event.wait(), timeout=1.0)
        assert len(legacy_updates) == 1
    finally:
        release_event.set()
        await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.parametrize("helper_name, helper_fn, legacy_field, measurement_attr", [
    ("inbound", twilio_incoming._record_inbound_call_evidence, "last_inbound_call_at", "record_inbound_call_measurement"),
    ("forwarding", twilio_incoming._record_forwarding_evidence, "forwarding_last_seen_at", "record_forwarded_call_measurement"),
])
@pytest.mark.asyncio
async def test_twilio_evidence_helpers_recent_stamp_throttle_and_thrown_measurement(
    monkeypatch, helper_name, helper_fn, legacy_field, measurement_attr
):
    """Verify recent legacy stamp skips duplicate write while measurement still runs, and thrown measurement isolates legacy stamp."""
    # 1. Recent stamp throttle (seen_at - previous < 3600): skips legacy write, measurement still executes
    legacy_updates = []
    measurement_calls = []

    async def fake_get_contractor_recent(cid):
        return {"contractor_id": cid, legacy_field: 4500.0}

    async def fake_update_contractor(cid, updates):
        legacy_updates.append((cid, updates))
        return True

    async def fake_measurement(cid, seen_at):
        measurement_calls.append((cid, seen_at))

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor_recent)
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_contractor)
    monkeypatch.setattr(f"app.services.acquisition.{measurement_attr}", fake_measurement)

    await helper_fn("cnt_1", 5000.0)
    assert len(legacy_updates) == 0  # throttled
    assert len(measurement_calls) == 1  # measurement still ran
    assert measurement_calls[0] == ("cnt_1", 5000.0)

    # 2. Thrown measurement isolates legacy stamp
    legacy_updates.clear()
    measurement_calls.clear()

    async def fake_get_contractor_old(cid):
        return {"contractor_id": cid, legacy_field: 0}

    async def fake_measurement_throw(cid, seen_at):
        raise RuntimeError("Measurement write dropped")

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor_old)
    monkeypatch.setattr(f"app.services.acquisition.{measurement_attr}", fake_measurement_throw)

    await helper_fn("cnt_2", 5000.0)
    assert len(legacy_updates) == 1
    assert legacy_updates[0] == ("cnt_2", {legacy_field: 5000.0})


@pytest.mark.parametrize("helper_name, helper_fn, legacy_field, measurement_attr", [
    ("inbound", twilio_incoming._record_inbound_call_evidence, "last_inbound_call_at", "record_inbound_call_measurement"),
    ("forwarding", twilio_incoming._record_forwarding_evidence, "forwarding_last_seen_at", "record_forwarded_call_measurement"),
])
@pytest.mark.asyncio
async def test_twilio_evidence_helpers_cancellation_during_held_lookup_prevents_measurement(
    monkeypatch, helper_name, helper_fn, legacy_field, measurement_attr
):
    """Verify cancelling helper during held contractor lookup cancels promptly and starts no measurement."""
    lookup_entered = asyncio.Event()
    lookup_release = asyncio.Event()
    measurement_called = []

    async def fake_held_get(cid):
        lookup_entered.set()
        await lookup_release.wait()
        return {"contractor_id": cid, legacy_field: 0}

    async def fake_measurement(cid, seen_at):
        measurement_called.append((cid, seen_at))

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_held_get)
    monkeypatch.setattr(f"app.services.acquisition.{measurement_attr}", fake_measurement)

    task = asyncio.create_task(helper_fn("cnt_cancel_lookup", 5000.0))
    await asyncio.wait_for(lookup_entered.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(measurement_called) == 0


@pytest.mark.parametrize("helper_name, helper_fn, legacy_field, measurement_attr", [
    ("inbound", twilio_incoming._record_inbound_call_evidence, "last_inbound_call_at", "record_inbound_call_measurement"),
    ("forwarding", twilio_incoming._record_forwarding_evidence, "forwarding_last_seen_at", "record_forwarded_call_measurement"),
])
@pytest.mark.asyncio
async def test_twilio_evidence_helpers_cancellation_during_held_update_prevents_measurement(
    monkeypatch, helper_name, helper_fn, legacy_field, measurement_attr
):
    """Verify cancelling helper during held contractor update cancels promptly and starts no measurement."""
    update_entered = asyncio.Event()
    update_release = asyncio.Event()
    measurement_called = []

    async def fake_get(cid):
        return {"contractor_id": cid, legacy_field: 0}

    async def fake_held_update(cid, updates):
        update_entered.set()
        await update_release.wait()
        return True

    async def fake_measurement(cid, seen_at):
        measurement_called.append((cid, seen_at))

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get)
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_held_update)
    monkeypatch.setattr(f"app.services.acquisition.{measurement_attr}", fake_measurement)

    task = asyncio.create_task(helper_fn("cnt_cancel_update", 5000.0))
    await asyncio.wait_for(update_entered.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(measurement_called) == 0


# ---------------------------------------------------------------------------
# In-memory Firestore fake for actual _purge_sync execution
# ---------------------------------------------------------------------------


class _PurgeFakeDoc:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    @property
    def id(self):
        return self._path.split("/")[-1]

    def collection(self, name):
        return _PurgeFakeCollection(self._store, f"{self._path}/{name}")

    def get(self):
        data = self._store.docs.get(self._path)
        return _PurgeFakeSnapshot(self._path, data)

    def set(self, value, merge=False):
        if merge and self._path in self._store.docs:
            self._store.docs[self._path].update(value)
        else:
            self._store.docs[self._path] = dict(value)

    def update(self, value):
        self._store.docs[self._path].update(value)

    def delete(self):
        self._store.docs.pop(self._path, None)

    @property
    def reference(self):
        return self


class _PurgeFakeSnapshot:
    def __init__(self, path, data):
        self._path = path
        self._data = data
        self.exists = data is not None
        self.id = path.split("/")[-1]
        self.reference = None

    def to_dict(self):
        return dict(self._data) if self._data else None


class _PurgeFakeCollection:
    def __init__(self, store, path, filters=(), limit=None):
        self._store = store
        self._path = path
        self._filters = filters
        self._limit = limit

    def document(self, doc_id):
        return _PurgeFakeDoc(self._store, f"{self._path}/{doc_id}")

    def where(self, filter=None):
        return _PurgeFakeCollection(
            self._store, self._path, self._filters + (filter,), self._limit
        )

    def limit(self, n):
        return _PurgeFakeCollection(self._store, self._path, self._filters, n)

    def list_documents(self):
        """Enumerate direct children INCLUDING phantom parents with descendants."""
        depth = self._path.count("/") + 2
        ids = set()
        for path in self._store.docs:
            if not path.startswith(self._path + "/"):
                continue
            parts = path.split("/")
            want = self._path.count("/") + 1
            ids.add(parts[want])
        return [_PurgeFakeDoc(self._store, f"{self._path}/{i}") for i in sorted(ids)]

    def stream(self, **_kwargs):
        depth = self._path.count("/") + 2
        out = []
        for path, data in sorted(self._store.docs.items()):
            if not path.startswith(self._path + "/"):
                continue
            if path.count("/") + 1 != depth:
                continue
            ok = True
            for f in self._filters:
                field, op, val = f.field_path, f.op_string, f.value
                have = data.get(field)
                if op == "==":
                    ok = have == val
                elif op == "<":
                    ok = have is not None and have < val
                else:
                    raise NotImplementedError(op)
                if not ok:
                    break
            if ok:
                snap = _PurgeFakeSnapshot(path, data)
                snap.reference = _PurgeFakeDoc(self._store, path)
                out.append(snap)
                if self._limit and len(out) >= self._limit:
                    break
        return iter(out)


class _PurgeFakeBatch:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def delete(self, ref):
        self._ops.append(ref)

    def commit(self):
        for ref in self._ops:
            ref.delete()
        self._ops = []


class _PurgeFakeDb:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _PurgeFakeCollection(self, name)

    def batch(self):
        return _PurgeFakeBatch(self)


def test_purge_sync_removes_acquisition_measurement(monkeypatch):
    """Verify executing real _purge_sync on a deactivated contractor strips acquisition_measurement and sets minimal tombstone."""
    from app.db.purge import _purge_sync, TOMBSTONE_FIELDS

    db = _PurgeFakeDb()
    contractor_id = "cnt_purge_test"
    db.docs[f"contractors/{contractor_id}"] = {
        "active": False,
        "business_name": "Purged Business",
        "owner_name": "Purged Owner",
        "owner_phone": "+14155550123",
        "deactivated_at": 1000000,
        "deletion_requested_at": 1000000,
        "subscription_uuid": "uuid-purge-123",
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": 1000000.0,
            "created_at": 1000000.0,
            "declared_onboarding_intent": "personal",
            "attempts": 1,
            "attribution_status": "recorded",
            "attribution": True,
            "first_positive_price_purchase_observed_at": 1000100.0,
        },
    }

    db.docs[f"contractors/{contractor_id}/contacts/c1"] = {"name": "Test Contact"}

    monkeypatch.setattr("app.db.purge.get_firestore_client", lambda: db)
    monkeypatch.setattr("app.db.purge.settings.estimate_media_bucket", "")

    res = _purge_sync(contractor_id)
    assert res["purged_at"] > 0
    assert res["deleted"].get("contacts") == 1

    # Assert document in DB is reduced to tombstone
    final_doc = db.docs.get(f"contractors/{contractor_id}")
    assert final_doc is not None
    assert final_doc["active"] is False
    assert final_doc["purged_at"] == res["purged_at"]
    assert "acquisition_measurement" not in final_doc
    assert "business_name" not in final_doc
    assert "owner_name" not in final_doc
    assert "owner_phone" not in final_doc

    # Assert only tombstone fields remain
    for key in final_doc:
        assert key in TOMBSTONE_FIELDS

    # Assert subcollection was purged
    assert f"contractors/{contractor_id}/contacts/c1" not in db.docs


# ---------------------------------------------------------------------------
# Subscription Entitlement & App Store Notification Measurement Hooks Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_response_isolation_with_held_payment_measurement(monkeypatch):
    """Verify verification and renewal notifications return ACTIVE/True immediately without awaiting held payment measurement."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    contractor_id = "cnt_held_test"
    sub_uuid = "uuid-held-123"

    async def fake_get_contractor(cid):
        return {"contractor_id": cid, "subscription_uuid": sub_uuid, "active": True}

    async def fake_get_by_uuid(uuid_val, include_inactive=False):
        if uuid_val == sub_uuid:
            return {"contractor_id": contractor_id, "subscription_uuid": sub_uuid, "active": True}
        return None

    async def fake_claim_ok(*args, **kwargs):
        return True, contractor_id

    async def fake_activate_ok(*args, **kwargs):
        return True

    async def fake_update_ok(cid, updates):
        return True

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_get_by_uuid)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim_ok)
    monkeypatch.setattr("app.db.contractors.activate_subscription_entitlement", fake_activate_ok)
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_ok)

    entered_event = asyncio.Event()
    release_event = asyncio.Event()

    async def fake_held_record(cid, tier, tx):
        entered_event.set()
        await release_event.wait()

    monkeypatch.setattr(acquisition, "record_payment_measurement", fake_held_record)

    valid_tx = {
        "productId": "com.kevin.callscreen.personal.monthly",
        "appAccountToken": sub_uuid,
        "originalTransactionId": "orig_held_123",
        "transactionId": "tx_held_123",
        "expiresDate": (time.time() + 86400) * 1000,
        "environment": "Production",
        "price": 999,
        "purchaseDate": int(time.time() * 1000),
    }

    # 1. Direct verification: returns ACTIVE without waiting for held measurement
    try:
        res = await asyncio.wait_for(
            update_subscription_from_transaction(contractor_id, valid_tx),
            timeout=0.5,
        )
        assert res.outcome == SubscriptionUpdateOutcome.ACTIVE
        assert len(acquisition._pending_payment_tasks) == 1

        # Wait until background task enters held writer
        await asyncio.wait_for(entered_event.wait(), timeout=0.5)
        assert not release_event.is_set()
    finally:
        release_event.set()
        if acquisition._pending_payment_tasks:
            await asyncio.gather(*list(acquisition._pending_payment_tasks), return_exceptions=True)
        assert len(acquisition._pending_payment_tasks) == 0

    # 2. App Store notification: returns True without waiting for held measurement
    entered_event.clear()
    release_event.clear()

    notif_payload = {
        "notificationType": "DID_RENEW",
        "subtype": "BILLING_RECOVERY",
        "data": {
            "signedTransactionInfo": _unsigned_jws(valid_tx)
        }
    }

    try:
        handled = await asyncio.wait_for(
            handle_appstore_notification(notif_payload),
            timeout=0.5,
        )
        assert handled is True
        assert len(acquisition._pending_payment_tasks) == 1

        await asyncio.wait_for(entered_event.wait(), timeout=0.5)
        assert not release_event.is_set()
    finally:
        release_event.set()
        if acquisition._pending_payment_tasks:
            await asyncio.gather(*list(acquisition._pending_payment_tasks), return_exceptions=True)
        assert len(acquisition._pending_payment_tasks) == 0


@pytest.mark.asyncio
async def test_update_subscription_from_transaction_measurement_hooks(monkeypatch):
    """Verify update_subscription_from_transaction schedules measurement once on success, zero on failure/throw, and isolates scheduler exceptions."""
    contractor_id = "cnt_sub_hook_test"
    sub_uuid = "uuid-sub-hook-123"

    async def fake_get_contractor(cid):
        if cid == contractor_id:
            return {"contractor_id": cid, "subscription_uuid": sub_uuid, "active": True}
        return None

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)

    scheduled_calls = []
    def fake_schedule_measurement(cid, tier, tx_info):
        scheduled_calls.append((cid, tier, tx_info))

    monkeypatch.setattr("app.services.acquisition.schedule_payment_measurement", fake_schedule_measurement)

    valid_tx = {
        "productId": "com.kevin.callscreen.personal.monthly",
        "appAccountToken": sub_uuid,
        "originalTransactionId": "orig_tx_123",
        "transactionId": "tx_123",
        "expiresDate": (time.time() + 86400) * 1000,
        "environment": "Production",
        "price": 999,
    }

    # 1. Accepted transaction -> schedule_payment_measurement called once
    async def fake_claim_ok(*args, **kwargs):
        return True, contractor_id
    async def fake_activate_ok(*args, **kwargs):
        return True

    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim_ok)
    monkeypatch.setattr("app.db.contractors.activate_subscription_entitlement", fake_activate_ok)

    res = await update_subscription_from_transaction(contractor_id, valid_tx)
    assert res.outcome == SubscriptionUpdateOutcome.ACTIVE
    assert len(scheduled_calls) == 1
    assert scheduled_calls[0][0] == contractor_id
    assert scheduled_calls[0][1] == "personal"

    scheduled_calls.clear()

    # 2a. False update from activate_subscription_entitlement -> zero measurement calls
    async def fake_activate_fail(*args, **kwargs):
        return False
    monkeypatch.setattr("app.db.contractors.activate_subscription_entitlement", fake_activate_fail)

    res_fail = await update_subscription_from_transaction(contractor_id, valid_tx)
    assert res_fail.outcome == SubscriptionUpdateOutcome.MALFORMED_TRANSACTION
    assert len(scheduled_calls) == 0

    # 2b. Rejected payload (unknown product) -> zero measurement calls
    bad_prod_tx = dict(valid_tx, productId="unknown.product.id")
    res_unknown = await update_subscription_from_transaction(contractor_id, bad_prod_tx)
    assert res_unknown.outcome == SubscriptionUpdateOutcome.UNKNOWN_PRODUCT
    assert len(scheduled_calls) == 0

    # 2c. Rejected payload (ownership mismatch) -> zero measurement calls
    bad_owner_tx = dict(valid_tx, appAccountToken="wrong-uuid")
    res_mismatch = await update_subscription_from_transaction(contractor_id, bad_owner_tx)
    assert res_mismatch.outcome == SubscriptionUpdateOutcome.OWNERSHIP_MISMATCH
    assert len(scheduled_calls) == 0

    # 2d. Rejected payload (cross-contractor receipt replay) -> zero measurement calls, raises CrossContractorReceiptError
    async def fake_claim_cross(*args, **kwargs):
        return False, "other_cnt"
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim_cross)

    with pytest.raises(CrossContractorReceiptError):
        await update_subscription_from_transaction(contractor_id, valid_tx)
    assert len(scheduled_calls) == 0

    # 3. Thrown update -> zero measurement calls
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim_ok)
    async def fake_activate_throw(*args, **kwargs):
        raise RuntimeError("DB connection dropped")
    monkeypatch.setattr("app.db.contractors.activate_subscription_entitlement", fake_activate_throw)

    with pytest.raises(RuntimeError):
        await update_subscription_from_transaction(contractor_id, valid_tx)
    assert len(scheduled_calls) == 0

    # 4. Measurement scheduler throws -> existing successful entitlement result still succeeds
    monkeypatch.setattr("app.db.contractors.activate_subscription_entitlement", fake_activate_ok)
    def fake_schedule_throw(cid, tier, tx_info):
        raise RuntimeError("Scheduler internal error")
    monkeypatch.setattr("app.services.acquisition.schedule_payment_measurement", fake_schedule_throw)

    res_safe = await update_subscription_from_transaction(contractor_id, valid_tx)
    assert res_safe.outcome == SubscriptionUpdateOutcome.ACTIVE


@pytest.mark.asyncio
async def test_handle_appstore_notification_measurement_hooks(monkeypatch):
    """Verify handle_appstore_notification DID_RENEW/SUBSCRIBED schedules measurement only when contractor updated, and isolates exceptions."""
    contractor_id = "cnt_notif_hook_test"
    sub_uuid = "uuid-notif-hook-123"

    async def fake_get_by_uuid(uuid_val, include_inactive=False):
        if uuid_val == sub_uuid:
            return {"contractor_id": contractor_id, "subscription_uuid": sub_uuid, "active": True}
        return None

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_get_by_uuid)

    async def fake_claim_ok(*args, **kwargs):
        return True, contractor_id
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", fake_claim_ok)

    # Patch push notification device token lookup and push sending to prevent real Firestore credential refresh
    async def fake_get_device_token(contractor_id=None):
        return None

    async def fake_send_regular_push(*args, **kwargs):
        raise AssertionError("send_regular_push should not be called when device_token is None")

    monkeypatch.setattr("app.services.push_notification.get_device_token", fake_get_device_token)
    monkeypatch.setattr("app.services.push_notification.send_regular_push", fake_send_regular_push)

    scheduled_calls = []
    def fake_schedule_measurement(cid, tier, tx_info):
        scheduled_calls.append((cid, tier, tx_info))

    monkeypatch.setattr("app.services.acquisition.schedule_payment_measurement", fake_schedule_measurement)

    valid_tx = {
        "productId": "com.kevin.callscreen.business.monthly",
        "appAccountToken": sub_uuid,
        "originalTransactionId": "orig_notif_123",
        "transactionId": "tx_notif_123",
        "expiresDate": (time.time() + 86400) * 1000,
        "environment": "Production",
        "price": 4999,
        "purchaseDate": time.time() * 1000,
    }

    notif_payload = {
        "notificationType": "DID_RENEW",
        "subtype": "BILLING_RECOVERY",
        "data": {
            "signedTransactionInfo": _unsigned_jws(valid_tx)
        }
    }

    # 1. Accepted notification with update_contractor returning True -> measurement scheduled once
    async def fake_update_ok(cid, updates):
        return True
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_ok)

    handled = await handle_appstore_notification(notif_payload)
    assert handled is True
    assert len(scheduled_calls) == 1
    assert scheduled_calls[0][0] == contractor_id
    assert scheduled_calls[0][1] == "business"

    scheduled_calls.clear()

    # 2a. False update from update_contractor -> zero measurement calls
    async def fake_update_false(cid, updates):
        return False
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_false)

    handled_false = await handle_appstore_notification(notif_payload)
    assert handled_false is True  # Notification processing completes
    assert len(scheduled_calls) == 0

    # 2b. Rejected payload (unknown product) -> zero measurement calls
    bad_prod_tx = dict(valid_tx, productId="unknown.product")
    bad_prod_payload = {
        "notificationType": "DID_RENEW",
        "subtype": "",
        "data": {"signedTransactionInfo": _unsigned_jws(bad_prod_tx)}
    }
    handled_bad = await handle_appstore_notification(bad_prod_payload)
    assert handled_bad is False
    assert len(scheduled_calls) == 0

    # 2c. Non-renewal/subscription notification type (EXPIRED) -> zero measurement calls and pushes isolated
    expired_payload = {
        "notificationType": "EXPIRED",
        "subtype": "VOLUNTARY",
        "data": {"signedTransactionInfo": _unsigned_jws(valid_tx)}
    }
    async def fake_get_contractor(cid):
        return {"contractor_id": cid, "active": True}
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_ok)

    handled_exp = await handle_appstore_notification(expired_payload)
    assert handled_exp is True
    assert len(scheduled_calls) == 0

    # 3. Thrown update from update_contractor -> zero measurement calls
    async def fake_update_throw(cid, updates):
        raise RuntimeError("Database error during update")
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_throw)

    with pytest.raises(RuntimeError):
        await handle_appstore_notification(notif_payload)
    assert len(scheduled_calls) == 0

    # 4. Measurement scheduler throws -> notification handling still succeeds (returns True)
    monkeypatch.setattr("app.db.contractors.update_contractor", fake_update_ok)
    def fake_schedule_throw(cid, tier, tx_info):
        raise RuntimeError("Scheduler error")
    monkeypatch.setattr("app.services.acquisition.schedule_payment_measurement", fake_schedule_throw)

    handled_safe = await handle_appstore_notification(notif_payload)
    assert handled_safe is True
