"""App Store notifications that match no account are kept and, for charges, alerted.

On 2026-10-10 a production DID_RENEW matched no Kevin account. The webhook
acked it 200, so Apple never retried, and one warning line, gone after 30
days, was the only sign that someone was paying for nothing. Now each
unmatched notification is stored in `appstore_unmatched_notifications`
(keyed by Apple's notificationUUID), and a charge logs at ERROR, which the
production error alert emails.
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest  # noqa: E402

from app.db import appstore_unmatched  # noqa: E402
from app.services import subscription as sub_service  # noqa: E402

TOKEN = "c2953dfe-0000-4000-8000-000000000000"


def _transaction(**overrides) -> dict:
    transaction = {
        "appAccountToken": TOKEN,
        "productId": "com.kevin.callscreen.personal.monthly",
        "transactionId": "tx-2",
        "originalTransactionId": "otx-1",
        "environment": "Production",
        "purchaseDate": 1791640000000,
        "expiresDate": 4102444800000,
    }
    transaction.update(overrides)
    return transaction


def _payload(notification_type="DID_RENEW", **extra) -> dict:
    payload = {
        "notificationType": notification_type,
        "notificationUUID": "notif-1",
        "signedDate": 1791640864000,
        "data": {"signedTransactionInfo": "signed-transaction", "environment": "Production"},
    }
    payload.update(extra)
    return payload


@pytest.fixture
def unmatched(monkeypatch):
    """No account carries the token; capture what gets recorded."""
    state = {"records": [], "found": None, "binding": None, "transaction": _transaction()}

    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda _s: dict(state["transaction"]))

    async def fake_lookup(subscription_uuid, include_inactive=False):
        return state["found"] if include_inactive else None

    async def fake_binding(original_id):
        if isinstance(state["binding"], Exception):
            raise state["binding"]
        return state["binding"]

    async def fake_record(notification_id, fields):
        if isinstance(state.get("record_error"), Exception):
            raise state["record_error"]
        state["records"].append((notification_id, fields))

    async def no_update(*_args, **_kwargs):
        raise AssertionError("an unmatched notification must not write to any account")

    monkeypatch.setattr("app.db.contractors.get_contractor_by_subscription_uuid", fake_lookup)
    monkeypatch.setattr("app.db.apple_transactions.get_transaction_binding", fake_binding)
    monkeypatch.setattr(appstore_unmatched, "record_unmatched_notification", fake_record)
    monkeypatch.setattr("app.db.contractors.update_contractor", no_update)
    return state


def _levels(caplog, text):
    return [r.levelno for r in caplog.records if text in r.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize("charge", ["DID_RENEW", "SUBSCRIBED"])
async def test_an_unmatched_charge_is_recorded_and_alerts(unmatched, caplog, charge):
    unmatched["binding"] = {"contractor_id": "c-bound"}

    with caplog.at_level(logging.INFO, logger="app.services.subscription"):
        handled = await sub_service.handle_appstore_notification(_payload(charge))

    assert handled is False
    assert _levels(caplog, "App Store charge matched no account") == [logging.ERROR]
    message = next(r.getMessage() for r in caplog.records if "matched no account" in r.getMessage())
    assert "env=Production" in message and "product=com.kevin.callscreen.personal.monthly" in message
    assert "token_prefix=c2953dfe" in message and "original_tx=otx-1" in message
    assert "bound_to=c-bound" in message
    assert TOKEN not in message

    [(notification_id, fields)] = unmatched["records"]
    assert notification_id == "notif-1"
    assert fields["notification_type"] == charge
    assert fields["is_charge"] is True
    assert fields["environment"] == "Production"
    assert fields["product_id"] == "com.kevin.callscreen.personal.monthly"
    assert fields["original_transaction_id"] == "otx-1"
    assert fields["transaction_id"] == "tx-2"
    assert fields["app_account_token"] == TOKEN
    assert fields["signed_date"] == 1791640864000
    assert fields["lookup"] == "no_account"
    assert fields["bound_contractor_id"] == "c-bound"


@pytest.mark.asyncio
@pytest.mark.parametrize("wind_down", ["EXPIRED", "DID_FAIL_TO_RENEW", "REFUND"])
async def test_an_unmatched_non_charge_is_recorded_without_alerting(unmatched, caplog, wind_down):
    with caplog.at_level(logging.INFO, logger="app.services.subscription"):
        handled = await sub_service.handle_appstore_notification(_payload(wind_down))

    assert handled is False
    assert _levels(caplog, "App Store notification matched no account") == [logging.WARNING]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    [(_id, fields)] = unmatched["records"]
    assert fields["is_charge"] is False and fields["notification_type"] == wind_down


@pytest.mark.asyncio
async def test_a_document_with_an_unexpected_active_value_is_named(unmatched):
    unmatched["found"] = {"contractor_id": "c-odd", "active": None}

    await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    [(_id, fields)] = unmatched["records"]
    assert fields["lookup"] == "unexpected_active"
    assert fields["found_contractor_id"] == "c-odd"
    assert fields["found_active_type"] == "NoneType"


@pytest.mark.asyncio
async def test_a_redelivery_without_a_notification_id_still_has_a_stable_key(unmatched):
    payload = _payload("DID_RENEW")
    del payload["notificationUUID"]

    await sub_service.handle_appstore_notification(payload)
    await sub_service.handle_appstore_notification(payload)

    ids = [notification_id for notification_id, _fields in unmatched["records"]]
    assert ids == ["otx-1:tx-2:DID_RENEW", "otx-1:tx-2:DID_RENEW"]


@pytest.mark.asyncio
async def test_failing_to_record_still_alerts_and_acks(unmatched, caplog):
    unmatched["record_error"] = RuntimeError("firestore down")

    with caplog.at_level(logging.INFO, logger="app.services.subscription"):
        handled = await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    assert handled is False
    assert _levels(caplog, "App Store charge matched no account") == [logging.ERROR]
    assert _levels(caplog, "Unmatched App Store notification not recorded") == [logging.ERROR]


@pytest.mark.asyncio
async def test_a_failed_binding_lookup_still_records(unmatched):
    unmatched["binding"] = RuntimeError("firestore blip")

    await sub_service.handle_appstore_notification(_payload("DID_RENEW"))

    [(_id, fields)] = unmatched["records"]
    assert fields["bound_contractor_id"] == ""


@pytest.mark.asyncio
async def test_an_unmatched_renewal_status_change_is_recorded(unmatched, monkeypatch):
    """DID_CHANGE_RENEWAL_STATUS has its own lookup branch; it records too."""
    renewal = {
        "autoRenewStatus": 0,
        "signedDate": 1791640864000,
        "appAccountToken": TOKEN,
        "originalTransactionId": "otx-1",
        "productId": "com.kevin.callscreen.personal.monthly",
        "environment": "Production",
    }
    decoded = {"signed-transaction": _transaction(), "signed-renewal": renewal}
    monkeypatch.setattr(sub_service, "_decode_jws_payload", lambda s: dict(decoded[s]))
    payload = _payload(
        "DID_CHANGE_RENEWAL_STATUS",
        subtype="AUTO_RENEW_DISABLED",
        data={"signedTransactionInfo": "signed-transaction", "signedRenewalInfo": "signed-renewal"},
    )

    handled = await sub_service.handle_appstore_notification(payload)

    assert handled is False
    [(_id, fields)] = unmatched["records"]
    assert fields["notification_type"] == "DID_CHANGE_RENEWAL_STATUS"
    assert fields["subtype"] == "AUTO_RENEW_DISABLED"
    assert fields["is_charge"] is False


# --- the store --------------------------------------------------------------


class _Snapshot:
    def __init__(self, data):
        self.exists = data is not None


class _Ref:
    def __init__(self, store, doc_id):
        self.store, self.doc_id = store, doc_id

    def get(self):
        return _Snapshot(self.store.get(self.doc_id))

    def set(self, data):
        self.store[self.doc_id] = dict(data)

    def update(self, data):
        for key, value in data.items():
            if key == "deliveries":
                value = self.store[self.doc_id]["deliveries"] + 1  # stands in for Increment(1)
            self.store[self.doc_id][key] = value


class _Db:
    def __init__(self, store):
        self.store = store
        self.collections = []

    def collection(self, name):
        self.collections.append(name)
        return self

    def document(self, doc_id):
        return _Ref(self.store, doc_id)


@pytest.mark.asyncio
async def test_the_first_delivery_is_stored_and_a_redelivery_is_counted(monkeypatch):
    store: dict = {}
    db = _Db(store)
    monkeypatch.setattr(appstore_unmatched, "get_firestore_client", lambda: db)

    await appstore_unmatched.record_unmatched_notification("notif-1", {"notification_type": "DID_RENEW"})
    first_seen = store["notif-1"]["first_seen_at"]
    await appstore_unmatched.record_unmatched_notification("notif-1", {"notification_type": "DID_RENEW"})

    assert db.collections == ["appstore_unmatched_notifications"] * 2
    assert store["notif-1"]["notification_type"] == "DID_RENEW"
    assert store["notif-1"]["deliveries"] == 2
    assert store["notif-1"]["first_seen_at"] == first_seen
    assert store["notif-1"]["last_seen_at"] >= first_seen
