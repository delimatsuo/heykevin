"""Contact sync whitelists every contact, however the app splits the upload.

Installed iOS builds send an address book of more than 200 numbers as
back-to-back 200-number requests and put contacts_hash only on the last one.
Before this fix the 5-minute cooldown dropped every request after the first,
so only the first 200 numbers (sorted by first name) were ever whitelisted, and
contacts later in the alphabet were screened by Kevin.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.api import contacts as contacts_api
from app.db import contacts as contacts_db
from app.utils.phone import phone_hash

CID = "ctr-sync"
AREA_CODES = (202, 212, 312, 415, 617, 713)


def _numbers(count: int) -> list[str]:
    """Distinct fictional (555-01XX) US numbers in the format iPhone contacts use."""
    numbers = [f"({area}) 555-01{line:02d}" for area in AREA_CODES for line in range(100)]
    assert count <= len(numbers)
    return numbers[:count]


def _e164(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    return f"+1{digits}"


class _Snapshot:
    def __init__(self, ref: _DocRef, data: dict | None):
        self.reference = ref
        self.id = ref.id
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> dict:
        return dict(self._data or {})


class _DocRef:
    def __init__(self, fs: _Firestore, path: str):
        self._fs = fs
        self.path = path
        self.id = path.rsplit("/", 1)[-1]

    def get(self):
        return _Snapshot(self, self._fs.docs.get(self.path))

    def collection(self, name: str) -> _Collection:
        return _Collection(self._fs, f"{self.path}/{name}")


class _Query:
    def __init__(self, fs: _Firestore, path: str, filters: tuple = ()):
        self._fs = fs
        self._path = path
        self._filters = filters

    def where(self, field: str, op: str, value):
        assert op == "=="
        return _Query(self._fs, self._path, self._filters + ((field, value),))

    def stream(self):
        prefix = f"{self._path}/"
        for path, data in list(self._fs.docs.items()):
            tail = path[len(prefix):] if path.startswith(prefix) else ""
            if not tail or "/" in tail:
                continue
            if all(data.get(field) == value for field, value in self._filters):
                yield _Snapshot(_DocRef(self._fs, path), data)


class _Collection(_Query):
    def document(self, doc_id: str) -> _DocRef:
        return _DocRef(self._fs, f"{self._path}/{doc_id}")


class _Batch:
    """Buffers writes until commit, like a Firestore WriteBatch."""

    def __init__(self, fs: _Firestore):
        self._fs = fs
        self._ops: list = []

    def set(self, ref: _DocRef, data: dict, merge: bool = False):
        self._ops.append(("set", ref.path, dict(data), merge))

    def update(self, ref: _DocRef, data: dict):
        self._ops.append(("update", ref.path, dict(data), True))

    def commit(self):
        if self._fs.fail_commits:
            raise RuntimeError("firestore unavailable")
        for _kind, path, data, merge in self._ops:
            if merge and path in self._fs.docs:
                self._fs.docs[path].update(data)
            else:
                self._fs.docs[path] = data
        self._fs.commits += 1


class _Firestore:
    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.commits = 0
        self.fail_commits = False

    def collection(self, name: str) -> _Collection:
        return _Collection(self, name)

    def batch(self) -> _Batch:
        return _Batch(self)


@pytest.fixture
def env(monkeypatch):
    fs = _Firestore()
    contractors = {CID: {"contractor_id": CID, "country_code": "US"}}
    clock = {"now": 1_000_000.0}

    async def get_contractor(contractor_id):
        doc = contractors.get(contractor_id)
        return dict(doc) if doc is not None else None

    async def update_contractor(contractor_id, updates):
        contractors[contractor_id].update(updates)
        return True

    monkeypatch.setattr(contacts_db, "get_firestore_client", lambda: fs)
    monkeypatch.setattr("app.db.contractors.get_contractor", get_contractor)
    monkeypatch.setattr("app.db.contractors.update_contractor", update_contractor)
    monkeypatch.setattr(contacts_api, "require_contractor_access", lambda *_a, **_k: None)
    monkeypatch.setattr(contacts_api, "time", SimpleNamespace(time=lambda: clock["now"]))
    return SimpleNamespace(fs=fs, contractor=contractors[CID], clock=clock)


async def _post(env, numbers: list[str], *, contacts_hash: str = "", at: float) -> dict:
    env.clock["now"] = at
    body = contacts_api.BulkSyncRequest(
        contacts=[{"name": f"Contact {i}", "phone": n} for i, n in enumerate(numbers)],
        contractor_id=CID,
        contacts_hash=contacts_hash,
    )
    return await contacts_api.api_bulk_sync(object(), body)


async def _installed_app_upload(env, numbers: list[str], *, contacts_hash: str, start: float) -> list[dict]:
    """Mirror ContactSyncManager in shipped builds: 200 per request, hash on the last."""
    responses = []
    chunks = [numbers[i:i + 200] for i in range(0, len(numbers), 200)]
    for index, chunk in enumerate(chunks):
        is_last = index == len(chunks) - 1
        responses.append(await _post(
            env, chunk, contacts_hash=contacts_hash if is_last else "", at=start + index,
        ))
    return responses


def _contact(env, number: str) -> dict | None:
    return env.fs.docs.get(f"contractors/{CID}/contacts/{phone_hash(_e164(number))}")


def _whitelisted(env) -> set[str]:
    prefix = f"contractors/{CID}/contacts/"
    return {
        data["phone"] for path, data in env.fs.docs.items()
        if path.startswith(prefix) and data.get("is_whitelisted")
    }


@pytest.mark.asyncio
async def test_installed_app_batched_upload_whitelists_every_contact(env):
    numbers = _numbers(450)

    responses = await _installed_app_upload(env, numbers, contacts_hash="h1", start=1_000_000)

    assert [r["status"] for r in responses] == ["ok", "ok", "ok"]
    assert _whitelisted(env) == {_e164(n) for n in numbers}
    # The caller who reached Kevin was past the first 200 numbers.
    last = numbers[-1]
    assert (await contacts_db.get_contact(_e164(last), contractor_id=CID))["is_whitelisted"] is True
    assert env.contractor["contacts_hash"] == "h1"
    assert env.contractor["contact_sync_id"] == ""


@pytest.mark.asyncio
async def test_batched_upload_removes_only_contacts_no_longer_on_the_phone(env):
    first = _numbers(450)
    await _installed_app_upload(env, first, contacts_hash="h1", start=1_000_000)
    manual = "+12025550999"
    env.fs.docs[f"contractors/{CID}/contacts/{phone_hash(manual)}"] = {
        "phone": manual, "is_whitelisted": True, "trust_level": 100,
    }

    # One contact deleted from the phone; the rest re-sent an hour later.
    deleted = first[10]
    second = [n for n in first if n != deleted]
    responses = await _installed_app_upload(env, second, contacts_hash="h2", start=1_003_600)

    assert [r["status"] for r in responses] == ["ok", "ok", "ok"]
    assert responses[-1]["removed"] == 1
    assert _contact(env, deleted)["is_whitelisted"] is False
    assert _whitelisted(env) == {_e164(n) for n in second} | {manual}

    # A contact removed earlier is not counted or rewritten by later syncs.
    responses = await _installed_app_upload(env, second, contacts_hash="h3", start=1_007_200)
    assert responses[-1]["removed"] == 0


@pytest.mark.asyncio
async def test_resent_contacts_stay_whitelisted_when_the_sync_completes_in_one_request(env):
    numbers = _numbers(150)
    await _post(env, numbers, contacts_hash="h1", at=1_000_000)

    # Same contacts from a reinstalled app with a different hash input order.
    response = await _post(env, numbers, contacts_hash="h1-reordered", at=1_003_600)

    assert response == {"status": "ok", "synced": 150, "removed": 0}
    assert _whitelisted(env) == {_e164(n) for n in numbers}


@pytest.mark.asyncio
async def test_a_new_sync_inside_the_cooldown_is_rate_limited(env):
    await _installed_app_upload(env, _numbers(450), contacts_hash="h1", start=1_000_000)

    response = await _post(env, _numbers(5), contacts_hash="h2", at=1_000_060)

    assert response["status"] == "rate_limited"
    assert env.contractor["contacts_hash"] == "h1"


@pytest.mark.asyncio
async def test_a_request_after_the_batch_window_cannot_continue_a_stale_sync(env):
    numbers = _numbers(450)
    await _post(env, numbers[:200], at=1_000_000)

    late = await _post(env, numbers[200:], contacts_hash="h1", at=1_000_000 + 121)

    assert late["status"] == "rate_limited"
    assert _whitelisted(env) == {_e164(n) for n in numbers[:200]}


@pytest.mark.asyncio
async def test_unchanged_single_request_sync_is_skipped_without_writes(env):
    numbers = _numbers(50)
    await _post(env, numbers, contacts_hash="h1", at=1_000_000)
    commits = env.fs.commits

    response = await _post(env, numbers, contacts_hash="h1", at=1_003_600)

    assert response == {"status": "skipped", "reason": "no_changes"}
    assert env.fs.commits == commits


@pytest.mark.asyncio
async def test_a_sync_over_the_cap_never_sweeps(env, monkeypatch):
    monkeypatch.setattr(contacts_api, "MAX_CONTACTS_PER_SYNC", 300)
    numbers = _numbers(450)
    await _installed_app_upload(env, numbers[:200], contacts_hash="h0", start=990_000)

    responses = await _installed_app_upload(env, numbers, contacts_hash="h1", start=1_000_000)

    assert [r["status"] for r in responses] == ["ok", "rejected", "rejected"]
    assert _whitelisted(env) == {_e164(n) for n in numbers[:200]}
    assert env.contractor["contacts_hash"] == "h0"


@pytest.mark.asyncio
async def test_local_numbers_use_the_owners_country(env):
    env.contractor["country_code"] = "BR"

    response = await _post(env, ["(11) 98765-4321"], contacts_hash="h1", at=1_000_000)

    assert response["synced"] == 1
    contact = await contacts_db.get_contact("+5511987654321", contractor_id=CID)
    assert contact["is_whitelisted"] is True


@pytest.mark.asyncio
async def test_a_failed_write_does_not_record_the_hash(env):
    env.fs.fail_commits = True

    response = await _post(env, _numbers(20), contacts_hash="h1", at=1_000_000)

    assert response == {"status": "error", "synced": 0, "removed": 0}
    assert "contacts_hash" not in env.contractor
