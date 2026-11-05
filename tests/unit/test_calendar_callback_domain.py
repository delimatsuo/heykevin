"""Google's sign-in callback must sit on a domain we own for app verification."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.api import integrations  # noqa: E402

CALLBACK = "/api/integrations/google-calendar/callback"
OWN_DOMAIN = "https://api.heykevin.one"
RUN_URL = "https://kevin-api-752910912062.us-central1.run.app"


def test_callback_uses_the_configured_domain():
    assert integrations.google_calendar_redirect_uri(OWN_DOMAIN, RUN_URL) == OWN_DOMAIN + CALLBACK


def test_callback_falls_back_to_the_service_url_when_unset():
    assert integrations.google_calendar_redirect_uri("", RUN_URL) == RUN_URL + CALLBACK


def test_trailing_slash_does_not_change_the_callback():
    assert integrations.google_calendar_redirect_uri(OWN_DOMAIN + "/", RUN_URL) == OWN_DOMAIN + CALLBACK


def test_only_the_production_deploy_sets_the_own_domain():
    setting = f"GOOGLE_CALENDAR_REDIRECT_BASE_URL={OWN_DOMAIN}"
    workflow = Path(".github/workflows/deploy.yml").read_text()
    assert workflow.count(setting) == 1
    assert setting in workflow[workflow.index("ENVIRONMENT=production") :]
