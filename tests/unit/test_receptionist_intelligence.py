"""Receptionist prompt and post-call scope guardrails."""

from app.services.gemini_pipeline import GeminiPipeline
from app.services.job_card import _build_extraction_prompt
from app.services.vcard import generate_vcard
from app.services.voice_pipeline import build_system_prompt, VoicePipeline


def _plumbing_config() -> dict:
    return {
        "owner_name": "Deli Matsuo",
        "business_name": "Matsuo Plumbing",
        "mode": "business",
        "effective_mode": "business",
        "knowledge": (
            "## Services\n"
            "- Water heater services\n"
            "- Faucet replacement\n"
            "- Water filter replacement\n"
            "- Dishwasher installation"
        ),
        "services": [
            {"name": "House call", "price_min": 100, "price_max": 100},
            {"name": "Faucet replacement", "price_min": 150, "price_max": 250},
        ],
    }


def test_business_prompt_rejects_out_of_scope_trade_work():
    prompt = build_system_prompt(_plumbing_config())

    assert "BUSINESS PROFILE AND SERVICE SCOPE" in prompt
    assert "Listed services: House call, Faucet replacement" in prompt
    assert "If it is OUT OF SCOPE" in prompt
    assert "do not ask trade-specific diagnostic questions for a different trade" in prompt
    assert "Do not say \"Sure, I can help with that\"" in prompt
    assert "electrical panel" in prompt


def test_business_prompt_instructs_media_followup_without_live_review_claim():
    prompt = build_system_prompt(_plumbing_config())

    assert "upload a photo or short video" in prompt
    assert "Do not claim you can review media live during the phone call" in prompt


def test_business_prompt_prevents_immediate_close_after_availability_check():
    prompt = build_system_prompt(_plumbing_config())

    assert "Never say \"I'll pass this along\" immediately after" in prompt
    assert "First wait for the availability result" in prompt


def test_job_card_extraction_prompt_can_classify_out_of_scope_requests():
    prompt = _build_extraction_prompt(
        "Caller: Can you help with my electric panel?\nKevin: Matsuo Plumbing may not be the right company.",
        _plumbing_config(),
    )

    assert '"out_of_scope"' in prompt
    assert "electrical panel or breaker request is out_of_scope" in prompt
    assert "Only use service_request when the caller's request appears related" in prompt
    assert "Water heater services" in prompt


def test_electrical_panel_terms_trigger_urgency_escalation():
    assert "electric panel" in VoicePipeline.URGENCY_KEYWORDS
    assert "breaker tripped" in VoicePipeline.URGENCY_KEYWORDS
    assert "electric panel" in GeminiPipeline.URGENCY_KEYWORDS
    assert "breaker tripped" in GeminiPipeline.URGENCY_KEYWORDS


def test_vcard_ignores_generic_or_wrong_service_type_labels():
    vcard = generate_vcard({
        "owner_name": "Deli Matsuo",
        "business_name": "Matsuo Plumbing",
        "twilio_number": "+15555550123",
        "service_type": "personal",
    })

    assert "FN:Deli Matsuo\r\n" in vcard
    assert "personal" not in vcard
