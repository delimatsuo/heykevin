"""Tests for the number-release sweep (app/services/number_release.py).

The sweep body used to live inline in app/main.py with no tests. It now runs as
`run_expired_contractor_cleanup_once`, which these tests drive with an in-memory
Firestore stand-in and fakes for SMS, deactivation, the latest-call lookup and
the App Store status check.
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
    def __init__(self, doc_id: str, data: dict | None):
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict | None:
        return dict(self._data) if self._data is not None else None


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
        return _Snap(self._id, self._store.get(self._id))

    def set(self, value: dict, merge: bool = False):
        current = self._store.get(self._id, {}) if merge else {}
        self._store[self._id] = {**current, **value}


class _Collection:
    def __init__(self, store: dict):
        self._store = store

    def where(self, *_a, **_k):
        return _Query(self._store)

    def document(self, doc_id: str):
        return _DocRef(self._store, doc_id)


class _Db:
    def __init__(self, stores: dict[str, dict]):
        self._stores = stores

    def collection(self, name: str):
        return _Collection(self._stores.setdefault(name, {}))


def _build_harness(monkeypatch, *, fake_apple: bool = True):
    store: dict[str, dict] = {}
    system: dict[str, dict] = {"number_release": {"observation_started_at": time.time() - 45 * DAY}}
    sms: list[tuple] = []
    deactivated: list[str] = []
    last_calls: dict[str, object] = {}
    on_lookup: dict[str, object] = {}
    apple: dict[str, object] = {}
    apple_calls: list[str] = []

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

    async def fake_apple_statuses(original_id):
        apple_calls.append(original_id)
        value = apple.get(original_id, [])
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(number_release, "get_firestore_client", lambda: _Db({"contractors": store, "system": system}))
    monkeypatch.setattr(number_release, "send_sms", fake_sms)
    monkeypatch.setattr(number_release, "deactivate_contractor", fake_deactivate)
    monkeypatch.setattr(number_release, "latest_call_timestamp", fake_latest)
    if fake_apple:
        monkeypatch.setattr(number_release, "_apple_subscription_statuses", fake_apple_statuses)
    monkeypatch.setattr(number_release.settings, "lapsed_number_release_enabled", True)
    return SimpleNamespace(
        store=store, system=system, sms=sms, deactivated=deactivated,
        last_calls=last_calls, on_lookup=on_lookup, apple=apple, apple_calls=apple_calls,
    )


@pytest.fixture
def harness(monkeypatch):
    return _build_harness(monkeypatch, fake_apple=True)


@pytest.fixture
def harness_real_apple(monkeypatch):
    """Same harness, but the App Store fetcher is the real one (HTTP mocked with respx)."""
    return _build_harness(monkeypatch, fake_apple=False)


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
async def test_first_run_starts_the_observation_window_and_releases_nothing_lapsed(harness):
    """Without a stamp history nobody can be proven quiet, so the first run only records the start."""
    harness.system.clear()
    now = time.time()
    harness.store["lapsed1"] = _lapsed(now)
    harness.store["gone1"] = _deleted_app(now)

    result = await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.system["number_release"]["observation_started_at"] == now
    assert harness.deactivated == ["gone1"]  # deleted-app path is not held back
    assert result["lapsed_released"] == 0


@pytest.mark.asyncio
async def test_observation_window_must_be_thirty_days_old(harness):
    now = time.time()
    harness.store["lapsed1"] = _lapsed(now)

    harness.system["number_release"] = {"observation_started_at": now - 29 * DAY}
    assert (await number_release.run_expired_contractor_cleanup_once(now=now))["lapsed_released"] == 0
    assert harness.deactivated == []

    harness.system["number_release"] = {"observation_started_at": now - 30 * DAY}
    assert (await number_release.run_expired_contractor_cleanup_once(now=now))["lapsed_released"] == 1


@pytest.mark.asyncio
async def test_window_marker_is_written_even_while_the_flag_is_off(harness, monkeypatch):
    monkeypatch.setattr(number_release.settings, "lapsed_number_release_enabled", False)
    harness.system.clear()
    now = time.time()

    await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.system["number_release"]["observation_started_at"] == now


@pytest.mark.asyncio
async def test_recent_inbound_call_blocks_lapsed_release(harness):
    now = time.time()
    harness.store["lapsed1"] = _lapsed(now)
    harness.last_calls["lapsed1"] = now - 3 * DAY

    await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == [] and harness.sms == []


@pytest.mark.asyncio
async def test_recent_inbound_stamp_blocks_lapsed_release(harness):
    now = time.time()
    harness.store["lapsed1"] = _lapsed(now, last_inbound_call_at=now - 2 * DAY)

    await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == []


@pytest.mark.asyncio
async def test_call_lookup_failure_fails_closed(harness):
    """If we cannot tell whether calls still arrive, we cannot prove the number is quiet."""
    now = time.time()
    harness.store["lapsed1"] = _lapsed(now)
    harness.last_calls["lapsed1"] = RuntimeError("firestore unavailable")

    result = await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == [] and result["lapsed_released"] == 0


@pytest.mark.asyncio
async def test_apple_live_subscription_holds_the_number(harness):
    """`expired` is written at the start of billing retry; Apple gets the last word."""
    now = time.time()
    for code in (1, 3, 4):
        harness.store.clear(); harness.deactivated.clear()
        harness.store["paid"] = _lapsed(now, subscription_original_transaction_id="2000000123")
        harness.apple["2000000123"] = [code]
        await number_release.run_expired_contractor_cleanup_once(now=now)
        assert harness.deactivated == [], f"status {code} must hold"


@pytest.mark.asyncio
async def test_apple_expired_or_revoked_allows_release(harness):
    now = time.time()
    harness.store["paid"] = _lapsed(now, subscription_original_transaction_id="2000000123")
    harness.apple["2000000123"] = [2, 5]

    await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == ["paid"]


@pytest.mark.asyncio
async def test_apple_check_failure_holds_the_number(harness):
    now = time.time()
    harness.store["paid"] = _lapsed(now, subscription_original_transaction_id="2000000123")
    harness.apple["2000000123"] = RuntimeError("App Store status HTTP 500")

    await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == []


@pytest.mark.asyncio
async def test_never_paid_account_is_not_checked_with_apple(harness):
    now = time.time()
    harness.store["trialonly"] = _lapsed(now)

    await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == ["trialonly"] and harness.apple_calls == []


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


@pytest.mark.asyncio
async def test_marker_read_failure_holds_lapsed_but_not_deleted_app_releases(harness, monkeypatch):
    """The observation marker is a lapsed-only concern; it must never stall the 14-day path."""
    now = time.time()
    harness.store["lapsed1"] = _lapsed(now)
    harness.store["gone1"] = _deleted_app(now)

    class _BrokenSystem:
        def document(self, _doc_id):
            raise RuntimeError("permission denied on system collection")

    real_db = number_release.get_firestore_client()

    class _Db2:
        def collection(self, name):
            return _BrokenSystem() if name == "system" else real_db.collection(name)

    monkeypatch.setattr(number_release, "get_firestore_client", lambda: _Db2())

    result = await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness.deactivated == ["gone1"]
    assert result == {"deleted_app_released": 1, "lapsed_released": 0, "skipped": 0}


# --- wire layer of the Apple hold ------------------------------------------

import httpx  # noqa: E402
import respx  # noqa: E402

_STATUS_RESPONSE = {
    "environment": "Production",
    "bundleId": "com.kevin.callscreen",
    "appAppleId": 6761427495,
    "data": [
        {
            "subscriptionGroupIdentifier": "22007035",
            "lastTransactions": [
                {
                    "originalTransactionId": "2000000123",
                    "status": 3,
                    "signedTransactionInfo": "eyJhbGciOiJFUzI1NiJ9.e30.sig",
                    "signedRenewalInfo": "eyJhbGciOiJFUzI1NiJ9.e30.sig",
                }
            ],
        }
    ],
}


@pytest.fixture
def apple_wire(monkeypatch):
    monkeypatch.setattr(number_release, "_get_appstore_jwt", lambda: "test.jwt.token")
    monkeypatch.setattr(number_release, "_get_appstore_url", lambda: "https://api.storekit.apple.com")


@pytest.mark.asyncio
@respx.mock
async def test_apple_statuses_parses_a_real_status_response(apple_wire):
    route = respx.get("https://api.storekit.apple.com/inApps/v1/subscriptions/2000000123").mock(
        return_value=httpx.Response(200, json=_STATUS_RESPONSE)
    )

    statuses = await number_release._apple_subscription_statuses("2000000123")

    assert statuses == [3]
    assert route.calls.last.request.headers["Authorization"] == "Bearer test.jwt.token"


@pytest.mark.asyncio
@respx.mock
async def test_apple_statuses_empty_data_means_no_hold(apple_wire):
    respx.get("https://api.storekit.apple.com/inApps/v1/subscriptions/2000000123").mock(
        return_value=httpx.Response(200, json={**_STATUS_RESPONSE, "data": []})
    )
    assert await number_release._apple_subscription_statuses("2000000123") == []


@pytest.mark.asyncio
@respx.mock
async def test_apple_statuses_non_200_raises(apple_wire):
    respx.get("https://api.storekit.apple.com/inApps/v1/subscriptions/2000000123").mock(
        return_value=httpx.Response(401, json={"errorCode": 4040010})
    )
    with pytest.raises(RuntimeError):
        await number_release._apple_subscription_statuses("2000000123")


@pytest.mark.asyncio
@respx.mock
async def test_apple_hold_end_to_end_through_the_wire(apple_wire, harness_real_apple):
    """Billing retry reported by Apple, parsed from the real wire shape, holds the number."""
    respx.get("https://api.storekit.apple.com/inApps/v1/subscriptions/2000000123").mock(
        return_value=httpx.Response(200, json=_STATUS_RESPONSE)
    )
    now = time.time()
    harness_real_apple.store["paid"] = _lapsed(now, subscription_original_transaction_id="2000000123")

    await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness_real_apple.deactivated == []


@pytest.mark.asyncio
@respx.mock
async def test_apple_expired_end_to_end_through_the_wire_releases(apple_wire, harness_real_apple):
    body = {**_STATUS_RESPONSE, "data": [{**_STATUS_RESPONSE["data"][0], "lastTransactions": [{**_STATUS_RESPONSE["data"][0]["lastTransactions"][0], "status": 2}]}]}
    respx.get("https://api.storekit.apple.com/inApps/v1/subscriptions/2000000123").mock(
        return_value=httpx.Response(200, json=body)
    )
    now = time.time()
    harness_real_apple.store["paid"] = _lapsed(now, subscription_original_transaction_id="2000000123")

    await number_release.run_expired_contractor_cleanup_once(now=now)

    assert harness_real_apple.deactivated == ["paid"]
