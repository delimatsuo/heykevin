"""Unit tests for Settings country_code exposure, validation, and persistence."""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from types import SimpleNamespace
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.api import settings as settings_api
from app.api.settings import (
    SettingsUpdate,
    api_get_settings,
    api_update_settings,
    _get_settings,
    _DEFAULT_SETTINGS,
)
from app.db.contractors import SUPPORTED_COUNTRIES


def _auth_request(contractor_id: str = "contractor-1", is_admin: bool = False):
    return SimpleNamespace(state=SimpleNamespace(is_admin=is_admin, contractor_id=contractor_id))


class _FakeDocSnapshot:
    def __init__(self, data: dict | None, exists: bool = True):
        self._data = data
        self.exists = exists

    def to_dict(self):
        return self._data if self._data is not None else {}


class _FakeDocRef:
    def __init__(self, data: dict | None = None, exists: bool = True):
        self.data = data if data is not None else {}
        self.exists = exists
        self.updated = {}
        self.set_data = {}
        self.collections = {}
        self.raise_on_get = False
        self.raise_on_update = False
        self.raise_on_set = False

    def get(self):
        if self.raise_on_get:
            raise RuntimeError("Firestore get failure")
        return _FakeDocSnapshot(
            self.data.copy() if self.data is not None else None, exists=self.exists
        )

    def update(self, updates: dict):
        if self.raise_on_update:
            raise RuntimeError("Firestore update failure")
        self.updated.update(updates)
        if self.data is not None:
            self.data.update(updates)
        else:
            self.data = updates.copy()

    def set(self, data: dict, merge: bool = False):
        if self.raise_on_set:
            raise RuntimeError("Firestore set failure")
        self.set_data.update(data)
        if self.data is not None and merge:
            self.data.update(data)
        else:
            self.data = data.copy()

    def collection(self, name: str):
        if name not in self.collections:
            self.collections[name] = _FakeCollectionRef()
        return self.collections[name]


class _FakeCollectionRef:
    def __init__(self):
        self.docs = {}

    def document(self, doc_id: str):
        if doc_id not in self.docs:
            self.docs[doc_id] = _FakeDocRef()
        return self.docs[doc_id]


class _FakeBatch:
    def __init__(self, db):
        self.db = db
        self.operations = []

    def update(self, doc_ref, updates: dict):
        self.operations.append(("update", doc_ref, updates, None))

    def set(self, doc_ref, data: dict, merge: bool = False):
        self.operations.append(("set", doc_ref, data, merge))

    def commit(self):
        if self.db.raise_on_batch_commit:
            raise RuntimeError("Firestore batch failure")

        # Firestore batches are atomic: validate every staged operation before
        # applying any of them so the fake cannot hide partial-write bugs.
        for operation, doc_ref, _, _ in self.operations:
            if operation == "update" and doc_ref.raise_on_update:
                raise RuntimeError("Firestore update failure")
            if operation == "set" and doc_ref.raise_on_set:
                raise RuntimeError("Firestore set failure")

        for operation, doc_ref, data, merge in self.operations:
            if operation == "update":
                doc_ref.update(data)
            else:
                doc_ref.set(data, merge=bool(merge))
        self.db.batch_commit_count += 1


class _FakeFirestoreDB:
    def __init__(self):
        self.collections = {}
        self.raise_on_batch_commit = False
        self.batch_commit_count = 0

    def collection(self, name: str):
        if name not in self.collections:
            self.collections[name] = _FakeCollectionRef()
        return self.collections[name]

    def batch(self):
        return _FakeBatch(self)


# ===========================================================================
# 1. SettingsUpdate Validation & Normalization
# ===========================================================================


def test_settings_update_supported_country_normalization():
    """Proves all supported country codes are trimmed and uppercased."""
    for code in SUPPORTED_COUNTRIES:
        # Lowercase test
        update_lower = SettingsUpdate(country_code=code.lower())
        assert update_lower.country_code == code

        # Whitespace test
        update_ws = SettingsUpdate(country_code=f"  {code.lower()}  ")
        assert update_ws.country_code == code


def test_settings_update_none_and_blank_preserve_no_change():
    """Proves None and blank strings (empty or whitespace) normalize to None (no-change semantics)."""
    assert SettingsUpdate().country_code is None
    assert SettingsUpdate(country_code=None).country_code is None
    assert SettingsUpdate(country_code="").country_code is None
    assert SettingsUpdate(country_code="   ").country_code is None


def test_settings_update_unsupported_nonblank_fails_validation():
    """Proves unsupported nonblank input raises ValidationError before any persistence write."""
    for invalid in ["XX", "invalid", "USA", "12", "JP", "us-east-1", 123]:
        with pytest.raises(ValidationError):
            SettingsUpdate(country_code=invalid)


@pytest.mark.asyncio
async def test_http_unsupported_country_returns_422_before_firestore(monkeypatch):
    """Proves FastAPI rejects unsupported input before endpoint persistence code runs."""
    monkeypatch.setattr(
        settings_api,
        "get_firestore_client",
        lambda: pytest.fail("Firestore must not be accessed for invalid input"),
    )
    monkeypatch.setattr(
        settings_api,
        "require_contractor_access",
        lambda request, contractor_id: None,
    )
    test_app = FastAPI()
    test_app.include_router(settings_api.router)
    test_app.dependency_overrides[settings_api.verify_api_token] = lambda: None

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.put(
            "/api/settings?contractor_id=c1",
            json={"country_code": "JP"},
        )

    assert response.status_code == 422


# ===========================================================================
# 2. GET /api/settings Root Exposure, Precedence & Fallbacks
# ===========================================================================


@pytest.mark.asyncio
async def test_get_settings_returns_root_country_code(monkeypatch):
    """Proves GET /api/settings exposes country_code sourced from root contractor document."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    root_doc.data = {"country_code": "CA", "business_name": "Acme North"}
    root_doc.exists = True

    pref_doc = root_doc.collection("settings").document("preferences")
    pref_doc.data = {"greeting_name": "Alice"}
    pref_doc.exists = True

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    req = _auth_request("c1")
    res = await api_get_settings(req, contractor_id="c1")

    assert res["country_code"] == "CA"
    assert res["greeting_name"] == "Alice"
    assert res["quiet_hours_enabled"] == _DEFAULT_SETTINGS["quiet_hours_enabled"]


@pytest.mark.asyncio
async def test_get_settings_preferences_cannot_spoof_root_country_code(monkeypatch):
    """Proves stored preferences subdocument cannot spoof or override root country_code."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    root_doc.data = {"country_code": "GB"}
    root_doc.exists = True

    # Preferences document attempts to claim "FR"
    pref_doc = root_doc.collection("settings").document("preferences")
    pref_doc.data = {"country_code": "FR", "greeting_name": "Bob"}
    pref_doc.exists = True

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    req = _auth_request("c1")
    res = await api_get_settings(req, contractor_id="c1")

    assert res["country_code"] == "GB"
    assert res["greeting_name"] == "Bob"


@pytest.mark.asyncio
async def test_get_settings_preferences_cannot_override_root_fallback(monkeypatch):
    """Proves preferences cannot inject country_code when root document is missing or invalid."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    root_doc.data = {"country_code": "UNSUPPORTED"}
    root_doc.exists = True

    # Preferences document attempts to supply "DE"
    pref_doc = root_doc.collection("settings").document("preferences")
    pref_doc.data = {"country_code": "DE"}
    pref_doc.exists = True

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    req = _auth_request("c1")
    res = await api_get_settings(req, contractor_id="c1")

    # Root fallback to US must win over preferences "DE"
    assert res["country_code"] == "US"


@pytest.mark.asyncio
async def test_get_settings_fallback_for_missing_root_doc(monkeypatch):
    """Proves GET /api/settings falls back to 'US' when the root contractor document does not exist."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    root_doc.exists = False

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    req = _auth_request("c1")
    res = await api_get_settings(req, contractor_id="c1")
    assert res["country_code"] == "US"


@pytest.mark.asyncio
async def test_get_settings_fallback_for_missing_or_blank_country_field(monkeypatch):
    """Proves missing, None, empty string, or whitespace-only root country_code falls back to 'US'."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    root_doc.exists = True

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    for blank_val in [{}, {"country_code": None}, {"country_code": ""}, {"country_code": "   "}]:
        root_doc.data = blank_val
        res = await _get_settings("c1")
        assert res["country_code"] == "US"


@pytest.mark.asyncio
async def test_get_settings_fallback_for_malformed_or_unsupported_root_field(monkeypatch):
    """Proves malformed or unsupported root country_code values fall back to 'US'."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    root_doc.exists = True

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    for bad_val in ["ZZ", "INVALID", 12345, True, False, ["US"], {"code": "US"}]:
        root_doc.data = {"country_code": bad_val}
        res = await _get_settings("c1")
        assert res["country_code"] == "US"


@pytest.mark.asyncio
async def test_get_settings_normalizes_stored_lowercase_and_whitespace(monkeypatch):
    """Proves stored root values with lowercase or surrounding whitespace normalize to canonical uppercase."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    root_doc.exists = True

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    root_doc.data = {"country_code": " br "}
    res = await _get_settings("c1")
    assert res["country_code"] == "BR"

    root_doc.data = {"country_code": "de"}
    res = await _get_settings("c1")
    assert res["country_code"] == "DE"


@pytest.mark.asyncio
async def test_get_settings_handles_firestore_read_exceptions_gracefully(monkeypatch):
    """Proves get_settings handles Firestore exceptions gracefully with deterministic fallback."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    pref_doc = root_doc.collection("settings").document("preferences")
    pref_doc.data = {"greeting_name": "Still available"}
    pref_doc.exists = True
    root_doc.raise_on_get = True

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    res = await _get_settings("c1")
    assert res["country_code"] == "US"
    assert res["greeting_name"] == "Still available"
    assert res["quiet_hours_start"] == "22:00"


@pytest.mark.asyncio
async def test_get_settings_wrong_contractor_authorization_raises_forbidden(monkeypatch):
    """Proves unauthorized / mismatched contractor access is rejected before reading Firestore."""
    monkeypatch.setattr(
        settings_api,
        "get_firestore_client",
        lambda: pytest.fail("Firestore must not be accessed before authorization"),
    )
    req = _auth_request("contractor-1")
    with pytest.raises(HTTPException) as exc_info:
        await api_get_settings(req, contractor_id="other-contractor")
    assert exc_info.value.status_code == 403


# ===========================================================================
# 3. PUT /api/settings Persistence, Isolation & No-Write Guarantees
# ===========================================================================


@pytest.mark.asyncio
async def test_put_settings_persists_valid_country_code_to_root_only(monkeypatch):
    """Proves valid country_code updates only root contractor document and returns updated code."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    root_doc.data = {"country_code": "US"}
    root_doc.exists = True

    pref_doc = root_doc.collection("settings").document("preferences")
    pref_doc.data = {"greeting_name": "Alice"}
    pref_doc.exists = True

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    req = _auth_request("c1")
    body = SettingsUpdate(country_code="ca")  # lowercase input
    res = await api_update_settings(req, body, contractor_id="c1")

    assert root_doc.updated == {"country_code": "CA"}
    # Preferences subdocument was not written since only country_code was passed
    assert pref_doc.set_data == {}
    assert res["country_code"] == "CA"
    assert res["greeting_name"] == "Alice"


@pytest.mark.asyncio
async def test_put_settings_persists_country_code_and_preferences_separately(monkeypatch):
    """Proves PUT atomically batches root country_code and preferences updates."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    root_doc.data = {"country_code": "US"}
    root_doc.exists = True

    pref_doc = root_doc.collection("settings").document("preferences")
    pref_doc.data = {"quiet_hours_enabled": True}
    pref_doc.exists = True

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    req = _auth_request("c1")
    body = SettingsUpdate(country_code="DE", greeting_name="Hallo")
    res = await api_update_settings(req, body, contractor_id="c1")

    assert root_doc.updated == {"country_code": "DE"}
    assert pref_doc.set_data == {"greeting_name": "Hallo"}
    assert "country_code" not in pref_doc.set_data
    assert pref_doc.data["quiet_hours_enabled"] is True
    assert fake_db.batch_commit_count == 1
    assert res["country_code"] == "DE"
    assert res["greeting_name"] == "Hallo"


@pytest.mark.asyncio
async def test_put_settings_omitted_none_blank_does_not_write_root_country(monkeypatch):
    """Proves omitted, None, or blank country_code performs zero root country writes."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    root_doc.data = {"country_code": "BR"}
    root_doc.exists = True

    pref_doc = root_doc.collection("settings").document("preferences")
    pref_doc.data = {}
    pref_doc.exists = True

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)
    req = _auth_request("c1")

    # 1. Omitted
    body1 = SettingsUpdate(greeting_name="Update 1")
    res1 = await api_update_settings(req, body1, contractor_id="c1")
    assert root_doc.updated == {}
    assert res1["country_code"] == "BR"
    assert res1["greeting_name"] == "Update 1"

    # 2. Explicit None
    body2 = SettingsUpdate(greeting_name="Update 2", country_code=None)
    res2 = await api_update_settings(req, body2, contractor_id="c1")
    assert root_doc.updated == {}
    assert res2["country_code"] == "BR"
    assert res2["greeting_name"] == "Update 2"

    # 3. Blank string
    body3 = SettingsUpdate(greeting_name="Update 3", country_code="   ")
    res3 = await api_update_settings(req, body3, contractor_id="c1")
    assert root_doc.updated == {}
    assert res3["country_code"] == "BR"
    assert res3["greeting_name"] == "Update 3"


@pytest.mark.asyncio
async def test_put_settings_wrong_contractor_authorization_raises_forbidden(monkeypatch):
    """Proves unauthorized / mismatched contractor access is rejected before any PUT persistence."""
    monkeypatch.setattr(
        settings_api,
        "get_firestore_client",
        lambda: pytest.fail("Firestore must not be accessed before authorization"),
    )
    req = _auth_request("contractor-1")
    body = SettingsUpdate(country_code="CA")
    with pytest.raises(HTTPException) as exc_info:
        await api_update_settings(req, body, contractor_id="other-contractor")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_put_settings_root_persistence_failure_returns_error(monkeypatch):
    """Proves failure during root contractor update aborts cleanly and returns error message."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    root_doc.raise_on_update = True

    pref_doc = root_doc.collection("settings").document("preferences")

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    req = _auth_request("c1")
    body = SettingsUpdate(country_code="CA")
    res = await api_update_settings(req, body, contractor_id="c1")

    assert res == {"error": "Failed to save country_code"}
    assert pref_doc.set_data == {}


@pytest.mark.asyncio
async def test_put_settings_batch_failure_leaves_root_and_preferences_unchanged(
    monkeypatch,
):
    """Proves a mixed-write failure cannot partially persist country_code."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    root_doc.data = {"country_code": "US"}
    root_doc.exists = True

    pref_doc = root_doc.collection("settings").document("preferences")
    pref_doc.data = {"greeting_name": "Before"}
    pref_doc.exists = True
    pref_doc.raise_on_set = True

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    req = _auth_request("c1")
    body = SettingsUpdate(country_code="CA", greeting_name="After")
    res = await api_update_settings(req, body, contractor_id="c1")

    assert res == {"error": "Failed to save settings"}
    assert root_doc.data == {"country_code": "US"}
    assert root_doc.updated == {}
    assert pref_doc.data == {"greeting_name": "Before"}
    assert pref_doc.set_data == {}
    assert fake_db.batch_commit_count == 0


@pytest.mark.asyncio
async def test_put_settings_preferences_persistence_failure_returns_error(monkeypatch):
    """Proves failure during preferences subdocument set returns error message."""
    fake_db = _FakeFirestoreDB()
    root_doc = fake_db.collection("contractors").document("c1")
    pref_doc = root_doc.collection("settings").document("preferences")
    pref_doc.raise_on_set = True

    monkeypatch.setattr(settings_api, "get_firestore_client", lambda: fake_db)

    req = _auth_request("c1")
    body = SettingsUpdate(greeting_name="Should Fail")
    res = await api_update_settings(req, body, contractor_id="c1")

    assert res == {"error": "Failed to save settings"}
