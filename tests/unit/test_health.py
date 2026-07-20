"""Health endpoint release metadata tests."""

import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC_TEST")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

from app import main


@pytest.mark.asyncio
async def test_health_includes_deploy_identity_without_secrets(monkeypatch):
    monkeypatch.setattr(main.settings, "environment", "staging")
    monkeypatch.setenv("K_SERVICE", "kevin-api-staging")
    monkeypatch.setenv("K_REVISION", "kevin-api-staging-00028-suv")
    monkeypatch.setenv("DEPLOY_SHA", "abc123def456")
    monkeypatch.setattr(main.settings, "gemini_controlled_pipeline_enabled", True)
    monkeypatch.setattr(
        main.settings,
        "gemini_controlled_tts_zero_retention_enabled",
        True,
    )

    response = await main.health()

    assert response == {
        "status": "ok",
        "environment": "staging",
        "service": "kevin-api-staging",
        "revision": "kevin-api-staging-00028-suv",
        "deploy_sha": "abc123def456",
        "gemini_controlled_pipeline_enabled": True,
        "gemini_controlled_pipeline_version": "gemini-controlled-v2",
        "gemini_controlled_tts_zero_retention_enabled": True,
    }
