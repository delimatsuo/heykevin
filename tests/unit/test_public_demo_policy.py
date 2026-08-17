"""Focused policy and security tests for the public call demo core."""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services.public_demo import (
    PUBLIC_DEMO_DISCLOSURE,
    PUBLIC_DEMO_LEASE_COLLECTION,
    PUBLIC_DEMO_LEASE_DOCUMENT,
    PUBLIC_DEMO_STREAM_CLAIM_COLLECTION,
    acquire_public_demo_lease,
    apply_public_demo_lease_acquire,
    apply_public_demo_lease_release,
    build_public_demo_profile,
    build_public_demo_system_prompt,
    claim_public_demo_stream,
    claim_public_demo_usage_trigger,
    complete_public_demo_usage_trigger,
    execute_public_demo_tool,
    hash_public_demo_identifier,
    is_public_demo_profile,
    prune_public_demo_leases,
    public_demo_available_slots,
    release_public_demo_lease,
    sign_public_demo_stream_token,
    verify_public_demo_stream_token,
)

SECRET = "demo-test-secret-that-is-at-least-thirty-two-characters"
NEW_YORK = ZoneInfo("America/New_York")
FIXED_NOW = datetime(2026, 8, 11, 12, 30, tzinfo=NEW_YORK)


def test_profile_is_fresh_fictional_business_prompt_input_without_destinations():
    first = build_public_demo_profile()
    second = build_public_demo_profile()

    assert first is not second
    assert first["services"] is not second["services"]
    assert first["faqs"] is not second["faqs"]
    assert first["public_demo_policy"] is not second["public_demo_policy"]
    first["services"][0]["name"] = "mutated"
    first["faqs"][0]["answer"] = "mutated"
    first["public_demo_policy"]["external_writes"] = True
    assert second["services"][0]["name"] == "Diagnostic visit"
    assert second["faqs"][0]["answer"].startswith("No. This is a fictional demo")
    assert second["public_demo_policy"]["external_writes"] is False

    assert second["public_demo"] is True
    assert is_public_demo_profile(second)
    assert second["effective_mode"] == "business"
    assert second["timezone"] == "America/New_York"
    assert len(second["services"]) >= 8
    assert second["service_area_zips"] == []
    json.dumps(second)

    forbidden_keys = {
        "api_token",
        "owner_phone",
        "personal_phone",
        "phone",
        "twilio_number",
        "twilio_phone_number",
        "device_token",
        "google_calendar_access_token",
        "google_calendar_refresh_token",
        "jobber_access_token",
        "jobber_refresh_token",
    }
    assert forbidden_keys.isdisjoint(second)


def test_plain_tenant_dict_cannot_forge_the_code_owned_demo_profile():
    genuine = build_public_demo_profile()
    forged = dict(genuine)

    assert is_public_demo_profile(genuine)
    assert not is_public_demo_profile(forged)

    with pytest.raises(ValueError, match="code-owned profile"):
        build_public_demo_system_prompt(forged)


def test_profile_has_explicit_no_real_world_and_no_pii_rules_and_builds_prompt():
    profile = build_public_demo_profile()
    knowledge = profile["knowledge"].lower()

    assert "fictional public demo" in knowledge
    assert "never ask for or repeat" in knowledge
    assert "phone number" in knowledge
    assert "never accepts or collects payment" in knowledge
    assert "never provides a real service" in knowledge
    assert "never say a technician" in knowledge
    assert "always returns booked=false" in knowledge
    assert "boston and the nearby communities" in knowledge
    assert "place names are real" in knowledge
    assert "claimed service territory is fictional" in knowledge
    assert "daily from 8:00 am to 6:00 pm eastern" in knowledge
    assert "licensed and insured?" in knowledge
    assert "no real appointments" in PUBLIC_DEMO_DISCLOSURE.lower()

    prompt = build_public_demo_system_prompt(profile)
    assert profile["business_name"] in prompt
    assert "Hey Kevin Boston Plumbing Demo" in prompt
    assert "Faucet repair labor: $165-$325" in prompt
    assert "RESIDENTIAL SERVICE SCOPE" in prompt
    assert "Answer area questions with a direct yes or no" in prompt
    assert "fictional residential plumbing demonstration" not in prompt
    assert "The opening disclosure happens once" in prompt
    assert 'Never preface an ordinary answer with "as part of our demo,"' in prompt
    assert 'Caller: "Do you do toilet replacement?"' in prompt
    assert "replacement labor runs $425 to $850" in prompt
    assert "Is yours an existing" in prompt
    assert "floor-mounted toilet?" in prompt
    assert 'ask "Is there anything else I can help with?"' in prompt
    assert "preferred_date" in prompt
    assert "YYYY-MM-DD" in prompt


def test_synthetic_availability_is_deterministic_and_eastern_relative_to_tomorrow():
    expected = [
        ("2026-08-12T09:00:00-04:00", "2026-08-12T10:00:00-04:00"),
        ("2026-08-12T13:00:00-04:00", "2026-08-12T14:00:00-04:00"),
        ("2026-08-13T10:00:00-04:00", "2026-08-13T11:00:00-04:00"),
    ]

    first = public_demo_available_slots(now=FIXED_NOW)
    second = public_demo_available_slots(now=FIXED_NOW)

    assert first == second
    assert [(slot["start_iso"], slot["end_iso"]) for slot in first] == expected
    assert all(slot["simulated"] is True for slot in first)
    assert all(slot["booked"] is False for slot in first)
    assert all(slot["timezone"] == "America/New_York" for slot in first)
    json.dumps(first)


def test_synthetic_availability_normalizes_utc_and_observes_dst():
    # 02:00 UTC is still the prior calendar day in New York.
    utc_now = datetime(2027, 1, 10, 2, 0, tzinfo=UTC)
    slots = public_demo_available_slots(now=utc_now, days_ahead=1)

    assert len(slots) == 2
    assert slots[0]["start_iso"] == "2027-01-10T09:00:00-05:00"
    assert slots[1]["start_iso"] == "2027-01-10T13:00:00-05:00"


def test_synthetic_availability_honors_preferred_date_without_changing_default():
    later = public_demo_available_slots(
        now=FIXED_NOW,
        preferred_date="2026-08-18",
    )
    default = public_demo_available_slots(now=FIXED_NOW)

    assert [(slot["start_iso"], slot["end_iso"]) for slot in later] == [
        ("2026-08-18T09:00:00-04:00", "2026-08-18T10:00:00-04:00"),
        ("2026-08-18T13:00:00-04:00", "2026-08-18T14:00:00-04:00"),
        ("2026-08-19T10:00:00-04:00", "2026-08-19T11:00:00-04:00"),
    ]
    assert [(slot["start_iso"], slot["end_iso"]) for slot in default] == [
        ("2026-08-12T09:00:00-04:00", "2026-08-12T10:00:00-04:00"),
        ("2026-08-12T13:00:00-04:00", "2026-08-12T14:00:00-04:00"),
        ("2026-08-13T10:00:00-04:00", "2026-08-13T11:00:00-04:00"),
    ]


def test_check_availability_tool_passes_preferred_date_and_booking_can_echo_it():
    availability = execute_public_demo_tool(
        "check_availability",
        {"preferred_date": "2026-08-18", "days_ahead": 7},
        now=FIXED_NOW,
    )
    selected = availability["available_slots"][0]
    booking = execute_public_demo_tool(
        "book_appointment",
        {
            "title": "demo",
            "start_time": selected["start_iso"],
            "end_time": selected["end_iso"],
        },
        now=FIXED_NOW,
    )

    assert availability["preferred_date"] == "2026-08-18"
    assert selected["start_iso"] == "2026-08-18T09:00:00-04:00"
    assert booking["requested_start"] == selected["start_iso"]
    assert booking["booked"] is False


def test_preferred_date_in_the_past_or_unparseable_falls_back_to_tomorrow():
    past = public_demo_available_slots(now=FIXED_NOW, preferred_date="2026-08-01")
    junk = public_demo_available_slots(now=FIXED_NOW, preferred_date="next Thursday")
    default = public_demo_available_slots(now=FIXED_NOW)

    assert past == default
    assert junk == default


def test_demo_tools_are_json_serializable_simulated_and_never_book(monkeypatch):
    def fail_if_firestore_is_touched():
        raise AssertionError("pure demo tool touched Firestore")

    monkeypatch.setattr(
        "app.db.firestore_client.get_firestore_client",
        fail_if_firestore_is_touched,
    )
    availability = execute_public_demo_tool(
        "check_availability",
        {"days_ahead": 7},
        now=FIXED_NOW,
    )
    selected = availability["available_slots"][0]
    private_title = "Jane Realname at 123 Real Street +1 212 555 0101"
    booking = execute_public_demo_tool(
        "book_appointment",
        {
            "title": private_title,
            "description": "card 4111111111111111",
            "start_time": selected["start_iso"],
            "end_time": selected["end_iso"],
        },
        now=FIXED_NOW,
    )

    assert availability["success"] is True
    assert availability["simulated"] is True
    assert availability["booked"] is False
    assert booking["success"] is False
    assert booking["simulated"] is True
    assert booking["booked"] is False
    assert booking["confirmed"] is False
    assert booking["status"] == "simulated_only_not_booked"
    assert booking["requested_start"] == selected["start_iso"]
    rendered = json.dumps(booking)
    assert private_title not in rendered
    assert "4111111111111111" not in rendered

    unsupported = execute_public_demo_tool("send_payment", {"card": "private"}, now=FIXED_NOW)
    assert unsupported["success"] is False
    assert unsupported["booked"] is False
    assert "private" not in json.dumps(unsupported)
    json.dumps(availability)
    json.dumps(unsupported)


def test_booking_tool_does_not_echo_arbitrary_or_unlisted_times():
    result = execute_public_demo_tool(
        "book_appointment",
        {
            "start_time": "Casey at a private address",
            "end_time": "private@example.com",
        },
        now=FIXED_NOW,
    )

    assert result["request_valid"] is False
    assert result["booked"] is False
    rendered = json.dumps(result)
    assert "Casey" not in rendered
    assert "private@example.com" not in rendered


def test_identifier_hmac_is_deterministic_domain_separated_and_nonrevealing():
    raw_value = "CA1234567890-private"
    first = hash_public_demo_identifier(SECRET, "call", raw_value)

    assert first == hash_public_demo_identifier(SECRET, "call", raw_value)
    assert first != hash_public_demo_identifier(SECRET, "caller", raw_value)
    assert first != hash_public_demo_identifier(SECRET + "x", "call", raw_value)
    assert len(first) == 64
    assert raw_value not in first
    int(first, 16)


def _decode_token_payload(token: str) -> dict[str, Any]:
    payload_segment = token.split(".", 1)[0]
    padded = payload_segment + "=" * (-len(payload_segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


def test_stream_token_is_short_lived_nonrevealing_and_bound_to_exact_identifiers():
    call_sid = "CA-stream-private-123"
    to_number = "+12025550199"
    token = sign_public_demo_stream_token(
        SECRET,
        call_sid,
        to_number,
        now=1000,
        ttl_seconds=30,
    )
    payload = _decode_token_payload(token)

    assert call_sid not in token
    assert to_number not in token
    assert call_sid not in json.dumps(payload)
    assert to_number not in json.dumps(payload)
    assert payload["iat"] == 1000
    assert payload["exp"] == 1030
    assert verify_public_demo_stream_token(token, SECRET, call_sid, to_number, now=1000)
    assert verify_public_demo_stream_token(token, SECRET, call_sid, to_number, now=1029)
    assert not verify_public_demo_stream_token(token, SECRET, call_sid, to_number, now=1030)
    assert not verify_public_demo_stream_token(token, SECRET, call_sid + "x", to_number, now=1000)
    assert not verify_public_demo_stream_token(token, SECRET, call_sid, to_number + " ", now=1000)
    assert not verify_public_demo_stream_token(token, SECRET + "x", call_sid, to_number, now=1000)


def test_stream_token_tampering_and_malformed_inputs_fail_closed():
    token = sign_public_demo_stream_token(SECRET, "CA123", "+12025550199", now=1000)
    payload_segment, signature_segment = token.split(".")
    tampered_payload = ("A" if payload_segment[0] != "A" else "B") + payload_segment[1:]
    tampered_signature = ("A" if signature_segment[0] != "A" else "B") + signature_segment[1:]

    assert not verify_public_demo_stream_token(
        f"{tampered_payload}.{signature_segment}",
        SECRET,
        "CA123",
        "+12025550199",
        now=1000,
    )
    assert not verify_public_demo_stream_token(
        f"{payload_segment}.{tampered_signature}",
        SECRET,
        "CA123",
        "+12025550199",
        now=1000,
    )

    for malformed in ("", ".", "one", "one.two.three", "!.abc", "abc.!", None):
        assert not verify_public_demo_stream_token(
            malformed,  # type: ignore[arg-type]
            SECRET,
            "CA123",
            "+12025550199",
            now=1000,
        )

    with pytest.raises(ValueError):
        sign_public_demo_stream_token(SECRET, "CA123", "+12025550199", ttl_seconds=0)
    with pytest.raises(ValueError):
        sign_public_demo_stream_token(SECRET, "CA123", "+12025550199", ttl_seconds=361)


def test_pure_lease_transitions_prune_enforce_limit_renew_and_release():
    lease_a = "a" * 64
    lease_b = "b" * 64
    lease_c = "c" * 64
    lease_d = "d" * 64
    persisted = {lease_a: 99.0, lease_b: 110.0}

    assert prune_public_demo_leases(persisted, now=100.0) == {lease_b: 110.0}
    allowed, active = apply_public_demo_lease_acquire(
        persisted,
        lease_c,
        limit=2,
        ttl_seconds=20,
        now=100.0,
    )
    assert allowed is True
    assert active == {lease_b: 110.0, lease_c: 120.0}

    allowed, still_active = apply_public_demo_lease_acquire(
        active,
        lease_d,
        limit=2,
        ttl_seconds=20,
        now=101.0,
    )
    assert allowed is False
    assert still_active == active

    renewed, renewed_active = apply_public_demo_lease_acquire(
        active,
        lease_b,
        limit=2,
        ttl_seconds=20,
        now=102.0,
    )
    assert renewed is True
    assert renewed_active[lease_b] == 122.0

    released, after_release = apply_public_demo_lease_release(
        renewed_active,
        lease_b,
        now=103.0,
    )
    assert released is True
    assert after_release == {lease_c: 120.0}


@pytest.mark.parametrize(
    "bad_leases",
    [
        [],
        {"raw-call-sid": 200.0},
        {"a" * 64: "tomorrow"},
        {"a" * 64: float("nan")},
    ],
)
def test_malformed_persisted_lease_state_is_rejected(bad_leases):
    with pytest.raises((TypeError, ValueError)):
        prune_public_demo_leases(bad_leases, now=100.0)


class _FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None):
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _FakeDocRef:
    def __init__(self, db: _FakeFirestore, path: str):
        self._db = db
        self.path = path

    def get(self, transaction=None):
        del transaction
        return _FakeSnapshot(self._db.docs.get(self.path))

    def set(self, value: dict[str, Any], merge: bool = False):
        if merge and self.path in self._db.docs:
            updated = dict(self._db.docs[self.path])
            updated.update(value)
            self._db.docs[self.path] = updated
        else:
            self._db.docs[self.path] = dict(value)


class _FakeCollection:
    def __init__(self, db: _FakeFirestore, name: str):
        self._db = db
        self._name = name

    def document(self, document_id: str):
        return _FakeDocRef(self._db, f"{self._name}/{document_id}")


class _FakeTransaction:
    def set(self, doc_ref: _FakeDocRef, value: dict[str, Any], merge: bool = False):
        doc_ref.set(value, merge=merge)


class _FakeFirestore:
    def __init__(self):
        self.docs: dict[str, dict[str, Any]] = {}

    def collection(self, name: str):
        return _FakeCollection(self, name)

    def transaction(self):
        return _FakeTransaction()


def _install_fake_firestore(monkeypatch) -> _FakeFirestore:
    fake = _FakeFirestore()
    monkeypatch.setattr("app.db.firestore_client.get_firestore_client", lambda: fake)
    from google.cloud import firestore

    monkeypatch.setattr(firestore, "transactional", lambda function: function)
    return fake


@pytest.mark.asyncio
async def test_firestore_lease_is_hmac_keyed_pruned_limited_and_releasable(monkeypatch):
    fake = _install_fake_firestore(monkeypatch)
    raw_a = "CA-raw-private-A"
    raw_b = "CA-raw-private-B"
    raw_c = "CA-raw-private-C"

    assert await acquire_public_demo_lease(raw_a, SECRET, limit=2, ttl_seconds=10, now=100)
    assert await acquire_public_demo_lease(raw_b, SECRET, limit=2, ttl_seconds=10, now=100)
    assert not await acquire_public_demo_lease(raw_c, SECRET, limit=2, ttl_seconds=10, now=100)

    path = f"{PUBLIC_DEMO_LEASE_COLLECTION}/{PUBLIC_DEMO_LEASE_DOCUMENT}"
    persisted_text = json.dumps(fake.docs, default=str)
    assert path in fake.docs
    assert raw_a not in persisted_text
    assert raw_b not in persisted_text
    assert raw_c not in persisted_text
    assert len(fake.docs[path]["leases"]) == 2
    assert all(len(lease_id) == 64 for lease_id in fake.docs[path]["leases"])
    assert fake.docs[path]["expires_at"] == datetime.fromtimestamp(110, tz=UTC)

    assert await release_public_demo_lease(raw_a, SECRET, now=101)
    assert not await release_public_demo_lease(raw_a, SECRET, now=101)
    assert await acquire_public_demo_lease(raw_c, SECRET, limit=2, ttl_seconds=10, now=101)

    # At t=112 both prior leases are stale; the transaction prunes before admitting.
    assert await acquire_public_demo_lease(raw_a, SECRET, limit=1, ttl_seconds=10, now=112)
    assert len(fake.docs[path]["leases"]) == 1
    assert fake.docs[path]["expires_at"] == datetime.fromtimestamp(122, tz=UTC)


@pytest.mark.asyncio
async def test_stream_claim_is_one_time_hmac_keyed_and_ttl_bounded(monkeypatch):
    fake = _install_fake_firestore(monkeypatch)
    raw_sid = "CA-private-one-time-stream"

    assert await claim_public_demo_stream(raw_sid, SECRET, ttl_seconds=30, now=100)
    assert not await claim_public_demo_stream(raw_sid, SECRET, ttl_seconds=30, now=101)

    assert len(fake.docs) == 1
    path, persisted = next(iter(fake.docs.items()))
    assert path.startswith(f"{PUBLIC_DEMO_STREAM_CLAIM_COLLECTION}/")
    assert raw_sid not in json.dumps(fake.docs, default=str)
    assert persisted["expires_epoch"] == 130
    assert persisted["expires_at"] == datetime.fromtimestamp(130, tz=UTC)

    # Firestore TTL deletion is asynchronous. Even if the expired document has not
    # been deleted yet, the atomic transition permits a newly authorized claim.
    assert await claim_public_demo_stream(raw_sid, SECRET, ttl_seconds=30, now=131)
    assert fake.docs[path]["expires_epoch"] == 161


@pytest.mark.asyncio
async def test_usage_trigger_claim_is_hmac_keyed_pending_then_completed(monkeypatch):
    fake = _install_fake_firestore(monkeypatch)
    raw_token = "ACtest-FIRES-UTtest-2026-08-11"

    assert await claim_public_demo_usage_trigger(raw_token, SECRET, now=100) == "new"
    assert await claim_public_demo_usage_trigger(raw_token, SECRET, now=101) == "pending"
    assert await complete_public_demo_usage_trigger(raw_token, SECRET)
    assert await claim_public_demo_usage_trigger(raw_token, SECRET, now=102) == "completed"

    path, persisted = next(iter(fake.docs.items()))
    assert path.startswith(f"{PUBLIC_DEMO_STREAM_CLAIM_COLLECTION}/")
    assert raw_token not in json.dumps(fake.docs, default=str)
    assert persisted["status"] == "completed"
    assert persisted["expires_at"] == datetime.fromtimestamp(172_900, tz=UTC)


@pytest.mark.asyncio
async def test_demo_rate_limit_document_has_explicit_ttl_and_no_raw_identifier(monkeypatch):
    from app.db import rate_limits

    fake = _install_fake_firestore(monkeypatch)
    raw_caller = "+12025550147"
    caller_key = hash_public_demo_identifier(SECRET, "caller", raw_caller)

    result = await rate_limits.check_and_increment(
        scope="public_demo_per_caller",
        key=caller_key,
        limit=3,
        window_seconds=3600,
        document_ttl_seconds=3600,
        now=100,
    )

    assert result.allowed is True
    path, persisted = next(iter(fake.docs.items()))
    assert path.startswith("rate_limits/public_demo_per_caller__")
    assert raw_caller not in json.dumps(fake.docs, default=str)
    assert persisted["expires_at"] == datetime.fromtimestamp(3700, tz=UTC)


@pytest.mark.asyncio
async def test_rate_limit_rejects_ttl_shorter_than_window_before_firestore(monkeypatch):
    from app.db import rate_limits

    def unexpected_firestore():
        raise AssertionError("invalid TTL must fail before constructing Firestore")

    monkeypatch.setattr(
        "app.db.firestore_client.get_firestore_client",
        unexpected_firestore,
    )
    result = await rate_limits.check_and_increment(
        scope="public_demo_per_caller",
        key="a" * 64,
        limit=3,
        window_seconds=3600,
        document_ttl_seconds=3599,
        now=100,
    )
    assert result.allowed is False


@pytest.mark.asyncio
async def test_firestore_lease_fails_closed_on_backend_and_malformed_state(monkeypatch, caplog):
    private_sid = "CA-do-not-log-or-store"

    def broken_client():
        raise RuntimeError("backend unavailable with private provider detail")

    monkeypatch.setattr("app.db.firestore_client.get_firestore_client", broken_client)
    assert not await acquire_public_demo_lease(
        private_sid,
        SECRET,
        limit=2,
        ttl_seconds=10,
        now=100,
    )
    assert private_sid not in caplog.text
    assert "private provider detail" not in caplog.text

    fake = _install_fake_firestore(monkeypatch)
    path = f"{PUBLIC_DEMO_LEASE_COLLECTION}/{PUBLIC_DEMO_LEASE_DOCUMENT}"
    fake.docs[path] = {"leases": {"raw-call-sid": 200.0}}
    assert not await acquire_public_demo_lease(
        private_sid,
        SECRET,
        limit=2,
        ttl_seconds=10,
        now=100,
    )
    assert fake.docs[path] == {"leases": {"raw-call-sid": 200.0}}


@pytest.mark.asyncio
async def test_invalid_lease_parameters_fail_before_firestore(monkeypatch):
    def fail_if_firestore_is_touched():
        raise AssertionError("invalid admission constructed Firestore")

    monkeypatch.setattr(
        "app.db.firestore_client.get_firestore_client",
        fail_if_firestore_is_touched,
    )
    assert not await acquire_public_demo_lease("", SECRET, limit=2, ttl_seconds=10, now=100)
    assert not await acquire_public_demo_lease("CA1", "", limit=2, ttl_seconds=10, now=100)
    assert not await acquire_public_demo_lease("CA1", SECRET, limit=0, ttl_seconds=10, now=100)
    assert not await acquire_public_demo_lease("CA1", SECRET, limit=2, ttl_seconds=0, now=100)
