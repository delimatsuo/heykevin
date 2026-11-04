"""AI call tags come from what call extraction already stored, for Business Pro only."""

from __future__ import annotations

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest  # noqa: E402

from app.api import calls as calls_api  # noqa: E402
from app.services.call_tags import tags_for_call  # noqa: E402

PRO = {"subscription_tier": "businessPro", "subscription_status": "active"}
BUSINESS = {"subscription_tier": "business", "subscription_status": "active"}
LAPSED_PRO = {"subscription_tier": "businessPro", "subscription_status": "expired"}


@pytest.mark.parametrize(
    ("call_type", "urgency", "expected"),
    [
        ("service_request", "emergency", ["service_request", "emergency"]),
        ("service_request", "same_day", ["service_request", "same_day"]),
        ("service_request", "routine", ["service_request", "routine"]),
        ("service_request", "quote", ["service_request", "quote"]),
        ("out_of_scope", "emergency", ["out_of_scope", "emergency"]),
        ("personal", "none", ["personal"]),
        ("business", "none", ["business"]),
        ("spam", "none", ["spam"]),
    ],
)
def test_tags_come_from_stored_extraction(call_type, urgency, expected):
    assert tags_for_call({"call_type": call_type, "urgency": urgency}) == expected


@pytest.mark.parametrize(
    "call",
    [
        {},
        {"call_type": "unknown", "urgency": "none"},
        {"call_type": "", "urgency": ""},
        {"call_type": None, "urgency": None},
        {"call_type": 7, "urgency": ["emergency"]},
        {"call_type": "made_up", "urgency": "made_up"},
    ],
)
def test_unknown_or_malformed_values_produce_no_tag(call):
    assert tags_for_call(call) == []


@pytest.fixture
def api(monkeypatch):
    state = {"contractor": PRO, "lookups": 0}

    async def get_calls(_contractor_id):
        return [
            {"call_sid": "CA1", "contractor_id": "c-1", "call_type": "service_request", "urgency": "emergency"},
            {"call_sid": "CA2", "contractor_id": "c-1"},
        ]

    async def get_call(_sid):
        return {"call_sid": "CA1", "contractor_id": "c-1", "call_type": "personal", "urgency": "none"}

    async def get_contractor(_contractor_id):
        state["lookups"] += 1
        if isinstance(state["contractor"], Exception):
            raise state["contractor"]
        return state["contractor"]

    monkeypatch.setattr(calls_api, "get_calls_for_contractor", get_calls)
    monkeypatch.setattr(calls_api, "get_call", get_call)
    monkeypatch.setattr(calls_api, "get_contractor", get_contractor)
    monkeypatch.setattr(calls_api, "require_contractor_access", lambda *_a: None)
    return state


@pytest.mark.asyncio
async def test_pro_call_list_includes_tags(api):
    result = await calls_api.api_list_calls(None, "c-1")

    assert result["calls"][0]["tags"] == ["service_request", "emergency"]
    assert result["calls"][1]["tags"] == []
    assert api["lookups"] == 1


@pytest.mark.asyncio
async def test_pro_call_detail_includes_tags(api):
    result = await calls_api.api_get_call("CA1", None)

    assert result["tags"] == ["personal"]


@pytest.mark.asyncio
@pytest.mark.parametrize("contractor", [BUSINESS, LAPSED_PRO, None])
async def test_other_plans_get_no_tags(api, contractor):
    api["contractor"] = contractor

    listed = await calls_api.api_list_calls(None, "c-1")
    detail = await calls_api.api_get_call("CA1", None)

    assert all("tags" not in call for call in listed["calls"])
    assert "tags" not in detail


@pytest.mark.asyncio
async def test_failed_plan_lookup_still_returns_calls_without_tags(api):
    api["contractor"] = RuntimeError("store unavailable")

    listed = await calls_api.api_list_calls(None, "c-1")
    detail = await calls_api.api_get_call("CA1", None)

    assert listed["count"] == 2
    assert all("tags" not in call for call in listed["calls"])
    assert detail["call_sid"] == "CA1" and "tags" not in detail


@pytest.mark.asyncio
async def test_missing_call_does_not_look_up_a_plan(api, monkeypatch):
    async def no_call(_sid):
        return None

    monkeypatch.setattr(calls_api, "get_call", no_call)

    assert await calls_api.api_get_call("CA9", None) == {"error": "Not found"}
    assert api["lookups"] == 0
