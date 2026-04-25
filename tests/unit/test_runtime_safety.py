"""Runtime environment safety checks."""

import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC_TEST")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

from app import config


def _set_common(monkeypatch):
    monkeypatch.setattr(config.settings, "allow_production_resources_in_non_production", False)
    monkeypatch.setattr(config.settings, "appstore_environment", "sandbox")
    monkeypatch.setattr(config.settings, "apns_sandbox", True)
    monkeypatch.setattr(config.settings, "production_twilio_account_sid", "AC_PROD")


def test_staging_rejects_production_data_resources(monkeypatch):
    _set_common(monkeypatch)
    monkeypatch.setattr(config.settings, "environment", "staging")
    monkeypatch.setattr(config.settings, "cloud_run_url", config.PRODUCTION_CLOUD_RUN_URL)
    monkeypatch.setattr(config.settings, "firestore_project_id", config.PRODUCTION_GCP_PROJECT_ID)
    monkeypatch.setattr(config.settings, "firebase_database_url", config.PRODUCTION_FIREBASE_DATABASE_URL)
    monkeypatch.setattr(config.settings, "twilio_account_sid", "AC_PROD")

    with pytest.raises(RuntimeError, match="Unsafe runtime configuration"):
        config.validate_runtime_safety()


def test_staging_accepts_isolated_resources(monkeypatch):
    _set_common(monkeypatch)
    monkeypatch.setattr(config.settings, "environment", "staging")
    monkeypatch.setattr(config.settings, "cloud_run_url", "https://kevin-api-staging.example.run.app")
    monkeypatch.setattr(config.settings, "firestore_project_id", "kevin-staging")
    monkeypatch.setattr(config.settings, "firebase_database_url", "https://kevin-staging-rtdb.firebaseio.com")
    monkeypatch.setattr(config.settings, "twilio_account_sid", "AC_STAGING")

    config.validate_runtime_safety()


def test_production_requires_production_billing_and_push(monkeypatch):
    monkeypatch.setattr(config.settings, "environment", "production")
    monkeypatch.setattr(config.settings, "appstore_environment", "sandbox")
    monkeypatch.setattr(config.settings, "apns_sandbox", True)
    monkeypatch.setattr(config.settings, "cloud_run_url", config.PRODUCTION_CLOUD_RUN_URL)
    monkeypatch.setattr(config.settings, "firestore_project_id", config.PRODUCTION_GCP_PROJECT_ID)
    monkeypatch.setattr(config.settings, "firebase_database_url", config.PRODUCTION_FIREBASE_DATABASE_URL)
    monkeypatch.setattr(config.settings, "production_twilio_account_sid", "AC_PROD")
    monkeypatch.setattr(config.settings, "twilio_account_sid", "AC_PROD")

    with pytest.raises(RuntimeError, match="APPSTORE_ENVIRONMENT must be production"):
        config.validate_runtime_safety()
