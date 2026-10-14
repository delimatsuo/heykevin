"""Unit tests for skippable Business Onboarding Services & Price List flow."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.api import contractors as contractors_api  # noqa: E402
from app.services.voice_pipeline import (  # noqa: E402
    _format_service_names_for_prompt,
    _format_services_for_prompt,
    build_system_prompt,
)


def _fake_owner_request(contractor_id: str):
    return SimpleNamespace(
        state=SimpleNamespace(contractor_id=contractor_id, is_admin=False),
    )


def test_parse_services_and_prices_text_supports_csv_markdown_and_unpriced_lines():
    raw = """
    Service,Min,Max
    Drain Cleaning,$95,$185
    - Water Heater Flush: $140 - $220
    * Slab Leak Detection | $250
    Emergency Shutoff Assistance
    Sewer Camera Inspection — $199
    """
    parsed = contractors_api.parse_services_and_prices_text(raw)
    assert parsed == [
        {"name": "Drain Cleaning", "price_min": 95, "price_max": 185},
        {"name": "Water Heater Flush", "price_min": 140, "price_max": 220},
        {"name": "Slab Leak Detection", "price_min": 250, "price_max": 250},
        {"name": "Emergency Shutoff Assistance", "price_min": 0, "price_max": 0},
        {"name": "Sewer Camera Inspection", "price_min": 199, "price_max": 199},
    ]


def test_parse_services_and_prices_text_deduplicates_and_swaps_inverted_range():
    raw = """
    Panel Upgrade: $3200 - $1800
    panel upgrade: $2000
    """
    parsed = contractors_api.parse_services_and_prices_text(raw)
    assert parsed == [
        {"name": "Panel Upgrade", "price_min": 1800, "price_max": 3200},
    ]


def test_format_services_knowledge_markdown_includes_priced_unpriced_and_notes():
    services = [
        {"name": "Drain Cleaning", "price_min": 95, "price_max": 185},
        {"name": "Diagnostic Visit", "price_min": 89, "price_max": 89},
        {"name": "Custom Remodel Repipe", "price_min": 0, "price_max": 0},
    ]
    md = contractors_api.format_services_knowledge_markdown(
        services,
        notes="$89 diagnostic waived with repair.",
    )
    assert "## Services" in md
    assert "- Drain Cleaning ($95-$185)" in md
    assert "- Diagnostic Visit ($89)" in md
    assert "- Custom Remodel Repipe" in md
    assert "Custom Remodel Repipe ($0" not in md
    assert "## Pricing & Service Notes\n$89 diagnostic waived with repair." in md


def test_voice_pipeline_unpriced_services_do_not_emit_zero_dollar_pricing():
    services = [
        {"name": "Emergency Leak Repair", "price_min": 0, "price_max": 0},
        {"name": "Drain Cleaning", "price_min": 120, "price_max": 200},
    ]
    assert _format_service_names_for_prompt(services) == "Emergency Leak Repair, Drain Cleaning"
    pricing_block = _format_services_for_prompt(services)
    assert "- Drain Cleaning: $120-$200" in pricing_block
    assert "Emergency Leak Repair" not in pricing_block
    assert "$0" not in pricing_block


def test_build_system_prompt_with_only_unpriced_services_omits_service_pricing_section():
    contractor = {
        "owner_name": "Mike",
        "business_name": "Mike's Plumbing",
        "service_type": "plumbing",
        "mode": "business",
        "effective_mode": "business",
        "services": [
            {"name": "Water Heater Replacement", "price_min": 0, "price_max": 0},
            {"name": "Leak Detection", "price_min": 0, "price_max": 0},
        ],
    }
    prompt = build_system_prompt(config=contractor)
    assert "Listed services: Water Heater Replacement, Leak Detection" in prompt
    assert "SERVICE PRICING:" not in prompt
    assert ": $0" not in prompt


@pytest.mark.asyncio
async def test_patch_contractor_accepts_services_and_knowledge(monkeypatch):
    captured = {}

    async def fake_get_contractor(cid):
        return {"id": cid, "mode": "business"}

    async def fake_update_contractor(cid, updates):
        captured["cid"] = cid
        captured["updates"] = updates
        return True

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)

    body = contractors_api.ContractorUpdate(
        services=[
            contractors_api.ServiceItem(name="  Drain Cleaning  ", price_min=95, price_max=185),
            contractors_api.ServiceItem(name="Free Estimate", price_min=0, price_max=0),
        ],
        knowledge="## Services\n- Drain Cleaning ($95-$185)",
    )
    res = await contractors_api.api_update_contractor(
        "contractor-123",
        body,
        _fake_owner_request("contractor-123"),
    )
    assert res == {"status": "ok"}
    assert captured["cid"] == "contractor-123"
    assert captured["updates"]["services"] == [
        {"name": "Drain Cleaning", "price_min": 95, "price_max": 185},
        {"name": "Free Estimate", "price_min": 0, "price_max": 0},
    ]
    assert "Drain Cleaning" in captured["updates"]["knowledge"]


def test_parse_services_preserves_leading_digits_and_handles_quoted_csv_commas():
    raw = """
    1. Drain Cleaning: $95 - $185
    2) Leak Detection: $150
    24/7 Emergency Service: $250
    3D Remodel Rendering
    Water Heater Replacement,"$1,200","$1,800"
    """
    parsed = contractors_api.parse_services_and_prices_text(raw)
    assert parsed == [
        {"name": "Drain Cleaning", "price_min": 95, "price_max": 185},
        {"name": "Leak Detection", "price_min": 150, "price_max": 150},
        {"name": "24/7 Emergency Service", "price_min": 250, "price_max": 250},
        {"name": "3D Remodel Rendering", "price_min": 0, "price_max": 0},
        {"name": "Water Heater Replacement", "price_min": 1200, "price_max": 1800},
    ]


def test_voice_pipeline_pricing_cap_applied_after_filtering_unpriced_services():
    services = [
        {"name": f"Unpriced Service {i}", "price_min": 0, "price_max": 0}
        for i in range(25)
    ] + [
        {"name": "Main Line Hydro Jetting", "price_min": 350, "price_max": 600},
    ]
    pricing_block = _format_services_for_prompt(services)
    assert "- Main Line Hydro Jetting: $350-$600" in pricing_block


@pytest.mark.asyncio
async def test_api_parse_price_list_returns_services_and_optionally_persists(monkeypatch):
    updated_calls = []

    async def fake_get_contractor(cid):
        return {"id": cid, "knowledge": "## Business Hours\nMon-Fri 8am-5pm"}

    async def fake_update_contractor(cid, updates):
        updated_calls.append((cid, updates))
        return True

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)

    body = contractors_api.ParsePriceListRequest(
        raw_text="Water Heater Install: $1200 - $1800\nGeneral Inspection",
        notes="Licensed and insured.",
        save=True,
    )
    res = await contractors_api.api_parse_price_list(
        "contractor-123",
        body,
        _fake_owner_request("contractor-123"),
    )
    assert res["status"] == "ok"
    assert res["count"] == 2
    assert len(updated_calls) == 1
    saved_services = updated_calls[0][1]["services"]
    assert saved_services == [
        {"name": "Water Heater Install", "price_min": 1200, "price_max": 1800},
        {"name": "General Inspection", "price_min": 0, "price_max": 0},
    ]
    assert "## Business Hours\nMon-Fri 8am-5pm" in updated_calls[0][1]["knowledge"]
    assert "Licensed and insured." in updated_calls[0][1]["knowledge"]
