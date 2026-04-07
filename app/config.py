"""Application configuration. Loads from .env locally, Secret Manager in production."""

import os

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str

    # Vapi (deprecated — kept for backward compat)
    vapi_api_key: str = ""
    vapi_public_key: str = ""
    vapi_phone_number_id: str = ""

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

    # Dial-in number (for Pick Up — user calls this to join conference)
    dial_in_number: str = "+16504222696"

    # Cloud Run URL (for WebSocket URL generation)
    cloud_run_url: str = "https://kevin-api-752910912062.us-central1.run.app"

    # App
    environment: str = "development"
    log_level: str = "INFO"
    port: int = 8080

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def get_settings() -> Settings:
    return Settings()


settings = get_settings()
