"""Tests for the medium-severity security-audit fixes (F-08, F-14, F-15)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import pytest


# ---------------------------------------------------------------------------
# Shared in-memory Firestore stub (mirrors tests/unit/test_subscription_security)
# ---------------------------------------------------------------------------


class _Snapshot:
    def __init__(self, exists: bool, data: Optional[dict], doc_id: str = "", ref=None):
        self.exists = exists
        self._data = data or {}
        self.id = doc_id
        self.reference = ref

    def to_dict(self):
        return dict(self._data)


class _DocRef:
    def __init__(self, fs: "_InMemoryFirestore", path: str):
        self._fs = fs
        self._path = path
        self.id = path.rsplit("/", 1)[-1]

    @property
    def reference(self):
        return self

    def get(self, transaction=None):
        data = self._fs._docs.get(self._path)
        return _Snapshot(data is not None, data, doc_id=self.id, ref=self)

    def set(self, value: dict, merge: bool = False):
        if merge and self._path in self._fs._docs:
            current = self._fs._docs[self._path]
            current.update(value)
        else:
            self._fs._docs[self._path] = dict(value)

    def update(self, value: dict):
        current = self._fs._docs.setdefault(self._path, {})
        current.update(value)

    def delete(self):
        self._fs._docs.pop(self._path, None)

    def collection(self, name: str):
        return _Collection(self._fs, f"{self._path}/{name}")


class _Collection:
    def __init__(self, fs: "_InMemoryFirestore", name: str):
        self._fs = fs
        self._name = name

    def document(self, doc_id: str):
        return _DocRef(self._fs, f"{self._name}/{doc_id}")

    def stream(self):
        prefix = f"{self._name}/"
        for path, data in list(self._fs._docs.items()):
            # Only direct children of this collection (no nested subcoll docs).
            if not path.startswith(prefix):
                continue
            tail = path[len(prefix):]
            if "/" in tail:
                continue
            yield _Snapshot(True, data, doc_id=tail, ref=_DocRef(self._fs, path))

    def where(self, *args, **kwargs):
        # Not used by the helpers under test.
        return self

    def limit(self, _n):
        return self


class _FakeTransaction:
    def __init__(self, fs):
        self._fs = fs

    def set(self, doc_ref: _DocRef, value: dict, merge: bool = False):
        doc_ref.set(value, merge=merge)


class _InMemoryFirestore:
    def __init__(self):
        self._docs: Dict[str, Dict[str, Any]] = {}

    def collection(self, name: str):
        return _Collection(self, name)

    def transaction(self):
        return _FakeTransaction(self)


def _install_fake_firestore(monkeypatch) -> _InMemoryFirestore:
    fake = _InMemoryFirestore()
    # Patch the source binding...
    monkeypatch.setattr(
        "app.db.firestore_client.get_firestore_client",
        lambda: fake,
    )
    # ...and every module that does `from app.db.firestore_client import
    # get_firestore_client` (which copies the symbol into the local
    # namespace at import time).
    for module_name in ("app.db.contacts", "app.db.rate_limits"):
        try:
            import importlib
            mod = importlib.import_module(module_name)
            if hasattr(mod, "get_firestore_client"):
                monkeypatch.setattr(mod, "get_firestore_client", lambda: fake)
        except ImportError:
            pass
    import google.cloud.firestore as fs_mod
    monkeypatch.setattr(fs_mod, "transactional", lambda fn: fn)
    return fake


# ===========================================================================
# F-08: vCard signing uses dedicated HMAC secret
# ===========================================================================


def _reset_vcard_warning():
    from app.services import vcard as vcard_mod
    vcard_mod._warned_about_missing_secret = False


def test_vcard_signing_uses_dedicated_secret(monkeypatch):
    from app.services import vcard as vcard_mod
    _reset_vcard_warning()

    monkeypatch.setattr(vcard_mod.settings, "vcard_hmac_secret", "v-secret-A")
    monkeypatch.setattr(vcard_mod.settings, "api_bearer_token", "bearer-XYZ")
    monkeypatch.setattr(vcard_mod.settings, "cloud_run_url", "https://example")

    url_a = vcard_mod.generate_signed_vcard_url("ctr-1")
    # Extract sig + expires.
    qs = url_a.split("?", 1)[1]
    parts = dict(p.split("=") for p in qs.split("&"))
    assert vcard_mod.verify_vcard_signature("ctr-1", int(parts["expires"]), parts["sig"])

    # Rotating the bearer token alone must NOT invalidate the signature.
    monkeypatch.setattr(vcard_mod.settings, "api_bearer_token", "bearer-DIFFERENT")
    assert vcard_mod.verify_vcard_signature("ctr-1", int(parts["expires"]), parts["sig"])

    # Rotating VCARD_HMAC_SECRET DOES invalidate.
    monkeypatch.setattr(vcard_mod.settings, "vcard_hmac_secret", "v-secret-B")
    assert not vcard_mod.verify_vcard_signature("ctr-1", int(parts["expires"]), parts["sig"])


def test_vcard_signing_falls_back_to_bearer_when_dedicated_unset(monkeypatch, caplog):
    from app.services import vcard as vcard_mod
    _reset_vcard_warning()

    monkeypatch.setattr(vcard_mod.settings, "vcard_hmac_secret", "")
    monkeypatch.setattr(vcard_mod.settings, "api_bearer_token", "bearer-LEGACY")
    monkeypatch.setattr(vcard_mod.settings, "environment", "development")
    monkeypatch.setattr(vcard_mod.settings, "cloud_run_url", "https://example")

    with caplog.at_level("WARNING"):
        url = vcard_mod.generate_signed_vcard_url("ctr-1")
    assert any("VCARD_HMAC_SECRET is not set" in m for m in caplog.messages)

    qs = url.split("?", 1)[1]
    parts = dict(p.split("=") for p in qs.split("&"))
    assert vcard_mod.verify_vcard_signature("ctr-1", int(parts["expires"]), parts["sig"])


def test_vcard_signature_rejects_expired(monkeypatch):
    from app.services import vcard as vcard_mod
    _reset_vcard_warning()

    monkeypatch.setattr(vcard_mod.settings, "vcard_hmac_secret", "secret")
    expired = int(time.time()) - 10
    # Signature is computed over (id, expires) so even a "valid" signature
    # for an expired timestamp must be rejected by the time-check branch.
    import hashlib
    import hmac as _hmac
    sig = _hmac.new(b"secret", f"ctr-1:{expired}".encode(), hashlib.sha256).hexdigest()[:32]
    assert not vcard_mod.verify_vcard_signature("ctr-1", expired, sig)


# ===========================================================================
# F-14: caller_contacts is per-contractor scoped
# ===========================================================================


@pytest.mark.asyncio
async def test_caller_contact_isolation_between_contractors(monkeypatch):
    fake = _install_fake_firestore(monkeypatch)
    from app.db import contacts as contacts_db

    ok = await contacts_db.upsert_caller_contact(
        "ctrA", "+15551234567", {"caller_name": "Alice from A"}
    )
    assert ok is True

    # Ctr B must NOT see Alice via caller_contacts (no legacy fallback hit).
    seen_b = await contacts_db.get_caller_contact("ctrB", "+15551234567")
    assert seen_b is None

    seen_a = await contacts_db.get_caller_contact("ctrA", "+15551234567")
    assert seen_a is not None
    assert seen_a["caller_name"] == "Alice from A"


@pytest.mark.asyncio
async def test_caller_contact_writes_under_contractor_path(monkeypatch):
    fake = _install_fake_firestore(monkeypatch)
    from app.db import contacts as contacts_db

    await contacts_db.upsert_caller_contact(
        "ctr-XYZ", "+15551234567", {"caller_name": "Bob"}
    )
    # The doc must live under the contractor's subcollection path.
    expected = "contractors/ctr-XYZ/caller_contacts/15551234567"
    assert expected in fake._docs
    # And NOT in the global collection.
    assert "caller_contacts/15551234567" not in fake._docs


@pytest.mark.asyncio
async def test_caller_contact_legacy_read_through(monkeypatch):
    fake = _install_fake_firestore(monkeypatch)
    from app.db import contacts as contacts_db

    # Pre-seed legacy global doc (state of the world before migration).
    fake._docs["caller_contacts/15551234567"] = {"caller_name": "Legacy Carol"}

    seen = await contacts_db.get_caller_contact("ctr-1", "+15551234567")
    assert seen is not None
    assert seen["caller_name"] == "Legacy Carol"


@pytest.mark.asyncio
async def test_caller_contact_refuses_write_without_contractor(monkeypatch):
    fake = _install_fake_firestore(monkeypatch)
    from app.db import contacts as contacts_db

    ok = await contacts_db.upsert_caller_contact("", "+15551234567", {"caller_name": "X"})
    assert ok is False
    assert fake._docs == {}


# ===========================================================================
# F-15: dial-in PIN limiter trips after N attempts
# ===========================================================================


@pytest.mark.asyncio
async def test_dial_in_pin_limiter_blocks_after_threshold(monkeypatch):
    _install_fake_firestore(monkeypatch)
    from app.api import forwarding
    from app.config import settings

    # Tighten window for a faster test; the helper reads settings at call time.
    monkeypatch.setattr(settings, "pin_rate_limit", 5)
    monkeypatch.setattr(settings, "pin_rate_window_seconds", 3600)

    caller = "+15550001234"
    for i in range(5):
        result = await forwarding.check_dial_in_pin_attempt(caller)
        assert result.allowed is True, f"attempt {i} should be allowed"

    blocked = await forwarding.check_dial_in_pin_attempt(caller)
    assert blocked.allowed is False
    assert blocked.retry_after_seconds > 0


@pytest.mark.asyncio
async def test_dial_in_pin_limiter_keys_are_independent(monkeypatch):
    _install_fake_firestore(monkeypatch)
    from app.api import forwarding
    from app.config import settings

    monkeypatch.setattr(settings, "pin_rate_limit", 3)
    monkeypatch.setattr(settings, "pin_rate_window_seconds", 3600)

    for _ in range(3):
        await forwarding.check_dial_in_pin_attempt("+15550000001")

    # Different source — fresh budget.
    fresh = await forwarding.check_dial_in_pin_attempt("+15550000002")
    assert fresh.allowed is True


@pytest.mark.asyncio
async def test_dial_in_pin_limiter_lockout_does_not_leak_remaining(monkeypatch):
    """Once locked out we must NOT advertise remaining attempts to callers."""
    _install_fake_firestore(monkeypatch)
    from app.api import forwarding
    from app.config import settings

    monkeypatch.setattr(settings, "pin_rate_limit", 2)
    monkeypatch.setattr(settings, "pin_rate_window_seconds", 3600)

    await forwarding.check_dial_in_pin_attempt("+15550009999")
    await forwarding.check_dial_in_pin_attempt("+15550009999")
    blocked = await forwarding.check_dial_in_pin_attempt("+15550009999")

    assert blocked.allowed is False
    assert blocked.remaining == 0
    # The webhook layer is expected to send a generic message; here we only
    # assert the limiter exposes no "you have N attempts left" hint to the
    # caller path (remaining is 0 once exceeded).


@pytest.mark.asyncio
async def test_dial_in_pin_limiter_record_failure_helper_no_op_safe(monkeypatch):
    _install_fake_firestore(monkeypatch)
    from app.api import forwarding
    from app.config import settings

    monkeypatch.setattr(settings, "pin_rate_limit", 1)
    monkeypatch.setattr(settings, "pin_rate_window_seconds", 3600)

    # First call burns the slot.
    first = await forwarding.check_dial_in_pin_attempt("+15550001111")
    assert first.allowed is True

    # record_dial_in_pin_failure runs without raising even after lockout.
    await forwarding.record_dial_in_pin_failure("+15550001111")
    await forwarding.record_dial_in_pin_failure("+15550001111")
