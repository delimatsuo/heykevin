"""Housekeeping races and batch bounds without datastore/provider access."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest

from app.services import background_maintenance as maintenance


def test_orphan_transaction_preserves_concurrently_refreshed_call():
    old = {"state_updated_at": 1000, "status": "screening"}
    fresh = {"state_updated_at": 9999, "status": "screening"}
    assert maintenance._delete_if_stale(old, 10000) is None
    assert maintenance._delete_if_stale(fresh, 10000) is fresh
    assert maintenance._delete_if_stale(None, 10000) is None
    assert maintenance._delete_if_stale({"state_updated_at": 0}, 10000) == {"state_updated_at": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("counts,expected", [([500]*4, 4), ([500, 2], 2), ([0], 1)])
async def test_retention_preserves_four_batch_cap_and_short_batch_stop(monkeypatch, counts, expected):
    from app.db import calls
    cleanup = AsyncMock(side_effect=counts)
    monkeypatch.setattr(calls, "cleanup_old_calls", cleanup)
    await maintenance.call_retention_once()
    assert cleanup.await_count == expected


@pytest.mark.asyncio
async def test_trial_transaction_does_not_clobber_paid_activation(monkeypatch):
    from google.cloud import firestore
    from app.db import firestore_client
    from app.services import subscription
    selected = {"subscription_status": "trial", "trial_start": 1}
    activated = {"subscription_status": "active", "trial_start": 1}
    updates = []
    ref = SimpleNamespace(get=lambda **kwargs: SimpleNamespace(exists=True, to_dict=lambda: activated))
    collection = SimpleNamespace(document=lambda _: ref)
    collection.where = lambda *args: SimpleNamespace(stream=lambda: [SimpleNamespace(id="c", to_dict=lambda: selected)])
    db = SimpleNamespace(collection=lambda _: collection, transaction=lambda: SimpleNamespace(
        update=lambda *args: updates.append(args)))
    monkeypatch.setattr(firestore_client, "get_firestore_client", lambda: db)
    monkeypatch.setattr(firestore, "transactional", lambda function: function)
    monkeypatch.setattr(subscription, "should_expire_trial", lambda data, now: data["subscription_status"] == "trial")
    await maintenance.lapsed_trials_once(strict=True)
    assert updates == []
