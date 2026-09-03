"""Tests for the number-release sweep (app/services/number_release.py).

The sweep body used to live inline in app/main.py with no tests. It now runs as
`run_expired_contractor_cleanup_once`, which these tests drive with an in-memory
Firestore stand-in and fakes for SMS, deactivation, and the latest-call lookup.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest

from app.services import number_release

DAY = 86400


class _Snap:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return True

    def to_dict(self) -> dict:
        return dict(self._data)


class _Query:
    def __init__(self, store: dict):
        self._store = store

    def where(self, *_a, **_k):
        return self

    def stream(self):
        # Snapshot semantics: copies taken at query time, like a real stream.
        return iter([_Snap(i, dict(d)) for i, d in self._store.items()])


class _DocRef:
    def __init__(self, store: dict, doc_id: str):
        self._store = store
        self._id = doc_id

    def get(self):
        return _Snap(self._id, self._store[self._id])


class _Collection:
    def __init__(self, store: dict):
        self._store = store

    def where(self, *_a, **_k):
        return _Query(self._store)

    def document(self, doc_id: str):
        return _DocRef(self._store, doc_id)


class _Db:
    def __init__(self, store: dict):
        self._store = store

    def collection(self, name: str):
        assert name == "contractors"
        return _Collection(self._store)


@pytest.fixture
def harness(monkeypatch):
    store: dict[str, dict] = {}
    sms: list[tuple] = []
    deactivated: list[str] = []
    last_calls: dict[str, object] = {}
    on_lookup: dict[str, object] = {}

    async def fake_sms(to, body, from_number=""):
        sms.append((to, body, from_number))
        return True

    async def fake_deactivate(contractor_id, user_requested=False):
        deactivated.append(contractor_id)
        store[contractor_id]["active"] = False
        return True

    async def fake_latest(contractor_id):
        hook = on_lookup.get(contractor_id)
        if hook:
            hook()
        value = last_calls.get(contractor_id)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(number_release, "get_firestore_client", lambda: _Db(store))
    monkeypatch.setattr(number_release, "send_sms", fake_sms)
    monkeypatch.setattr(number_release, "deactivate_contractor", fake_deactivate)
    monkeypatch.setattr(number_release, "latest_call_timestamp", fake_latest)
    monkeypatch.setattr(number_release.settings, "lapsed_number_release_enabled", True)
    return SimpleNamespace(store=store, sms=sms, deactivated=deactivated, last_calls=last_calls, on_lookup=on_lookup)


def _lapsed(now: float, days: float = 40, **kw) -> dict:
    base = {
        "active": True,
        "subscription_status": "expired",
        "twilio_number": "+15555550200",
        "owner_phone": "+15555550300",
        "subscription_expires": now - days * DAY,
        "trial_start": now - (days + 14) * DAY,
        "forwarding_last_seen_at": None,
        "deleted_app_detected_at": None,
    }
    base.update(kw)
    return base


def _deleted_app(now: float, days: float = 20, **kw) -> dict:
    base = _lapsed(now, days=5)  # expired recently, so only the deleted-app rule can release it
    base["deleted_app_detected_at"] = now - days * DAY
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_lapsed_account_is_released_with_thirty_day_notice(harness):
    now = time.time()
    harness.store["lapsed1"] = _lapsed(now)

    result = await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == ["lapsed1"]
    assert result["lapsed_released"] == 1 and result["deleted_app_released"] == 0
    (to, body, from_number), = harness.sms
    assert to == "+15555550300" and from_number == "+15555550200"
    assert "30 days" in body and "resubscribe" in body.lower()


@pytest.mark.asyncio
async def test_flag_off_keeps_lapsed_numbers_but_deleted_app_path_still_runs(harness, monkeypatch):
    monkeypatch.setattr(number_release.settings, "lapsed_number_release_enabled", False)
    now = time.time()
    harness.store["lapsed1"] = _lapsed(now)
    harness.store["gone1"] = _deleted_app(now)

    result = await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == ["gone1"]
    assert result == {"deleted_app_released": 1, "lapsed_released": 0, "skipped": 0}
    assert "14 days" in harness.sms[0][1]


@pytest.mark.asyncio
async def test_recent_inbound_call_blocks_lapsed_release(harness):
    now = time.time()
    harness.store["lapsed1"] = _lapsed(now)
    harness.last_calls["lapsed1"] = now - 3 * DAY

    await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == [] and harness.sms == []


@pytest.mark.asyncio
async def test_call_lookup_failure_fails_closed(harness):
    """If we cannot tell whether calls still arrive, we cannot prove the number is quiet."""
    now = time.time()
    harness.store["lapsed1"] = _lapsed(now)
    harness.last_calls["lapsed1"] = RuntimeError("firestore unavailable")

    result = await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == [] and result["lapsed_released"] == 0


@pytest.mark.asyncio
async def test_lapsed_releases_are_capped_per_run_but_deleted_app_releases_are_not(harness):
    now = time.time()
    for i in range(number_release.LAPSED_RELEASE_MAX_PER_RUN + 2):
        harness.store[f"lapsed{i}"] = _lapsed(now, days=40 + i)
    harness.store["gone1"] = _deleted_app(now)

    result = await number_release.run_expired_contractor_cleanup_once(now=now)

    assert result["lapsed_released"] == number_release.LAPSED_RELEASE_MAX_PER_RUN
    assert result["deleted_app_released"] == 1
    assert len(harness.deactivated) == number_release.LAPSED_RELEASE_MAX_PER_RUN + 1


@pytest.mark.asyncio
async def test_forwarding_evidence_arriving_after_the_snapshot_blocks_release(harness):
    """The fresh re-read before acting must see evidence written since the query."""
    now = time.time()
    harness.store["lapsed1"] = _lapsed(now)

    def _forward_just_landed():
        harness.store["lapsed1"]["forwarding_last_seen_at"] = time.time()

    harness.on_lookup["lapsed1"] = _forward_just_landed

    result = await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == [] and result["lapsed_released"] == 0


@pytest.mark.asyncio
async def test_missing_owner_phone_still_releases_without_sms(harness):
    now = time.time()
    harness.store["lapsed1"] = _lapsed(now, owner_phone="")

    await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == ["lapsed1"] and harness.sms == []


@pytest.mark.asyncio
async def test_expired_but_recent_account_is_left_alone(harness):
    now = time.time()
    harness.store["fresh"] = _lapsed(now, days=10)

    result = await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == [] and result["lapsed_released"] == 0
