"""Application configuration. Loads from .env locally, Secret Manager in production."""

import base64 as _base64
import binascii as _binascii
import json as _json
from typing import Optional as _Optional

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
    anthropic_model: str = "claude-sonnet-5"
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

    # vCard URL signing (F-08): dedicated HMAC secret. If unset in production
    # the server still boots, but a clear warning is logged at startup and
    # vcard signing falls back to a value derived from API_BEARER_TOKEN for
    # backwards-compat. Required (not blank) for production hardening.
    vcard_hmac_secret: str = ""

    # Dial-in PIN brute-force protection (F-15). 10 attempts / 60 minutes per
    # source key by default. Tunable via env to support legitimate retry
    # patterns (e.g. PSTN re-dials) without weakening the lockout.
    pin_rate_limit: int = 10
    pin_rate_window_seconds: int = 3600

    # Gemini
    gemini_api_key: str = ""
    gemini_live_model: str = "gemini-2.5-flash-native-audio-latest"
    gemini_live_thinking_budget: int = 0
    gemini_live_temperature: float = 0.4
    # Text model for the ConversationRelay engine. gemini-2.5-flash is gated
    # ("no longer available to new users") for this API project — keep this on
    # a current GA flash model. Env-overridable so a model retirement is a
    # config change, not a deploy.
    relay_text_model: str = "gemini-3.5-flash"

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

    # Application-level encryption for call transcripts at rest (F-11).
    # 32-byte AES-256-GCM key, base64 encoded. Generate with
    # `python scripts/gen_transcript_key.py`. Staging and production require a
    # valid key; development and tests retain legacy plaintext compatibility.
    transcript_encryption_key: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def staging_native_live_safety_controls_enabled() -> bool:
    """Return whether the staging-only Live safety envelope is active.

    This is intentionally derived from the deployment environment rather than a
    mutable caller-facing setting.  The first native-Live qualification must
    not expose model tools or turn model wording into an autonomous hangup.
    """
    return (settings.environment or "").strip().lower() == "staging"


def decode_transcript_encryption_key(raw: str) -> bytes | None:
    """Return a valid 32-byte transcript key without logging key material."""
    if not raw or not raw.strip():
        return None
    try:
        key = _base64.b64decode(raw.strip(), validate=True)
    except (_binascii.Error, ValueError):
        return None
    return key if len(key) == 32 else None


def validate_runtime_safety() -> None:
    """Fail fast when an environment is pointed at the wrong runtime resources."""
    env = (settings.environment or "").strip().lower()
    errors: list[str] = []

    if env not in {"development", "staging", "production", "test"}:
        errors.append("ENVIRONMENT must be one of development, staging, production, or test")

    if env in {"staging", "production"} and decode_transcript_encryption_key(
        settings.transcript_encryption_key
    ) is None:
        errors.append(
            "TRANSCRIPT_ENCRYPTION_KEY must be valid 32-byte base64 in staging and production"
        )

    if env == "production":
        if settings.appstore_environment != "production":
            errors.append("APPSTORE_ENVIRONMENT must be production when ENVIRONMENT=production")
        if settings.apns_sandbox:
            errors.append("APNS_SANDBOX must be false when ENVIRONMENT=production")
        if "staging" in settings.cloud_run_url:
            errors.append("CLOUD_RUN_URL must not point at staging when ENVIRONMENT=production")
        if settings.firestore_project_id and settings.firestore_project_id != PRODUCTION_GCP_PROJECT_ID:
            errors.append("FIRESTORE_PROJECT_ID must be the production project when ENVIRONMENT=production")
        if not settings.production_twilio_account_sid:
            errors.append("PRODUCTION_TWILIO_ACCOUNT_SID is required in production")
        elif settings.twilio_account_sid != settings.production_twilio_account_sid:
            errors.append("TWILIO_ACCOUNT_SID must be the production account when ENVIRONMENT=production")

    if env in {"development", "staging"} and not settings.allow_production_resources_in_non_production:
        if settings.appstore_environment == "production":
            errors.append("APPSTORE_ENVIRONMENT must not be production outside ENVIRONMENT=production")
        if not settings.apns_sandbox:
            errors.append("APNS_SANDBOX must be true outside ENVIRONMENT=production")
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
