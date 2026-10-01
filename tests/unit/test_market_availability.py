"""Unit tests for public market availability discovery (GET /api/markets)."""

import httpx
import pytest
from fastapi import FastAPI

from app.api import markets as markets_api
from app.services.country_policy import get_markets_payload, QUALIFICATION_ORDER


def _app_with_markets_router() -> FastAPI:
    app = FastAPI()
    app.include_router(markets_api.router)
    return app


@pytest.mark.asyncio
async def test_markets_endpoint_returns_exact_shape():
    """Proves GET /api/markets is unauthenticated and returns exact documented payload shape."""
    transport = httpx.ASGITransport(app=_app_with_markets_router())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/markets")

    assert resp.status_code == 200
    data = resp.json()

    assert "markets" in data
    assert "qualification_order" in data
    assert data["qualification_order"] == ["BR", "CA", "GB"]

    expected_markets = [
        {"country_code": "US", "status": "available"},
        {"country_code": "CA", "status": "available"},
        {"country_code": "BR", "status": "qualification_required"},
        {"country_code": "GB", "status": "qualification_required"},
        {"country_code": "DE", "status": "unsupported"},
        {"country_code": "FR", "status": "unsupported"},
        {"country_code": "IT", "status": "unsupported"},
        {"country_code": "ES", "status": "unsupported"},
        {"country_code": "PT", "status": "unsupported"},
    ]
    assert data["markets"] == expected_markets


def test_country_policy_get_markets_payload_matches_constant():
    payload = get_markets_payload()
    assert payload["qualification_order"] == list(QUALIFICATION_ORDER)
    market_map = {m["country_code"]: m["status"] for m in payload["markets"]}
    assert market_map["US"] == "available"
    assert market_map["CA"] == "available"
    assert market_map["BR"] == "qualification_required"
    assert market_map["GB"] == "qualification_required"
    assert market_map["DE"] == "unsupported"
    assert market_map["FR"] == "unsupported"
    assert market_map["IT"] == "unsupported"
    assert market_map["ES"] == "unsupported"
    assert market_map["PT"] == "unsupported"
