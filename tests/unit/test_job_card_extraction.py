"""Regression tests for job-card extraction against the Anthropic Messages API.

Production incident 2026-08-06 (call CAfed098): every extraction failed with
``Job card extraction error: 'text'``. The configured model (claude-sonnet-5)
runs adaptive thinking when the ``thinking`` parameter is omitted, so the first
``content`` block is a ``thinking`` block and ``data["content"][0]["text"]``
raises KeyError. The fix must (a) find the first *text* block wherever it sits,
and (b) explicitly disable thinking so the small ``max_tokens`` budget is spent
on the JSON payload, not on reasoning.
"""

import json

import httpx
import pytest

from app.services import job_card


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Captures the request body and returns a canned Anthropic response."""

    captured_json = None
    response_payload = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None, timeout=None):
        type(self).captured_json = json
        return _FakeResponse(type(self).response_payload)


@pytest.fixture
def fake_client(monkeypatch):
    _FakeAsyncClient.captured_json = None
    _FakeAsyncClient.response_payload = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


_CARD_JSON = json.dumps({
    "call_type": "service_request",
    "caller_name": "Ana",
    "issue_description": "toilet replacement",
    "urgency": "routine",
})


@pytest.mark.asyncio
async def test_extracts_text_block_after_thinking_block(fake_client):
    """A leading thinking block must not break extraction (the CAfed098 bug)."""
    fake_client.response_payload = {
        "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": _CARD_JSON},
        ],
    }

    result = await job_card.extract_job_card("caller: hi", "+15551234567")

    assert result["call_type"] == "service_request"
    assert result["issue_description"] == "toilet replacement"
    assert result["caller_phone"] == "+15551234567"


@pytest.mark.asyncio
async def test_no_text_block_falls_back_to_minimal_card(fake_client):
    fake_client.response_payload = {
        "stop_reason": "end_turn",
        "content": [{"type": "thinking", "thinking": ""}],
    }

    result = await job_card.extract_job_card("caller: hi", "+15551234567")

    assert result["call_type"] == "unknown"
    assert result["caller_phone"] == "+15551234567"


@pytest.mark.asyncio
async def test_request_disables_thinking(fake_client):
    """The request must pin thinking off so max_tokens covers only the JSON."""
    fake_client.response_payload = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": _CARD_JSON}],
    }

    await job_card.extract_job_card("caller: hi", "+15551234567")

    body = fake_client.captured_json
    assert body is not None
    assert body.get("thinking") == {"type": "disabled"}
