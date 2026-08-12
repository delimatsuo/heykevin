"""Application configuration. Loads from .env locally, Secret Manager in production."""

import base64 as _base64
import binascii as _binascii
import json as _json
import re as _re

from pydantic_settings import BaseSettings

PRODUCTION_GCP_PROJECT_ID = "kevin-491315"
PRODUCTION_CLOUD_RUN_URL = "https://kevin-api-752910912062.us-central1.run.app"
PRODUCTION_FIREBASE_DATABASE_URL = "https://kevin-491315-rtdb.firebaseio.com"
_E164_RE = _re.compile(r"\+[1-9]\d{7,14}\Z")
_TWILIO_USAGE_TRIGGER_SID_RE = _re.compile(r"UT[0-9a-fA-F]{32}\Z")


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

    # Public try-it-yourself phone demo. This is intentionally disabled unless
    # every value is set explicitly. The demo has its own fail-closed webhook
    # and never falls through to the ordinary contractor/owner call path.
    public_demo_enabled: bool = False
    public_demo_number: str = ""
    public_demo_hmac_secret: str = ""
    public_demo_per_caller_limit: int = 3
    public_demo_per_caller_window_seconds: int = 3600
    public_demo_daily_call_limit: int = 100
    public_demo_concurrency_limit: int = 3
    public_demo_max_call_duration_seconds: int = 180
    public_demo_lease_ttl_seconds: int = 300
    public_demo_twilio_daily_spend_limit_usd: float = 5.0
    public_demo_twilio_usage_trigger_sid: str = ""
    # Operator assertion set only after the required Firestore TTL policies have
    # been inspected in the isolated demo project and deletion has been proven.
    public_demo_ttl_policies_verified: bool = False

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


def validate_runtime_safety(*, public_demo_entrypoint: bool = False) -> None:
    """Fail fast when an environment is pointed at the wrong runtime resources."""
    env = (settings.environment or "").strip().lower()
    errors: list[str] = []

    if env not in {"development", "staging", "production", "demo", "test"}:
        errors.append(
            "ENVIRONMENT must be one of development, staging, production, demo, or test"
        )

    if env == "demo" and not public_demo_entrypoint:
        errors.append("ENVIRONMENT=demo requires the dedicated public demo entry point")
    if env != "demo" and public_demo_entrypoint:
        errors.append("The dedicated public demo entry point requires ENVIRONMENT=demo")

    if settings.public_demo_enabled:
        demo_number = settings.public_demo_number.strip()
        if env != "demo":
            errors.append("PUBLIC_DEMO_ENABLED=true requires ENVIRONMENT=demo")
        if not _E164_RE.fullmatch(demo_number):
            errors.append(
                "PUBLIC_DEMO_NUMBER must be an exact E.164 number when "
                "PUBLIC_DEMO_ENABLED=true"
            )
        if not settings.cloud_run_url.startswith("https://"):
            errors.append("CLOUD_RUN_URL must use HTTPS when PUBLIC_DEMO_ENABLED=true")
        if len(settings.public_demo_hmac_secret.strip()) < 32:
            errors.append(
                "PUBLIC_DEMO_HMAC_SECRET must be at least 32 characters when "
                "PUBLIC_DEMO_ENABLED=true"
            )
        if not settings.public_demo_ttl_policies_verified:
            errors.append(
                "PUBLIC_DEMO_TTL_POLICIES_VERIFIED=true is required after verified "
                "Firestore TTL configuration when PUBLIC_DEMO_ENABLED=true"
            )
        if not _TWILIO_USAGE_TRIGGER_SID_RE.fullmatch(
            settings.public_demo_twilio_usage_trigger_sid.strip()
        ):
            errors.append(
                "PUBLIC_DEMO_TWILIO_USAGE_TRIGGER_SID must bind the enabled demo "
                "to its exact daily spend trigger"
            )
        if not 0 < settings.public_demo_twilio_daily_spend_limit_usd <= 25:
            errors.append(
                "PUBLIC_DEMO_TWILIO_DAILY_SPEND_LIMIT_USD must be greater than 0 "
                "and no more than 25"
            )
        if settings.public_demo_per_caller_limit <= 0:
            errors.append("PUBLIC_DEMO_PER_CALLER_LIMIT must be positive")
        if settings.public_demo_per_caller_window_seconds <= 0:
            errors.append("PUBLIC_DEMO_PER_CALLER_WINDOW_SECONDS must be positive")
        if settings.public_demo_daily_call_limit <= 0:
            errors.append("PUBLIC_DEMO_DAILY_CALL_LIMIT must be positive")
        if settings.public_demo_concurrency_limit <= 0:
            errors.append("PUBLIC_DEMO_CONCURRENCY_LIMIT must be positive")
        if not 30 <= settings.public_demo_max_call_duration_seconds <= 300:
            errors.append(
                "PUBLIC_DEMO_MAX_CALL_DURATION_SECONDS must be between 30 and 300"
            )
        if (
            settings.public_demo_lease_ttl_seconds
            < settings.public_demo_max_call_duration_seconds + 60
        ):
            errors.append(
                "PUBLIC_DEMO_LEASE_TTL_SECONDS must be at least the max call duration plus 60"
            )

    if env == "demo" and settings.allow_production_resources_in_non_production:
        errors.append(
            "ALLOW_PRODUCTION_RESOURCES_IN_NON_PRODUCTION must be false when ENVIRONMENT=demo"
        )
    if env == "demo" and (settings.log_level or "").strip().upper() == "DEBUG":
        errors.append(
            "LOG_LEVEL=DEBUG is forbidden when ENVIRONMENT=demo because provider "
            "transport logs may contain credentials or caller media"
        )

    if env in {"staging", "production", "demo"} and decode_transcript_encryption_key(
        settings.transcript_encryption_key
    ) is None:
        errors.append(
            "TRANSCRIPT_ENCRYPTION_KEY must be valid 32-byte base64 in staging, "
            "production, and demo"
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

    if env in {"development", "staging", "demo"} and not settings.allow_production_resources_in_non_production:
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

_dial_in_cache: dict | None = None


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
