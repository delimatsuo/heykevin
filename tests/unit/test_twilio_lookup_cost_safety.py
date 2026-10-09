import asyncio
import pytest

from app.services.routing import Route


class MockTwilioResponse:
    def __init__(self, line_type=None, caller_name=None):
        self.line_type_intelligence = line_type
        self.caller_name = caller_name


class MockTwilioClient:
    instances = []
    fetch_calls = []
    fetch_handler = None

    def __init__(self, account_sid=None, auth_token=None):
        MockTwilioClient.instances.append(self)
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.lookups = self

    @property
    def v2(self):
        return self

    def phone_numbers(self, phone):
        self._phone = phone
        return self

    def fetch(self, fields=None):
        MockTwilioClient.fetch_calls.append({"phone": self._phone, "fields": fields})
        if MockTwilioClient.fetch_handler:
            return MockTwilioClient.fetch_handler(self._phone, fields)
        return MockTwilioResponse(
            line_type={"carrier_name": "Test Wireless", "type": "mobile"},
            caller_name={"caller_name": "Verified Business LLC"},
        )

    @classmethod
    def reset(cls):
        cls.instances.clear()
        cls.fetch_calls.clear()
        cls.fetch_handler = None


@pytest.fixture
def lookup(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "test-account-sid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550000000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-telegram-token")
    monkeypatch.setenv("USER_PHONE", "+15550000001")

    from app.services import lookup as lookup_module

    MockTwilioClient.reset()
    monkeypatch.setattr(lookup_module, "Client", MockTwilioClient)
    return lookup_module


@pytest.fixture
def active_updates(lookup, monkeypatch):
    updates_list = []

    async def capture(call_sid, updates):
        updates_list.append((call_sid, updates))

    monkeypatch.setattr("app.db.cache.update_active_call", capture)
    return updates_list


@pytest.fixture
def twilio_incoming(lookup, active_updates):
    from app.webhooks import twilio_incoming as twilio_incoming_module

    return twilio_incoming_module


# --- 1. Generic lookup default carrier fields and optional CNAM ---

@pytest.mark.asyncio
async def test_lookup_twilio_default_generic_behavior(lookup):
    """Default lookup requests line_type_intelligence and returns carrier/line_type."""
    res = await lookup._lookup_twilio("+15551234567")
    assert len(MockTwilioClient.instances) == 1
    assert len(MockTwilioClient.fetch_calls) == 1
    assert MockTwilioClient.fetch_calls[0]["fields"] == "line_type_intelligence"
    assert res == {"carrier": "Test Wireless", "line_type": "mobile"}
    assert "caller_name" not in res


@pytest.mark.asyncio
async def test_lookup_twilio_generic_with_optional_cnam(lookup):
    """Default plus include_cnam requests both fields and returns carrier/line_type/caller_name."""
    res = await lookup._lookup_twilio("+15551234567", include_cnam=True)
    assert len(MockTwilioClient.instances) == 1
    assert len(MockTwilioClient.fetch_calls) == 1
    assert MockTwilioClient.fetch_calls[0]["fields"] == "line_type_intelligence,caller_name"
    assert res == {
        "carrier": "Test Wireless",
        "line_type": "mobile",
        "caller_name": "Verified Business LLC",
    }


# --- 2. CNAM-only valid numbers ---

@pytest.mark.parametrize(
    "phone",
    [
        "+15551234567",         # US
        "+14165550199",         # CA
        "+442071838750",        # UK
        "+81312345678",         # Japan
        "+5511987654321",       # Brazil
        "+99912345678",         # Syntactically valid new-range
    ],
)
@pytest.mark.asyncio
async def test_lookup_twilio_cnam_only_valid_numbers(lookup, phone):
    """CNAM-only with valid E.164 numbers makes exactly 1 fetch with fields=caller_name and returns caller_name only."""
    res = await lookup._lookup_twilio(phone, include_cnam=True, include_line_type=False)
    assert len(MockTwilioClient.instances) == 1
    assert len(MockTwilioClient.fetch_calls) == 1
    assert MockTwilioClient.fetch_calls[0]["phone"] == phone
    assert MockTwilioClient.fetch_calls[0]["fields"] == "caller_name"
    assert res == {"caller_name": "Verified Business LLC"}
    assert "carrier" not in res
    assert "line_type" not in res


@pytest.mark.asyncio
async def test_lookup_twilio_cnam_only_empty_provider_name_returns_empty_dict(lookup):
    """CNAM-only returns {} if provider returns empty caller_name."""
    MockTwilioClient.fetch_handler = lambda p, f: MockTwilioResponse(
        caller_name={"caller_name": ""}
    )
    res = await lookup._lookup_twilio("+15551234567", include_cnam=True, include_line_type=False)
    assert len(MockTwilioClient.instances) == 1
    assert len(MockTwilioClient.fetch_calls) == 1
    assert res == {}


# --- 3. No-fields mode and noncanonical inputs ---

@pytest.mark.asyncio
async def test_lookup_twilio_no_fields_mode_zero_client(lookup):
    """No-fields mode returns {} without constructing Client or executor."""
    res = await lookup._lookup_twilio("+15551234567", include_cnam=False, include_line_type=False)
    assert res == {}
    assert len(MockTwilioClient.instances) == 0
    assert len(MockTwilioClient.fetch_calls) == 0


@pytest.mark.parametrize(
    "invalid_phone",
    [
        "",
        "anonymous",
        "restricted",
        "unknown",
        "unavailable",
        "client:contractor_c1",
        "15551234567",           # Missing plus
        "+01555123456",          # Country code starts with 0
        "+1",                    # Only 1 digit total
        "+1234567890123456",     # 16 digits (overlong for E.164)
        "+1-555-123-4567",       # Non-digit characters (hyphens)
        "+1 (555) 123-4567",     # Non-digit characters (spaces/parens)
        "+15551234567\n",        # Trailing newline
        "+15551234567\r",        # Carriage return
        " +15551234567",         # Leading whitespace
        "+15551234567 ",         # Trailing whitespace
        "+1555 1234567",         # Embedded whitespace
        "+1555123456\u0660",     # Non-ASCII digits (Arabic-Indic digit)
        "+1555１２３４567",       # Non-ASCII digits (Fullwidth digits)
        None,
        15551234567,
    ],
)
@pytest.mark.asyncio
async def test_lookup_twilio_cnam_only_noncanonical_zero_client(lookup, invalid_phone):
    """CNAM-only mode skips noncanonical phone syntax with zero Client construction and returns {}."""
    res = await lookup._lookup_twilio(invalid_phone, include_cnam=True, include_line_type=False)
    assert res == {}
    assert len(MockTwilioClient.instances) == 0
    assert len(MockTwilioClient.fetch_calls) == 0


# --- 4. CNAM-only provider error or timeout ---

@pytest.mark.asyncio
async def test_lookup_twilio_cnam_only_provider_error_no_retry(lookup):
    """Provider error returns {} and fetches exactly once (no retry)."""
    def error_handler(p, f):
        raise RuntimeError("Twilio API 500 internal error")

    MockTwilioClient.fetch_handler = error_handler
    res = await lookup._lookup_twilio("+15551234567", include_cnam=True, include_line_type=False)
    assert res == {}
    assert len(MockTwilioClient.fetch_calls) == 1


@pytest.mark.asyncio
async def test_lookup_twilio_cnam_only_timeout_no_retry(lookup):
    """Timeout returns {} and executes exactly once without retry."""
    def timeout_handler(p, f):
        raise asyncio.TimeoutError()

    MockTwilioClient.fetch_handler = timeout_handler
    res = await lookup._lookup_twilio("+15551234567", include_cnam=True, include_line_type=False)
    assert res == {}
    assert len(MockTwilioClient.instances) == 1
    assert len(MockTwilioClient.fetch_calls) == 1


# --- 5. _post_routing_tasks in twilio_incoming ---

@pytest.mark.asyncio
async def test_post_routing_tasks_ai_screening_known_caller(lookup, twilio_incoming, monkeypatch, active_updates):
    """Known caller: saves call + active state, sends push, zero lookup calls."""
    saved_calls = []
    saved_active = []
    sent_pushes = []
    lookup_calls = []

    async def fake_save_call(call_sid, record):
        saved_calls.append((call_sid, record))

    async def fake_save_active_call(active_call):
        saved_active.append(active_call)

    async def fake_send_regular_push(**kwargs):
        sent_pushes.append(kwargs)

    async def fake_get_device_token(**kwargs):
        return "push_token_test"

    async def fake_get_contractor(contractor_id):
        return {"contractor_id": contractor_id, "cnam_lookup_enabled": True}

    real_lookup = lookup._lookup_twilio

    async def spy_lookup_twilio(*args, **kwargs):
        lookup_calls.append((args, kwargs))
        return await real_lookup(*args, **kwargs)

    monkeypatch.setattr("app.db.calls.save_call", fake_save_call)
    monkeypatch.setattr("app.db.cache.save_active_call", fake_save_active_call)
    monkeypatch.setattr("app.services.push_notification.send_regular_push", fake_send_regular_push)
    monkeypatch.setattr("app.services.push_notification.get_device_token", fake_get_device_token)
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr(lookup, "_lookup_twilio", spy_lookup_twilio)

    await twilio_incoming._post_routing_tasks(
        call_sid="CA_KNOWN_1",
        caller_phone="+15551234567",
        caller_name="Alice Smith",
        trust_score=85,
        score_breakdown={},
        route=Route.AI_SCREENING,
        lookups={"twilio": {}, "nomorobo": {}},
        conference_name="conf_known_1",
        contractor_id="c1",
        ws_token="ws_tok_1",
        caller_name_trusted=True,
    )

    assert len(saved_calls) == 1
    assert saved_calls[0][1]["caller_name"] == "Alice Smith"
    assert len(saved_active) == 1
    assert saved_active[0].caller_name == "Alice Smith"
    assert saved_active[0].caller_name_trusted is True
    assert len(sent_pushes) == 1
    assert len(lookup_calls) == 0
    assert len(MockTwilioClient.fetch_calls) == 0
    assert active_updates == []


@pytest.mark.asyncio
async def test_post_routing_tasks_ai_screening_unknown_caller_cnam_disabled(lookup, twilio_incoming, monkeypatch, active_updates):
    """Unknown caller with CNAM disabled: saves call + active state, sends push, zero lookup calls."""
    saved_calls = []
    saved_active = []
    sent_pushes = []
    lookup_calls = []

    async def fake_save_call(call_sid, record):
        saved_calls.append((call_sid, record))

    async def fake_save_active_call(active_call):
        saved_active.append(active_call)

    async def fake_send_regular_push(**kwargs):
        sent_pushes.append(kwargs)

    async def fake_get_device_token(**kwargs):
        return "push_token_test"

    async def fake_get_contractor(contractor_id):
        return {"contractor_id": contractor_id, "cnam_lookup_enabled": False}

    real_lookup = lookup._lookup_twilio

    async def spy_lookup_twilio(*args, **kwargs):
        lookup_calls.append((args, kwargs))
        return await real_lookup(*args, **kwargs)

    monkeypatch.setattr("app.db.calls.save_call", fake_save_call)
    monkeypatch.setattr("app.db.cache.save_active_call", fake_save_active_call)
    monkeypatch.setattr("app.services.push_notification.send_regular_push", fake_send_regular_push)
    monkeypatch.setattr("app.services.push_notification.get_device_token", fake_get_device_token)
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr(lookup, "_lookup_twilio", spy_lookup_twilio)

    await twilio_incoming._post_routing_tasks(
        call_sid="CA_UNKNOWN_CNAM_OFF",
        caller_phone="+15551234567",
        caller_name="",
        trust_score=50,
        score_breakdown={},
        route=Route.AI_SCREENING,
        lookups={"twilio": {}, "nomorobo": {}},
        conference_name="conf_unknown_1",
        contractor_id="c1",
        ws_token="ws_tok_2",
        caller_name_trusted=False,
    )

    assert len(saved_calls) == 1
    assert saved_calls[0][1]["caller_name"] == ""
    assert len(saved_active) == 1
    assert saved_active[0].caller_name == ""
    assert saved_active[0].caller_name_trusted is False
    assert len(sent_pushes) == 1
    assert len(lookup_calls) == 0
    assert len(MockTwilioClient.fetch_calls) == 0
    assert active_updates == []


@pytest.mark.asyncio
async def test_post_routing_tasks_ai_screening_unknown_caller_cnam_enabled(lookup, twilio_incoming, monkeypatch):
    """Unknown caller with CNAM enabled: exactly one caller_name-only lookup, persists name, untrusted."""
    saved_calls = []
    saved_active = []
    updated_active = []
    sent_pushes = []
    lookup_calls = []

    async def fake_save_call(call_sid, record):
        saved_calls.append((call_sid, record))

    async def fake_save_active_call(active_call):
        saved_active.append(active_call)

    async def fake_update_active_call(call_sid, updates):
        updated_active.append((call_sid, updates))

    async def fake_send_regular_push(**kwargs):
        sent_pushes.append(kwargs)

    async def fake_get_device_token(**kwargs):
        return "push_token_test"

    async def fake_get_contractor(contractor_id):
        return {"contractor_id": contractor_id, "cnam_lookup_enabled": True}

    real_lookup = lookup._lookup_twilio

    async def spy_lookup_twilio(*args, **kwargs):
        lookup_calls.append((args, kwargs))
        return await real_lookup(*args, **kwargs)

    monkeypatch.setattr("app.db.calls.save_call", fake_save_call)
    monkeypatch.setattr("app.db.cache.save_active_call", fake_save_active_call)
    monkeypatch.setattr("app.db.cache.update_active_call", fake_update_active_call)
    monkeypatch.setattr("app.services.push_notification.send_regular_push", fake_send_regular_push)
    monkeypatch.setattr("app.services.push_notification.get_device_token", fake_get_device_token)
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr(lookup, "_lookup_twilio", spy_lookup_twilio)

    MockTwilioClient.fetch_handler = lambda p, f: MockTwilioResponse(
        caller_name={"caller_name": "Acme Corp"}
    )

    await twilio_incoming._post_routing_tasks(
        call_sid="CA_UNKNOWN_CNAM_ON",
        caller_phone="+15551234567",
        caller_name="",
        trust_score=50,
        score_breakdown={},
        route=Route.AI_SCREENING,
        lookups={"twilio": {}, "nomorobo": {}},
        conference_name="conf_unknown_2",
        contractor_id="c1",
        ws_token="ws_tok_3",
        caller_name_trusted=False,
    )

    # Initial save + CNAM enrichment save
    assert len(saved_calls) == 2
    assert saved_calls[0][1]["caller_name"] == ""
    assert saved_calls[1] == ("CA_UNKNOWN_CNAM_ON", {"caller_name": "Acme Corp"})

    # Active call saved initially, then updated with CNAM caller_name
    assert len(saved_active) == 1
    assert saved_active[0].caller_name == ""
    assert saved_active[0].caller_name_trusted is False
    assert len(updated_active) == 1
    assert updated_active[0] == ("CA_UNKNOWN_CNAM_ON", {"caller_name": "Acme Corp"})

    # Push sent
    assert len(sent_pushes) == 1

    # Exactly one lookup call with include_cnam=True and include_line_type=False
    assert len(lookup_calls) == 1
    assert lookup_calls[0] == (("+15551234567",), {"include_cnam": True, "include_line_type": False})
    assert len(MockTwilioClient.fetch_calls) == 1
    assert MockTwilioClient.fetch_calls[0]["fields"] == "caller_name"


@pytest.mark.asyncio
async def test_post_routing_tasks_ai_screening_withheld_caller(lookup, twilio_incoming, monkeypatch, active_updates):
    """Withheld/anonymous caller: saves call + active state and sends push, no paid Client lookup."""
    saved_calls = []
    saved_active = []
    sent_pushes = []
    lookup_calls = []

    async def fake_save_call(call_sid, record):
        saved_calls.append((call_sid, record))

    async def fake_save_active_call(active_call):
        saved_active.append(active_call)

    async def fake_send_regular_push(**kwargs):
        sent_pushes.append(kwargs)

    async def fake_get_device_token(**kwargs):
        return "push_token_test"

    async def fake_get_contractor(contractor_id):
        return {"contractor_id": contractor_id, "cnam_lookup_enabled": True}

    real_lookup = lookup._lookup_twilio

    async def spy_lookup_twilio(*args, **kwargs):
        lookup_calls.append((args, kwargs))
        return await real_lookup(*args, **kwargs)

    monkeypatch.setattr("app.db.calls.save_call", fake_save_call)
    monkeypatch.setattr("app.db.cache.save_active_call", fake_save_active_call)
    monkeypatch.setattr("app.services.push_notification.send_regular_push", fake_send_regular_push)
    monkeypatch.setattr("app.services.push_notification.get_device_token", fake_get_device_token)
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr(lookup, "_lookup_twilio", spy_lookup_twilio)

    await twilio_incoming._post_routing_tasks(
        call_sid="CA_WITHHELD",
        caller_phone="anonymous",
        caller_name="",
        trust_score=20,
        score_breakdown={},
        route=Route.AI_SCREENING,
        lookups={"twilio": {}, "nomorobo": {}},
        conference_name="conf_withheld",
        contractor_id="c1",
        ws_token="ws_tok_4",
        caller_name_trusted=False,
    )

    assert len(saved_calls) == 1
    assert len(saved_active) == 1
    assert len(sent_pushes) == 1
    assert len(lookup_calls) == 1
    # Client is NOT constructed and fetch is NOT called because "anonymous" is noncanonical
    assert len(MockTwilioClient.instances) == 0
    assert len(MockTwilioClient.fetch_calls) == 0
    assert active_updates == []
