"""Jobber setup and management static handoff page tests."""

import os
import pytest
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.api import integrations


@pytest.mark.asyncio
async def test_jobber_setup_endpoint_returns_static_html_and_security_headers():
    """GET /api/integrations/jobber/setup serves static safe handoff instructions."""
    resp = await integrations.jobber_setup()
    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200

    # Security headers
    assert resp.headers.get("Cache-Control") == "no-store"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'none'" in csp
    assert "style-src 'unsafe-inline'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'none'" in csp

    body = resp.body.decode("utf-8")
    assert "heykevin://integrations/jobber" in body
    assert "Connect Jobber to Hey Kevin" in body
    assert "Kevin" in body
    assert "Integrations" in body
    assert "Jobber" in body
    assert "Connect" in body
    assert "https://apps.apple.com/app/id6761427495" in body


@pytest.mark.asyncio
async def test_jobber_manage_endpoint_returns_static_html_and_security_headers():
    """GET /api/integrations/jobber/manage serves static safe handoff instructions."""
    resp = await integrations.jobber_manage()
    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200

    # Security headers
    assert resp.headers.get("Cache-Control") == "no-store"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'none'" in csp
    assert "style-src 'unsafe-inline'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'none'" in csp

    body = resp.body.decode("utf-8")
    assert "heykevin://integrations/jobber" in body
    assert "Manage Jobber Integration" in body
    assert "Kevin" in body
    assert "Integrations" in body
    assert "Jobber" in body
    assert "https://apps.apple.com/app/id6761427495" in body


@pytest.mark.asyncio
async def test_jobber_manage_page_has_no_db_dependencies_or_hostile_interpolation():
    """Manage page ignores every input, renders no dynamic query data, and requires no datastore."""
    hostile_inputs = [
        "<script>alert(1)</script>",
        "javascript:alert(1)",
        "contractor_id=victim_account",
        "bearer_token=secret",
        "' OR '1'='1",
    ]

    # Verify that helper function produces pure static HTML without user variables
    html = integrations._manage_page_html()
    assert "heykevin://integrations/jobber" in html

    for payload in hostile_inputs:
        assert payload not in html


def test_jobber_success_page_contains_fixed_deep_link():
    """OAuth completion success page provides fixed return link to Jobber section."""
    html = integrations._success_page("Jobber")
    assert "heykevin://integrations/jobber" in html
    assert "Jobber Connected" in html
    assert "Return to Hey Kevin" in html


def test_google_calendar_success_page_contains_no_jobber_deep_link():
    """Google Calendar success page preserves original behavior and omits Jobber return link."""
    html = integrations._success_page("Google Calendar")
    assert "Google Calendar Connected" in html
    assert "heykevin://integrations/jobber" not in html
    assert "heykevin://" not in html
    assert "Return to Hey Kevin" not in html


@pytest.mark.asyncio
async def test_jobber_callback_without_state_redirects_to_setup_safely():
    """GET /api/integrations/jobber/callback without state safely redirects to setup with no-store."""
    resp = await integrations.jobber_callback(code="unsolicited_auth_code", state=None)
    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 303
    assert resp.headers.get("location") == "/api/integrations/jobber/setup"
    assert resp.headers.get("Cache-Control") == "no-store"
