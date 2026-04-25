"""Application configuration. Loads from .env locally, Secret Manager in production."""

import os

from pydantic_settings import BaseSettings


PRODUCTION_GCP_PROJECT_ID = "kevin-491315"
PRODUCTION_CLOUD_RUN_URL = "https://kevin-api-752910912062.us-central1.run.app"
PRODUCTION_FIREBASE_DATABASE_URL = "https://kevin-491315-rtdb.firebaseio.com"


class Settings(BaseSettings):
    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str
    production_twilio_account_sid: str = ""

    # Vapi (deprecated — kept for backward compat)
    vapi_api_key: str = ""
    vapi_public_key: str = ""
    vapi_phone_number_id: str = ""
    vapi_webhook_secret: str = ""

    # AI Services
    anthropic_api_key: str = ""
    deepgram_api_key: str = ""
    fish_audio_api_key: str = ""
    elevenlabs_api_key: str = ""

    # Telegram
    telegram_bot_token: str
    telegram_webhook_secret: str = ""
    telegram_chat_id: str = ""

    # User config (single-user MVP)
    user_phone: str
    user_name: str = "the owner"

    # API auth
    api_bearer_token: str = ""

    # Gemini
    gemini_api_key: str = ""

    # Twilio Voice SDK (for iOS app)
    twilio_api_key_sid: str = ""      # API Key SID (not the Account SID)
    twilio_api_key_secret: str = ""   # API Key Secret
    twilio_twiml_app_sid: str = ""    # TwiML App SID

    # APNs (for VoIP push to iOS app)
    apns_key_id: str = ""             # Key ID from .p8 file
    apns_team_id: str = ""            # Apple Developer Team ID
    apns_key_content: str = ""        # .p8 key file content (PEM)
    apns_bundle_id: str = ""          # App bundle ID (e.g., com.kevin.app)

    # Dial-in number (DEPRECATED — use dial_in_numbers for per-country support)
    dial_in_number: str = "+16504222696"

    # Regional dial-in numbers (JSON string, parsed by get_dial_in_number helper)
    # One per supported country. Provision new numbers as countries are added.
    dial_in_numbers: str = ""

    # Jobber (FSM integration)
    jobber_client_id: str = ""
    jobber_client_secret: str = ""

    # Google Calendar (fallback scheduling for non-Jobber contractors)
    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""

    # App Store Server API (for subscription verification and offer signing)
    appstore_key_id: str = ""         # Key ID from App Store Connect
    appstore_issuer_id: str = ""      # Issuer ID from App Store Connect
    appstore_private_key: str = ""    # .p8 key content (PEM, | as newline separator)
    appstore_bundle_id: str = "com.kevin.callscreen"  # App bundle ID
    appstore_environment: str = "sandbox"  # "sandbox" or "production"

    # Cloud Run URL (for WebSocket URL generation)
    cloud_run_url: str = PRODUCTION_CLOUD_RUN_URL

    # Firebase / Firestore
    # Production may rely on Cloud Run ADC. Staging/development must set an
    # explicit non-production project and RTDB URL to avoid touching live data.
    firestore_project_id: str = ""
    firebase_database_url: str = PRODUCTION_FIREBASE_DATABASE_URL

    # App
    environment: str = "development"
    apns_sandbox: bool = True  # Use APNs sandbox endpoint; set to false for App Store builds
    allow_production_resources_in_non_production: bool = False
    log_level: str = "INFO"
    port: int = 8080

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def validate_runtime_safety() -> None:
    """Fail fast when an environment is pointed at the wrong runtime resources."""
    env = (settings.environment or "").strip().lower()
    errors: list[str] = []

    if env not in {"development", "staging", "production", "test"}:
        errors.append("ENVIRONMENT must be one of development, staging, production, or test")

    if env == "production":
        if settings.appstore_environment != "production":
            errors.append("APPSTORE_ENVIRONMENT must be production when ENVIRONMENT=production")
        if settings.apns_sandbox:
            errors.append("APNS_SANDBOX must be false when ENVIRONMENT=production")
        if "staging" in settings.cloud_run_url:
            errors.append("CLOUD_RUN_URL must not point at staging when ENVIRONMENT=production")
        if settings.firestore_project_id and settings.firestore_project_id != PRODUCTION_GCP_PROJECT_ID:
            errors.append("FIRESTORE_PROJECT_ID must be the production project when ENVIRONMENT=production")
        if (
            settings.production_twilio_account_sid
            and settings.twilio_account_sid != settings.production_twilio_account_sid
        ):
            errors.append("TWILIO_ACCOUNT_SID must be the production account when ENVIRONMENT=production")

    if env in {"development", "staging"} and not settings.allow_production_resources_in_non_production:
        if settings.appstore_environment == "production":
            errors.append("APPSTORE_ENVIRONMENT must not be production outside ENVIRONMENT=production")
        if settings.cloud_run_url == PRODUCTION_CLOUD_RUN_URL:
            errors.append("CLOUD_RUN_URL must not be the production URL outside ENVIRONMENT=production")
        if not settings.firestore_project_id:
            errors.append("FIRESTORE_PROJECT_ID is required outside production")
        elif settings.firestore_project_id == PRODUCTION_GCP_PROJECT_ID:
            errors.append("FIRESTORE_PROJECT_ID must not be the production project outside production")
        if settings.firebase_database_url == PRODUCTION_FIREBASE_DATABASE_URL:
            errors.append("FIREBASE_DATABASE_URL must not be the production RTDB outside production")
        if not settings.production_twilio_account_sid:
            errors.append("PRODUCTION_TWILIO_ACCOUNT_SID is required outside production")
        elif settings.twilio_account_sid == settings.production_twilio_account_sid:
            errors.append("TWILIO_ACCOUNT_SID must not be the production account outside production")

    if errors:
        raise RuntimeError("Unsafe runtime configuration: " + "; ".join(errors))

import json as _json
from typing import Optional as _Optional
_dial_in_cache: _Optional[dict] = None


def get_dial_in_number(country_code: str = "US") -> str:
    """Get the dial-in number for a country, falling back to US, then legacy field."""
    global _dial_in_cache
    if _dial_in_cache is None:
        try:
            _dial_in_cache = _json.loads(settings.dial_in_numbers) if settings.dial_in_numbers else {}
        except (_json.JSONDecodeError, TypeError):
            _dial_in_cache = {}
    if _dial_in_cache:
        return _dial_in_cache.get(country_code, _dial_in_cache.get("US", settings.dial_in_number))
    return settings.dial_in_number
