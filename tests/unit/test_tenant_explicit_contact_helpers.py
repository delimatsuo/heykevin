"""Tests for tenant-explicit contact and history helpers."""

from __future__ import annotations

import asyncio
import inspect
import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import pytest

from app.services import adaptive_trust, lookup as lookup_service


class HostileStr(str):
    """Subclass of str used to verify exact built-in str type enforcement."""
    pass


INVALID_CONTRACTOR_IDS = [
    "",
    "   ",
    "\t\n ",
    None,
    123,
    0,
    False,
    object(),
    HostileStr("tenant-123"),
]


# ============================================================================
# A. Signature Gates
# ============================================================================

def test_signature_gates_keyword_only_no_default():
    """Verify contractor_id is a required keyword-only parameter with no default."""
    for fn in (
        adaptive_trust.adjust_trust_after_call,
        lookup_service._lookup_contact,
        lookup_service._lookup_history,
        lookup_service.run_lookups,
    ):
        params = inspect.signature(fn).parameters
        assert "contractor_id" in params, f"{fn.__name__} missing contractor_id parameter"
        param = params["contractor_id"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{fn.__name__} contractor_id must be KEYWORD_ONLY"
        )
        assert param.default is inspect.Parameter.empty, (
            f"{fn.__name__} contractor_id must have no default"
        )


@pytest.mark.asyncio
async def test_calling_without_contractor_id_fails_before_dependency_calls(monkeypatch):
    """Calling helpers without contractor_id fails before executing any dependency."""
    call_counts = {"db": 0, "child_lookup": 0}

    async def _fail_db(*args, **kwargs):
        call_counts["db"] += 1
        raise AssertionError("DB helper should not have been called")

    async def _fail_lookup(*args, **kwargs):
        call_counts["child_lookup"] += 1
        raise AssertionError("Child lookup should not have been called")

    monkeypatch.setattr(adaptive_trust, "get_contact", _fail_db)
    monkeypatch.setattr(adaptive_trust, "upsert_contact", _fail_db)
    monkeypatch.setattr(lookup_service, "get_contact", _fail_db)
    monkeypatch.setattr(lookup_service, "get_call_history", _fail_db)
    monkeypatch.setattr(lookup_service, "_lookup_twilio", _fail_lookup)
    monkeypatch.setattr(lookup_service, "_lookup_nomorobo", _fail_lookup)

    with pytest.raises(TypeError):
        await adaptive_trust.adjust_trust_after_call("+15551234567", "picked_up")

    with pytest.raises(TypeError):
        await lookup_service._lookup_contact("+15551234567")

    with pytest.raises(TypeError):
        await lookup_service._lookup_history("+15551234567")

    with pytest.raises(TypeError):
        await lookup_service.run_lookups("+15551234567")

    assert call_counts["db"] == 0
    assert call_counts["child_lookup"] == 0


# ============================================================================
# B. Invalid IDs Cause Zero Effects
# ============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_id", INVALID_CONTRACTOR_IDS)
@pytest.mark.parametrize("outcome", ["picked_up", "ignored", "voicemail", "blocked", "unknown_outcome"])
async def test_adjust_trust_after_call_invalid_id_raises_and_causes_no_effects(
    monkeypatch, invalid_id, outcome
):
    """Invalid contractor_id raises ValueError and causes zero DB calls across all outcomes (including delta 0)."""
    calls = []

    async def _db_trap(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("DB should never be touched on invalid contractor_id")

    monkeypatch.setattr(adaptive_trust, "get_contact", _db_trap)
    monkeypatch.setattr(adaptive_trust, "upsert_contact", _db_trap)

    with pytest.raises(ValueError, match="^contractor_id is required$"):
        await adaptive_trust.adjust_trust_after_call(
            "+15551234567", outcome, contractor_id=invalid_id
        )

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_id", INVALID_CONTRACTOR_IDS)
async def test_run_lookups_invalid_id_raises_and_schedules_no_children(monkeypatch, invalid_id):
    """Invalid contractor_id raises ValueError before scheduling any coroutine or child lookup."""
    child_calls = []

    def _trap_child(name):
        async def _inner(*args, **kwargs):
            child_calls.append((name, args, kwargs))
            raise AssertionError(f"{name} should not be called")
        return _inner

    monkeypatch.setattr(lookup_service, "_lookup_twilio", _trap_child("twilio"))
    monkeypatch.setattr(lookup_service, "_lookup_nomorobo", _trap_child("nomorobo"))
    monkeypatch.setattr(lookup_service, "_lookup_contact", _trap_child("contact"))
    monkeypatch.setattr(lookup_service, "_lookup_history", _trap_child("history"))
    monkeypatch.setattr(lookup_service, "get_contact", _trap_child("get_contact"))
    monkeypatch.setattr(lookup_service, "get_call_history", _trap_child("get_call_history"))

    with pytest.raises(ValueError, match="^contractor_id is required$"):
        await lookup_service.run_lookups("+15551234567", contractor_id=invalid_id)

    assert child_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_id", INVALID_CONTRACTOR_IDS)
async def test_direct_lookup_helpers_invalid_id_raises_and_zero_db(monkeypatch, invalid_id):
    """Direct _lookup_contact and _lookup_history calls fail closed on invalid contractor_id."""
    db_calls = []

    async def _db_trap(*args, **kwargs):
        db_calls.append((args, kwargs))
        raise AssertionError("DB should never be touched on invalid contractor_id")

    monkeypatch.setattr(lookup_service, "get_contact", _db_trap)
    monkeypatch.setattr(lookup_service, "get_call_history", _db_trap)

    with pytest.raises(ValueError, match="^contractor_id is required$"):
        await lookup_service._lookup_contact("+15551234567", contractor_id=invalid_id)

    with pytest.raises(ValueError, match="^contractor_id is required$"):
        await lookup_service._lookup_history("+15551234567", contractor_id=invalid_id)

    assert db_calls == []


# ============================================================================
# C. Exact Forwarding and Tenant Isolation
# ============================================================================

@pytest.mark.asyncio
async def test_adjust_trust_existing_contact_forwarding_and_tenant_isolation(monkeypatch):
    """Updating existing contact forwards exact contractor_id and isolates tenants."""
    contacts_store: dict[str, dict[str, dict]] = {
        "tenant-a": {
            "+15551112222": {"phone": "+15551112222", "trust_level": 50, "times_picked_up": 0}
        },
        "tenant-b": {
            "+15551112222": {"phone": "+15551112222", "trust_level": 80, "times_ignored": 0}
        },
    }
    history_calls = []

    async def mock_get_contact(phone: str, contractor_id: str = ""):
        history_calls.append(("get_contact", phone, contractor_id))
        return contacts_store.get(contractor_id, {}).get(phone)

    async def mock_upsert_contact(phone: str, data: dict, contractor_id: str = ""):
        history_calls.append(("upsert_contact", phone, dict(data), contractor_id))
        if contractor_id in contacts_store:
            existing = contacts_store[contractor_id].setdefault(phone, {})
            existing.update(data)

    monkeypatch.setattr(adaptive_trust, "get_contact", mock_get_contact)
    monkeypatch.setattr(adaptive_trust, "upsert_contact", mock_upsert_contact)

    # Adjust Tenant A: picked_up (+5)
    await adaptive_trust.adjust_trust_after_call(
        "+15551112222", "picked_up", contractor_id="tenant-a"
    )

    assert contacts_store["tenant-a"]["+15551112222"]["trust_level"] == 55
    assert contacts_store["tenant-a"]["+15551112222"]["times_picked_up"] == 1
    # Tenant B must remain unmodified
    assert contacts_store["tenant-b"]["+15551112222"]["trust_level"] == 80
    assert contacts_store["tenant-b"]["+15551112222"]["times_ignored"] == 0

    # Adjust Tenant B: ignored (-3)
    await adaptive_trust.adjust_trust_after_call(
        "+15551112222", "ignored", contractor_id="tenant-b"
    )

    assert contacts_store["tenant-b"]["+15551112222"]["trust_level"] == 77
    assert contacts_store["tenant-b"]["+15551112222"]["times_ignored"] == 1
    # Tenant A must remain 55
    assert contacts_store["tenant-a"]["+15551112222"]["trust_level"] == 55

    # Verify exact forwarding in recorded calls
    assert history_calls[0] == ("get_contact", "+15551112222", "tenant-a")
    assert history_calls[1] == (
        "upsert_contact",
        "+15551112222",
        {"trust_level": 55, "times_picked_up": 1},
        "tenant-a",
    )
    assert history_calls[2] == ("get_contact", "+15551112222", "tenant-b")
    assert history_calls[3] == (
        "upsert_contact",
        "+15551112222",
        {"trust_level": 77, "times_ignored": 1},
        "tenant-b",
    )


@pytest.mark.asyncio
async def test_adjust_trust_new_contact_forwarding_and_tenant_isolation(monkeypatch):
    """Creating new contact forwards exact contractor_id and does not mutate other tenants."""
    contacts_store: dict[str, dict[str, dict]] = {
        "tenant-a": {},
        "tenant-b": {},
    }
    history_calls = []

    async def mock_get_contact(phone: str, contractor_id: str = ""):
        history_calls.append(("get_contact", phone, contractor_id))
        return contacts_store.get(contractor_id, {}).get(phone)

    async def mock_upsert_contact(phone: str, data: dict, contractor_id: str = ""):
        history_calls.append(("upsert_contact", phone, dict(data), contractor_id))
        if contractor_id in contacts_store:
            contacts_store[contractor_id][phone] = dict(data)

    monkeypatch.setattr(adaptive_trust, "get_contact", mock_get_contact)
    monkeypatch.setattr(adaptive_trust, "upsert_contact", mock_upsert_contact)

    # Create new contact in Tenant A: blocked (-5 -> base 50 - 5 = 45)
    await adaptive_trust.adjust_trust_after_call(
        "+15559998888", "blocked", contractor_id="tenant-a"
    )

    assert "+15559998888" in contacts_store["tenant-a"]
    assert contacts_store["tenant-a"]["+15559998888"]["trust_level"] == 45
    assert contacts_store["tenant-a"]["+15559998888"]["name"] == ""
    # Tenant B has no contact
    assert "+15559998888" not in contacts_store["tenant-b"]

    assert history_calls[0] == ("get_contact", "+15559998888", "tenant-a")
    assert history_calls[1] == (
        "upsert_contact",
        "+15559998888",
        {"trust_level": 45, "name": ""},
        "tenant-a",
    )


@pytest.mark.asyncio
async def test_direct_lookup_contact_and_history_forwarding(monkeypatch):
    """_lookup_contact and _lookup_history forward exact contractor_id to DB helpers."""
    received_contact = {}
    received_history = {}

    async def mock_get_contact(phone: str, contractor_id: str = ""):
        received_contact["phone"] = phone
        received_contact["contractor_id"] = contractor_id
        return {"name": "Alice", "phone": phone}

    async def mock_get_call_history(phone: str, *, contractor_id: str, limit: int = 10):
        received_history["phone"] = phone
        received_history["contractor_id"] = contractor_id
        received_history["limit"] = limit
        return [{"outcome": "picked_up"}, {"outcome": "ignored"}]

    monkeypatch.setattr(lookup_service, "get_contact", mock_get_contact)
    monkeypatch.setattr(lookup_service, "get_call_history", mock_get_call_history)

    contact_res = await lookup_service._lookup_contact(
        "+15550001111", contractor_id="tenant-target"
    )
    assert contact_res == {"name": "Alice", "phone": "+15550001111"}
    assert received_contact == {"phone": "+15550001111", "contractor_id": "tenant-target"}

    history_res = await lookup_service._lookup_history(
        "+15550002222", contractor_id="tenant-target"
    )
    assert history_res == {
        "total_calls": 2,
        "times_picked_up": 1,
        "times_ignored": 1,
        "times_blocked": 0,
    }
    assert received_history == {
        "phone": "+15550002222",
        "contractor_id": "tenant-target",
        "limit": 20,
    }


@pytest.mark.asyncio
async def test_run_lookups_passes_exact_contractor_id_and_returns_tenant_results(monkeypatch):
    """run_lookups passes contractor_id to child lookups and aggregates tenant-isolated results."""
    contacts_by_tenant = {
        "tenant-a": {"name": "Tenant A Contact", "phone": "+15550003333"},
        "tenant-b": {"name": "Tenant B Contact", "phone": "+15550003333"},
    }
    histories_by_tenant = {
        "tenant-a": [{"outcome": "picked_up"}, {"outcome": "picked_up"}],
        "tenant-b": [{"outcome": "blocked"}],
    }

    async def mock_get_contact(phone: str, contractor_id: str = ""):
        return contacts_by_tenant.get(contractor_id)

    async def mock_get_call_history(phone: str, *, contractor_id: str, limit: int = 10):
        return histories_by_tenant.get(contractor_id, [])

    async def mock_lookup_twilio(phone: str, include_cnam: bool = False):
        return {"carrier": "Verizon", "line_type": "mobile"}

    async def mock_lookup_nomorobo(phone: str, twilio_addon_data=None):
        return {"spam_score": 1}

    monkeypatch.setattr(lookup_service, "get_contact", mock_get_contact)
    monkeypatch.setattr(lookup_service, "get_call_history", mock_get_call_history)
    monkeypatch.setattr(lookup_service, "_lookup_twilio", mock_lookup_twilio)
    monkeypatch.setattr(lookup_service, "_lookup_nomorobo", mock_lookup_nomorobo)

    res_a = await lookup_service.run_lookups("+15550003333", contractor_id="tenant-a")
    assert res_a["twilio"] == {"carrier": "Verizon", "line_type": "mobile"}
    assert res_a["nomorobo"] == {"spam_score": 1}
    assert res_a["contact"] == {"name": "Tenant A Contact", "phone": "+15550003333"}
    assert res_a["history"] == {
        "total_calls": 2,
        "times_picked_up": 2,
        "times_ignored": 0,
        "times_blocked": 0,
    }

    res_b = await lookup_service.run_lookups("+15550003333", contractor_id="tenant-b")
    assert res_b["contact"] == {"name": "Tenant B Contact", "phone": "+15550003333"}
    assert res_b["history"] == {
        "total_calls": 1,
        "times_picked_up": 0,
        "times_ignored": 0,
        "times_blocked": 1,
    }


# ============================================================================
# D. Failure Containment
# ============================================================================

@pytest.mark.asyncio
async def test_run_lookups_failure_containment_contact_error(monkeypatch):
    """When contact lookup raises or times out, run_lookups returns partial results with contact=None."""
    async def mock_lookup_twilio(phone: str, include_cnam: bool = False):
        return {"carrier": "AT&T", "line_type": "landline"}

    async def mock_lookup_nomorobo(phone: str, twilio_addon_data=None):
        return {"spam_score": 0}

    async def mock_get_contact_failing(phone: str, contractor_id: str = ""):
        raise RuntimeError("Contact DB down")

    async def mock_get_call_history(phone: str, *, contractor_id: str, limit: int = 10):
        return [{"outcome": "picked_up"}]

    monkeypatch.setattr(lookup_service, "_lookup_twilio", mock_lookup_twilio)
    monkeypatch.setattr(lookup_service, "_lookup_nomorobo", mock_lookup_nomorobo)
    monkeypatch.setattr(lookup_service, "get_contact", mock_get_contact_failing)
    monkeypatch.setattr(lookup_service, "get_call_history", mock_get_call_history)

    res = await lookup_service.run_lookups("+15550004444", contractor_id="tenant-a")
    assert res["twilio"] == {"carrier": "AT&T", "line_type": "landline"}
    assert res["nomorobo"] == {"spam_score": 0}
    assert res["contact"] is None
    assert res["history"] == {
        "total_calls": 1,
        "times_picked_up": 1,
        "times_ignored": 0,
        "times_blocked": 0,
    }


@pytest.mark.asyncio
async def test_run_lookups_failure_containment_history_error(monkeypatch):
    """When history lookup raises or times out, run_lookups returns partial results with history={}."""
    async def mock_lookup_twilio(phone: str, include_cnam: bool = False):
        return {"carrier": "T-Mobile", "line_type": "mobile"}

    async def mock_lookup_nomorobo(phone: str, twilio_addon_data=None):
        return {"spam_score": 0}

    async def mock_get_contact(phone: str, contractor_id: str = ""):
        return {"name": "Bob", "phone": phone}

    async def mock_get_call_history_failing(phone: str, *, contractor_id: str, limit: int = 10):
        raise RuntimeError("Calls DB down")

    monkeypatch.setattr(lookup_service, "_lookup_twilio", mock_lookup_twilio)
    monkeypatch.setattr(lookup_service, "_lookup_nomorobo", mock_lookup_nomorobo)
    monkeypatch.setattr(lookup_service, "get_contact", mock_get_contact)
    monkeypatch.setattr(lookup_service, "get_call_history", mock_get_call_history_failing)

    res = await lookup_service.run_lookups("+15550005555", contractor_id="tenant-a")
    assert res["twilio"] == {"carrier": "T-Mobile", "line_type": "mobile"}
    assert res["nomorobo"] == {"spam_score": 0}
    assert res["contact"] == {"name": "Bob", "phone": "+15550005555"}
    assert res["history"] == {}


@pytest.mark.asyncio
async def test_direct_lookups_timeout_containment(monkeypatch):
    """Direct _lookup_contact and _lookup_history handle TimeoutError gracefully."""
    async def _hang_forever(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(lookup_service, "LOOKUP_TIMEOUT", 0.01)
    monkeypatch.setattr(lookup_service, "get_contact", _hang_forever)
    monkeypatch.setattr(lookup_service, "get_call_history", _hang_forever)

    contact_res = await lookup_service._lookup_contact("+15550006666", contractor_id="tenant-a")
    assert contact_res is None

    history_res = await lookup_service._lookup_history("+15550006666", contractor_id="tenant-a")
    assert history_res == {}
