"""Tests for the App Store re-check of stale `active` accounts.

A lost Apple notification leaves an account `active` with a past expiry, and the
call gate keeps serving it. The re-check asks Apple and applies only an explicit
answer for the account's own bound receipt; everything else changes nothing.
"""

from __future__ import annotations

import base64
import json
import os
import time

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import contractors  # noqa: E402
from app.db.contractors import StaleSubscriptionUpdate  # noqa: E402
from app.services import subscription_recheck as recheck  # noqa: E402
from app.services.subscription import APPSTORE_PRODUCTION_URL, APPSTORE_SANDBOX_URL  # noqa: E402

NOW = time.time()
STALE = NOW - 64 * 86400
ORIGINAL = "2000000123"
ACCOUNT_TOKEN = "0f6c2f1e-1111-4222-8333-444455556666"


def _jws(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJFUzI1NiJ9.{body}.sig"


def _transaction(**overrides) -> dict:
    transaction = {
        "bundleId": "com.kevin.callscreen",
        "originalTransactionId": ORIGINAL,
        "appAccountToken": ACCOUNT_TOKEN,
        "productId": "com.kevin.callscreen.business.monthly",
        "expiresDate": int(STALE * 1000),
    }
    transaction.update(overrides)
    return transaction


def _item(status: int, transaction: dict | None = None) -> dict:
    return {
        "originalTransactionId": ORIGINAL,
        "status": status,
        "signedTransactionInfo": _jws(transaction or _transaction()),
        "signedRenewalInfo": _jws({}),
    }


class _World:
    def __init__(self):
        self.account = {
            "contractor_id": "c1",
            "active": True,
            "subscription_status": "active",
            "subscription_tier": "business",
            "subscription_expires": STALE,
            "subscription_original_transaction_id": ORIGINAL,
            "subscription_uuid": ACCOUNT_TOKEN,
        }
        self.binding = {"contractor_id": "c1"}
        self.item: dict | None = _item(2)
        self.apple_error: Exception | None = None
        self.update_result = StaleSubscriptionUpdate.APPLIED
        self.apple_asked: list[str] = []
        self.updates: list[tuple] = []
        self.pushes: list[str] = []


@pytest.fixture
def world(monkeypatch):
    w = _World()
    monkeypatch.setattr(settings, "appstore_key_id", "TESTKEY")
    monkeypatch.setattr(settings, "appstore_bundle_id", "com.kevin.callscreen")

    async def fake_get_contractor(contractor_id):
        return dict(w.account) if contractor_id == w.account["contractor_id"] else None

    async def fake_binding(original_id):
        return dict(w.binding) if w.binding is not None else None

    async def fake_apple(original_id):
        w.apple_asked.append(original_id)
        if w.apple_error:
            raise w.apple_error
        return w.item

    async def fake_update(contractor_id, stale_expires, updates):
        w.updates.append((contractor_id, stale_expires, updates))
        return w.update_result

    async def fake_notify(contractor_id):
        w.pushes.append(contractor_id)

    monkeypatch.setattr(contractors, "get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.db.apple_transactions.get_transaction_binding", fake_binding)
    monkeypatch.setattr(recheck, "_apple_last_transaction", fake_apple)
    monkeypatch.setattr(contractors, "update_stale_subscription", fake_update)
    monkeypatch.setattr(recheck, "notify_subscription_ended", fake_notify)
    return w


async def _run(world) -> str:
    return await recheck.recheck_stale_subscription("c1", now=NOW)


# --- Apple's answers --------------------------------------------------------


@pytest.mark.asyncio
async def test_apple_active_writes_the_renewed_term_and_sends_no_push(world):
    renewed_until = NOW + 20 * 86400
    world.item = _item(1, _transaction(
        productId="com.kevin.callscreen.personal.monthly",
        expiresDate=int(renewed_until * 1000),
    ))

    assert await _run(world) == "renewed"

    assert world.updates == [("c1", STALE, {
        "subscription_tier": "personal",
        "subscription_status": "active",
        "subscription_expires": pytest.approx(renewed_until, abs=0.001),
    })]
    assert world.pushes == []


@pytest.mark.asyncio
async def test_apple_expired_marks_the_account_expired_and_tells_the_owner(world):
    world.item = _item(2)

    assert await _run(world) == "expired"

    assert world.updates == [("c1", STALE, {"subscription_status": "expired"})]
    assert world.pushes == ["c1"]


@pytest.mark.asyncio
async def test_apple_revoked_marks_the_account_expired(world):
    world.item = _item(5, _transaction(revocationDate=int((NOW - 86400) * 1000)))

    assert await _run(world) == "revoked"

    assert world.updates == [("c1", STALE, {"subscription_status": "expired"})]
    assert world.pushes == ["c1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [3, 4, 99])
async def test_billing_retry_grace_period_and_unknown_statuses_change_nothing(world, status):
    world.item = _item(status)

    assert await _run(world) == f"no_change(apple_status={status})"

    assert world.updates == [] and world.pushes == []


@pytest.mark.asyncio
async def test_expired_status_with_a_term_still_running_changes_nothing(world):
    world.item = _item(2, _transaction(expiresDate=int((NOW + 86400) * 1000)))

    assert (await _run(world)).startswith("no_change")
    assert world.updates == []


@pytest.mark.asyncio
async def test_revoked_status_without_a_revocation_date_changes_nothing(world):
    world.item = _item(5)

    assert (await _run(world)).startswith("no_change")
    assert world.updates == []


@pytest.mark.asyncio
async def test_active_status_without_a_future_term_changes_nothing(world):
    world.item = _item(1)  # expiresDate is the stale one

    assert await _run(world) == "no_change"
    assert world.updates == []


# --- whose receipt ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"appAccountToken": "99999999-9999-4999-8999-999999999999"},
    {"bundleId": "com.example.other"},
    {"originalTransactionId": "2000000999"},
])
async def test_a_receipt_that_is_not_this_accounts_changes_nothing(world, override):
    world.item = _item(2, _transaction(**override))

    assert await _run(world) == "receipt_mismatch"
    assert world.updates == [] and world.pushes == []


@pytest.mark.asyncio
async def test_a_receipt_without_an_account_token_relies_on_the_binding(world):
    transaction = _transaction()
    del transaction["appAccountToken"]
    world.item = _item(2, transaction)

    assert await _run(world) == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", [None, {"contractor_id": "someone-else"}])
async def test_a_receipt_not_bound_to_this_account_is_never_asked_about(world, binding):
    world.binding = binding

    assert await _run(world) == "receipt_not_bound_here"
    assert world.apple_asked == [] and world.updates == []


@pytest.mark.asyncio
async def test_an_account_without_a_receipt_is_never_asked_about(world):
    world.account["subscription_original_transaction_id"] = "  "

    assert await _run(world) == "no_receipt"
    assert world.apple_asked == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"subscription_expires": NOW - 86400},
    {"subscription_status": "trial"},
    {"subscription_status": "expired"},
    {"active": False},
])
async def test_accounts_that_are_not_stale_and_active_are_never_asked_about(world, change):
    world.account.update(change)

    assert await _run(world) == "not_stale"
    assert world.apple_asked == []


@pytest.mark.asyncio
async def test_apple_with_no_record_changes_nothing(world):
    world.item = None

    assert await _run(world) == "apple_has_no_record"
    assert world.updates == []


@pytest.mark.asyncio
async def test_an_unconfigured_app_store_is_never_asked(world, monkeypatch):
    monkeypatch.setattr(settings, "appstore_key_id", "")

    assert await _run(world) == "not_configured"
    assert world.apple_asked == []


@pytest.mark.asyncio
async def test_failing_to_reach_apple_changes_nothing(world):
    world.apple_error = RuntimeError("App Store status HTTP 500 (production)")

    await recheck._recheck_and_log("c1")  # must not raise

    assert world.apple_asked == [ORIGINAL]

    assert world.updates == [] and world.pushes == []


# --- the write --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_renewal_that_lands_meanwhile_wins(world):
    world.update_result = StaleSubscriptionUpdate.MOVED_ON

    assert await _run(world) == "moved_on"
    assert world.pushes == []


@pytest.mark.asyncio
async def test_a_number_release_hold_sends_no_push(world):
    world.update_result = StaleSubscriptionUpdate.HELD

    assert await _run(world) == "held_for_number_release"
    assert world.pushes == []


class _Doc:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Ref:
    def __init__(self, store, doc_id):
        self.store, self.doc_id = store, doc_id

    def get(self, transaction=None):
        return _Doc(self.store.get(self.doc_id))


class _Transaction:
    def __init__(self, store):
        self.store = store

    def update(self, ref, fields):
        self.store[ref.doc_id].update(fields)


class _Db:
    def __init__(self, store):
        self.store = store

    def collection(self, _name):
        return self

    def document(self, doc_id):
        return _Ref(self.store, doc_id)

    def transaction(self):
        return _Transaction(self.store)


@pytest.fixture
def stored(monkeypatch):
    store = {"c1": {
        "active": True,
        "subscription_status": "active",
        "subscription_expires": STALE,
    }}
    monkeypatch.setattr("google.cloud.firestore.transactional", lambda fn: fn)
    monkeypatch.setattr(contractors, "get_firestore_client", lambda: _Db(store))
    return store


@pytest.mark.asyncio
async def test_the_write_applies_to_the_stale_account_it_read(stored):
    result = await contractors.update_stale_subscription("c1", STALE, {"subscription_status": "expired"})

    assert result is StaleSubscriptionUpdate.APPLIED
    assert stored["c1"]["subscription_status"] == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"subscription_expires": NOW + 30 * 86400},
    {"subscription_status": "expired"},
    {"active": False},
])
async def test_the_write_is_dropped_once_the_account_moved_on(stored, change):
    stored["c1"].update(change)
    before = dict(stored["c1"])

    result = await contractors.update_stale_subscription("c1", STALE, {"subscription_status": "expired"})

    assert result is StaleSubscriptionUpdate.MOVED_ON
    assert stored["c1"] == before


@pytest.mark.asyncio
async def test_the_write_is_dropped_for_a_missing_account(stored):
    result = await contractors.update_stale_subscription("gone", STALE, {"subscription_status": "expired"})

    assert result is StaleSubscriptionUpdate.MOVED_ON


@pytest.mark.asyncio
async def test_the_write_respects_a_number_release_in_progress(stored):
    stored["c1"][contractors.NUMBER_RELEASE_CLAIM_FIELD] = {
        "state": contractors.NUMBER_RELEASE_CLAIM_DELETING,
    }

    result = await contractors.update_stale_subscription("c1", STALE, {"subscription_status": "expired"})

    assert result is StaleSubscriptionUpdate.HELD
    assert stored["c1"]["subscription_status"] == "active"


# --- when it runs -----------------------------------------------------------


@pytest.fixture
def scheduler(monkeypatch):
    started: list[str] = []

    async def fake_recheck_and_log(contractor_id):
        started.append(contractor_id)

    monkeypatch.setattr(recheck, "_recheck_and_log", fake_recheck_and_log)
    monkeypatch.setattr(recheck, "_last_attempt", {})
    return started


def _stale_account(**overrides) -> dict:
    account = {
        "contractor_id": "c1",
        "subscription_status": "active",
        "subscription_expires": STALE,
    }
    account.update(overrides)
    return account


@pytest.mark.asyncio
async def test_the_recheck_is_off_by_default(scheduler):
    assert settings.appstore_stale_recheck_enabled is False

    assert recheck.schedule_stale_subscription_recheck(_stale_account(), NOW) is False


@pytest.mark.asyncio
async def test_a_busy_line_asks_apple_once_per_window(scheduler, monkeypatch):
    monkeypatch.setattr(settings, "appstore_stale_recheck_enabled", True)

    assert recheck.schedule_stale_subscription_recheck(_stale_account(), NOW) is True
    assert recheck.schedule_stale_subscription_recheck(_stale_account(), NOW + 60) is False
    assert recheck.schedule_stale_subscription_recheck(
        _stale_account(), NOW + recheck.RECHECK_INTERVAL_SECONDS + 1
    ) is True
    await __import__("asyncio").sleep(0)

    assert scheduler == ["c1", "c1"]


@pytest.mark.asyncio
async def test_a_recently_lapsed_account_is_left_to_apples_own_lag(scheduler, monkeypatch):
    monkeypatch.setattr(settings, "appstore_stale_recheck_enabled", True)

    assert recheck.schedule_stale_subscription_recheck(
        _stale_account(subscription_expires=NOW - 86400), NOW
    ) is False


@pytest.mark.asyncio
async def test_an_incoming_call_on_a_stale_account_schedules_a_recheck(monkeypatch):
    """The call webhook's stale branch hands the account to the scheduler."""
    from app.webhooks import twilio_incoming

    class _Stop(Exception):
        pass

    class _Request:
        async def form(self):
            return {"CallSid": "CA-stale", "From": "+15555550100", "To": "+15555550199"}

    async def fake_lookup(_number):
        return {
            "contractor_id": "c1",
            "owner_phone": "+15555550123",
            "subscription_status": "active",
            "subscription_expires": STALE,
        }

    async def fake_record(*_args, **_kwargs):
        return True

    async def no_measurement(*_args, **_kwargs):
        return None

    handed: list[dict] = []

    def fake_schedule(contractor, now):
        handed.append(contractor)
        raise _Stop()

    monkeypatch.setattr(contractors, "get_contractor_by_twilio_number", fake_lookup)
    monkeypatch.setattr(contractors, "record_inbound_call_evidence", fake_record)
    monkeypatch.setattr(twilio_incoming, "_record_inbound_call_measurement", no_measurement)
    monkeypatch.setattr(recheck, "schedule_stale_subscription_recheck", fake_schedule)
    monkeypatch.setattr("app.services.circuit_breaker.record_error", lambda: None)

    await twilio_incoming.handle_incoming_call(_Request(), None)

    assert [c["contractor_id"] for c in handed] == ["c1"]


# --- the wire ---------------------------------------------------------------


@pytest.fixture
def apple_wire(monkeypatch):
    monkeypatch.setattr(recheck, "_get_appstore_jwt", lambda: "test.jwt.token")
    monkeypatch.setattr(settings, "appstore_environment", "production")


def _status_body(*items) -> dict:
    return {"data": [{"subscriptionGroupIdentifier": "22007035", "lastTransactions": list(items)}]}


@pytest.mark.asyncio
@respx.mock
async def test_the_lookup_falls_back_to_sandbox_and_picks_this_receipt(apple_wire):
    respx.get(f"{APPSTORE_PRODUCTION_URL}/inApps/v1/subscriptions/{ORIGINAL}").mock(
        return_value=httpx.Response(404, json={"errorCode": 4040010})
    )
    other = {**_item(1), "originalTransactionId": "2000000999"}
    ours = _item(2)
    sandbox = respx.get(f"{APPSTORE_SANDBOX_URL}/inApps/v1/subscriptions/{ORIGINAL}").mock(
        return_value=httpx.Response(200, json=_status_body(other, ours))
    )

    assert await recheck._apple_last_transaction(ORIGINAL) == ours
    assert sandbox.calls.last.request.headers["Authorization"] == "Bearer test.jwt.token"


@pytest.mark.asyncio
@respx.mock
async def test_the_lookup_reports_no_record_when_apple_lists_other_receipts(apple_wire):
    respx.get(f"{APPSTORE_PRODUCTION_URL}/inApps/v1/subscriptions/{ORIGINAL}").mock(
        return_value=httpx.Response(200, json=_status_body({**_item(1), "originalTransactionId": "x"}))
    )

    assert await recheck._apple_last_transaction(ORIGINAL) is None


@pytest.mark.asyncio
@respx.mock
async def test_the_lookup_raises_when_apple_cannot_answer(apple_wire):
    respx.get(f"{APPSTORE_PRODUCTION_URL}/inApps/v1/subscriptions/{ORIGINAL}").mock(
        return_value=httpx.Response(500, json={})
    )

    with pytest.raises(RuntimeError):
        await recheck._apple_last_transaction(ORIGINAL)
