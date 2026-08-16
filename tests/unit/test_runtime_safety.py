"""Runtime environment safety checks."""

import base64
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
    monkeypatch.setattr(
        config.settings,
        "public_demo_breaker_url",
        "https://kevin-demo-breaker.example.run.app",
    )
    monkeypatch.setattr(
        config.settings,
        "public_demo_breaker_audience",
        "https://kevin-demo-breaker.example.run.app",
    )
    monkeypatch.setattr(config.settings, "public_demo_breaker_hmac_secret", "b" * 32)
    monkeypatch.setattr(
        config.settings,
        "public_demo_breaker_caller_service_account",
        "public-demo@example.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(
        config.settings,
        "transcript_encryption_key",
        base64.b64encode(b"k" * 32).decode("ascii"),
    )


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


def test_generic_runtime_rejects_private_breaker_authority(monkeypatch):
    _set_common(monkeypatch)
    monkeypatch.setattr(config.settings, "environment", "staging")
    monkeypatch.setattr(config.settings, "cloud_run_url", "https://kevin-staging.example.run.app")
    monkeypatch.setattr(config.settings, "firestore_project_id", "kevin-staging")
    monkeypatch.setattr(
        config.settings,
        "firebase_database_url",
        "https://kevin-staging-rtdb.firebaseio.com",
    )
    monkeypatch.setattr(config.settings, "twilio_account_sid", "AC_STAGING")
    monkeypatch.setattr(
        config.settings,
        "public_demo_breaker_twilio_parent_main_api_key_secret",
        "parent-secret-must-never-be-generic",
    )

    with pytest.raises(RuntimeError, match="forbidden outside the private"):
        config.validate_runtime_safety()


def test_enabled_public_demo_accepts_only_isolated_demo_runtime(monkeypatch):
    _set_common(monkeypatch)
    monkeypatch.setattr(config.settings, "environment", "demo")
    monkeypatch.setattr(config.settings, "cloud_run_url", "https://kevin-demo.example.run.app")
    monkeypatch.setattr(config.settings, "firestore_project_id", "kevin-public-demo")
    monkeypatch.setattr(
        config.settings,
        "firebase_database_url",
        "https://kevin-public-demo-rtdb.firebaseio.com",
    )
    monkeypatch.setattr(config.settings, "twilio_account_sid", "AC_DEMO")
    monkeypatch.setattr(config.settings, "public_demo_enabled", True)
    monkeypatch.setattr(config.settings, "public_demo_number", "+12025550199")
    monkeypatch.setattr(config.settings, "public_demo_hmac_secret", "x" * 32)
    monkeypatch.setattr(config.settings, "public_demo_ttl_policies_verified", True)
    monkeypatch.setattr(
        config.settings,
        "public_demo_twilio_usage_trigger_sid",
        "UT" + "1" * 32,
    )

    with pytest.raises(RuntimeError, match="dedicated public demo entry point"):
        config.validate_runtime_safety()
    config.validate_runtime_safety(public_demo_entrypoint=True)


def test_enabled_public_demo_is_rejected_on_generic_runtime(monkeypatch):
    _set_common(monkeypatch)
    monkeypatch.setattr(config.settings, "environment", "test")
    monkeypatch.setattr(config.settings, "cloud_run_url", "https://kevin-demo.example.run.app")
    monkeypatch.setattr(config.settings, "public_demo_enabled", True)
    monkeypatch.setattr(config.settings, "public_demo_number", "+12025550199")
    monkeypatch.setattr(config.settings, "public_demo_hmac_secret", "x" * 32)
    monkeypatch.setattr(config.settings, "public_demo_ttl_policies_verified", True)

    with pytest.raises(RuntimeError, match="requires ENVIRONMENT=demo"):
        config.validate_runtime_safety()


def test_demo_runtime_rejects_debug_transport_logging(monkeypatch):
    _set_common(monkeypatch)
    monkeypatch.setattr(config.settings, "environment", "demo")
    monkeypatch.setattr(config.settings, "cloud_run_url", "https://kevin-demo.example.run.app")
    monkeypatch.setattr(config.settings, "firestore_project_id", "kevin-public-demo")
    monkeypatch.setattr(
        config.settings,
        "firebase_database_url",
        "https://kevin-public-demo-rtdb.firebaseio.com",
    )
    monkeypatch.setattr(config.settings, "twilio_account_sid", "AC_DEMO")
    monkeypatch.setattr(config.settings, "log_level", "DEBUG")

    with pytest.raises(RuntimeError, match="LOG_LEVEL=DEBUG is forbidden"):
        config.validate_runtime_safety(public_demo_entrypoint=True)


def test_enabled_demo_rejects_unverified_ttl_policies(monkeypatch):
    _set_common(monkeypatch)
    monkeypatch.setattr(config.settings, "environment", "demo")
    monkeypatch.setattr(config.settings, "cloud_run_url", "https://kevin-demo.example.run.app")
    monkeypatch.setattr(config.settings, "firestore_project_id", "kevin-public-demo")
    monkeypatch.setattr(
        config.settings,
        "firebase_database_url",
        "https://kevin-public-demo-rtdb.firebaseio.com",
    )
    monkeypatch.setattr(config.settings, "twilio_account_sid", "AC_DEMO")
    monkeypatch.setattr(config.settings, "public_demo_enabled", True)
    monkeypatch.setattr(config.settings, "public_demo_number", "+12025550199")
    monkeypatch.setattr(config.settings, "public_demo_hmac_secret", "x" * 32)
    monkeypatch.setattr(config.settings, "public_demo_ttl_policies_verified", False)

    with pytest.raises(RuntimeError, match="PUBLIC_DEMO_TTL_POLICIES_VERIFIED"):
        config.validate_runtime_safety(public_demo_entrypoint=True)


def test_enabled_demo_rejects_missing_or_oversized_spend_breaker(monkeypatch):
    _set_common(monkeypatch)
    monkeypatch.setattr(config.settings, "environment", "demo")
    monkeypatch.setattr(config.settings, "cloud_run_url", "https://kevin-demo.example.run.app")
    monkeypatch.setattr(config.settings, "firestore_project_id", "kevin-public-demo")
    monkeypatch.setattr(
        config.settings,
        "firebase_database_url",
        "https://kevin-public-demo-rtdb.firebaseio.com",
    )
    monkeypatch.setattr(config.settings, "twilio_account_sid", "AC_DEMO")
    monkeypatch.setattr(config.settings, "public_demo_enabled", True)
    monkeypatch.setattr(config.settings, "public_demo_number", "+12025550199")
    monkeypatch.setattr(config.settings, "public_demo_hmac_secret", "x" * 32)
    monkeypatch.setattr(config.settings, "public_demo_ttl_policies_verified", True)
    monkeypatch.setattr(config.settings, "public_demo_twilio_usage_trigger_sid", "")
    monkeypatch.setattr(config.settings, "public_demo_twilio_daily_spend_limit_usd", 26)

    with pytest.raises(RuntimeError, match="PUBLIC_DEMO_TWILIO_USAGE_TRIGGER_SID"):
        config.validate_runtime_safety(public_demo_entrypoint=True)
    monkeypatch.setattr(
        config.settings,
        "public_demo_twilio_usage_trigger_sid",
        "UT" + "1" * 32,
    )
    with pytest.raises(RuntimeError, match="PUBLIC_DEMO_TWILIO_DAILY_SPEND_LIMIT_USD"):
        config.validate_runtime_safety(public_demo_entrypoint=True)


def _set_enabled_bounded_demo(monkeypatch) -> None:
    _set_common(monkeypatch)
    monkeypatch.setattr(config.settings, "environment", "demo")
    monkeypatch.setattr(config.settings, "cloud_run_url", "https://kevin-demo.example.run.app")
    monkeypatch.setattr(config.settings, "firestore_project_id", "kevin-public-demo")
    monkeypatch.setattr(
        config.settings,
        "firebase_database_url",
        "https://kevin-public-demo-rtdb.firebaseio.com",
    )
    monkeypatch.setattr(config.settings, "twilio_account_sid", "AC_DEMO")
    monkeypatch.setattr(config.settings, "public_demo_enabled", True)
    monkeypatch.setattr(config.settings, "public_demo_number", "+12025550199")
    monkeypatch.setattr(config.settings, "public_demo_hmac_secret", "x" * 32)
    monkeypatch.setattr(config.settings, "public_demo_ttl_policies_verified", True)
    monkeypatch.setattr(
        config.settings,
        "public_demo_twilio_usage_trigger_sid",
        "UT" + "1" * 32,
    )


@pytest.mark.parametrize(
    ("concurrency", "duration", "expected_error"),
    [
        (3, 180, "PUBLIC_DEMO_CONCURRENCY_LIMIT"),
        (2, 181, "PUBLIC_DEMO_MAX_CALL_DURATION_SECONDS"),
    ],
)
def test_enabled_demo_rejects_unbounded_drain(monkeypatch, concurrency, duration, expected_error):
    _set_enabled_bounded_demo(monkeypatch)
    monkeypatch.setattr(config.settings, "public_demo_concurrency_limit", concurrency)
    monkeypatch.setattr(config.settings, "public_demo_max_call_duration_seconds", duration)

    with pytest.raises(RuntimeError, match=expected_error):
        config.validate_runtime_safety(public_demo_entrypoint=True)


def test_enabled_demo_accepts_exact_bounded_drain(monkeypatch):
    _set_enabled_bounded_demo(monkeypatch)
    monkeypatch.setattr(config.settings, "public_demo_concurrency_limit", 2)
    monkeypatch.setattr(config.settings, "public_demo_max_call_duration_seconds", 180)

    config.validate_runtime_safety(public_demo_entrypoint=True)


def test_staging_rejects_production_apns_endpoint(monkeypatch):
    _set_common(monkeypatch)
    monkeypatch.setattr(config.settings, "environment", "staging")
    monkeypatch.setattr(config.settings, "cloud_run_url", "https://kevin-api-staging.example.run.app")
    monkeypatch.setattr(config.settings, "firestore_project_id", "kevin-staging")
    monkeypatch.setattr(
        config.settings,
        "firebase_database_url",
        "https://kevin-staging-rtdb.firebaseio.com",
    )
    monkeypatch.setattr(config.settings, "twilio_account_sid", "AC_STAGING")
    monkeypatch.setattr(config.settings, "apns_sandbox", False)

    with pytest.raises(RuntimeError, match="APNS_SANDBOX must be true"):
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


def test_staging_rejects_missing_or_invalid_transcript_encryption_key(monkeypatch):
    _set_common(monkeypatch)
    monkeypatch.setattr(config.settings, "environment", "staging")
    monkeypatch.setattr(config.settings, "cloud_run_url", "https://kevin-api-staging.example.run.app")
    monkeypatch.setattr(config.settings, "firestore_project_id", "kevin-staging")
    monkeypatch.setattr(
        config.settings,
        "firebase_database_url",
        "https://kevin-staging-rtdb.firebaseio.com",
    )
    monkeypatch.setattr(config.settings, "twilio_account_sid", "AC_STAGING")

    for invalid_key in ("", "not-valid-base64", base64.b64encode(b"short").decode("ascii")):
        monkeypatch.setattr(config.settings, "transcript_encryption_key", invalid_key)
        with pytest.raises(RuntimeError, match="TRANSCRIPT_ENCRYPTION_KEY"):
            config.validate_runtime_safety()


def test_development_allows_missing_transcript_encryption_key(monkeypatch):
    _set_common(monkeypatch)
    monkeypatch.setattr(config.settings, "environment", "development")
    monkeypatch.setattr(config.settings, "allow_production_resources_in_non_production", True)
    monkeypatch.setattr(config.settings, "transcript_encryption_key", "")

    config.validate_runtime_safety()


def test_production_requires_explicit_twilio_account_boundary(monkeypatch):
    monkeypatch.setattr(config.settings, "environment", "production")
    monkeypatch.setattr(config.settings, "appstore_environment", "production")
    monkeypatch.setattr(config.settings, "apns_sandbox", False)
    monkeypatch.setattr(config.settings, "cloud_run_url", config.PRODUCTION_CLOUD_RUN_URL)
    monkeypatch.setattr(config.settings, "firestore_project_id", config.PRODUCTION_GCP_PROJECT_ID)
    monkeypatch.setattr(
        config.settings,
        "firebase_database_url",
        config.PRODUCTION_FIREBASE_DATABASE_URL,
    )
    monkeypatch.setattr(config.settings, "production_twilio_account_sid", "")
    monkeypatch.setattr(config.settings, "twilio_account_sid", "AC_ACTIVE")

    with pytest.raises(RuntimeError, match="PRODUCTION_TWILIO_ACCOUNT_SID is required"):
        config.validate_runtime_safety()
