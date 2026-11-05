"""Leaving Google's page always ends on a readable page with a way back to the app."""

from __future__ import annotations

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from app.api import integrations  # noqa: E402

CALENDAR_LINK = "heykevin://integrations/google-calendar"
JOBBER_LINK = "heykevin://integrations/jobber"
PATH = "/api/integrations/google-calendar/callback"


async def _get(query: str) -> httpx.Response:
    app = FastAPI()
    app.include_router(integrations.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        return await client.get(f"{PATH}?{query}")


@pytest.fixture
def exchange(monkeypatch):
    calls = []

    async def fake(code, state, request=None):
        calls.append((code, state))
        return HTMLResponse("connected page")

    monkeypatch.setattr(integrations, "google_calendar_callback", fake)
    return calls


def _raises(monkeypatch, status_code, detail):
    async def fake(code, state, request=None):
        raise HTTPException(status_code=status_code, detail=detail)

    monkeypatch.setattr(integrations, "google_calendar_callback", fake)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["error=access_denied&state=abc", "state=abc", "code=abc", ""],
)
async def test_canceling_shows_a_page_with_a_way_back(exchange, query):
    response = await _get(query)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert CALENDAR_LINK in response.text
    assert "wasn't connected" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert exchange == []


@pytest.mark.asyncio
async def test_approval_still_completes_the_connection(exchange):
    response = await _get("code=abc&state=def")

    assert response.text == "connected page"
    assert exchange == [("abc", "def")]


@pytest.mark.asyncio
async def test_unticked_permission_explains_what_to_do(monkeypatch):
    _raises(monkeypatch, 400, integrations.REDUCED_SCOPE_DETAIL)

    response = await _get("code=abc&state=def")

    assert response.status_code == 400
    assert "text/html" in response.headers["content-type"]
    assert "tick both" in response.text
    assert CALENDAR_LINK in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "detail"),
    [(400, "Invalid or expired state"), (500, "Failed to securely persist"), (502, "Bad gateway")],
)
async def test_other_failures_keep_their_status_and_hide_details(
    monkeypatch, status_code, detail
):
    _raises(monkeypatch, status_code, detail)

    response = await _get("code=abc&state=def")

    assert response.status_code == status_code
    assert "text/html" in response.headers["content-type"]
    assert detail not in response.text
    assert CALENDAR_LINK in response.text


@pytest.mark.parametrize(
    ("service", "link"),
    [("Google Calendar", CALENDAR_LINK), ("Jobber", JOBBER_LINK)],
)
def test_success_page_returns_to_the_app_by_itself(service, link):
    page = integrations._success_page(service)

    assert f'href="{link}"' in page
    assert f'content="1;url={link}"' in page
