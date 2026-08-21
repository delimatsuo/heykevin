"""Post-call address validation via the Google Geocoding API.

Two live calls produced "175 Fox Run Road" and "175 Foxburn Road" for the
same job — a contractor can drive to a street that does not exist. Extracted
addresses are geocoded after the call and flagged when they do not resolve.
Never during the call: voice_pipeline deliberately forbids address read-back.

The trap, measured live 2026-08-20: a nonsense street returns ``status: OK``
with the town centroid (``partial_match: true``, ``location_type:
APPROXIMATE``, formatted address without a street number). ``status`` is not
a validity signal — resolution is gated on location_type and partial_match.

Validation is strictly best-effort: no API key, a blank address, or any
fetch failure returns None and must never block the post-call path.
"""

from __future__ import annotations

import httpx

from app.utils.logging import get_logger

logger = get_logger(__name__)

_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# location_types that pin an address to a deliverable street position.
# APPROXIMATE is the town-centroid trap; GEOMETRIC_CENTER is a street or
# place centroid — neither proves the house number exists.
_RESOLVED_LOCATION_TYPES = {"ROOFTOP", "RANGE_INTERPOLATED"}


def interpret_geocode_response(payload: dict) -> dict:
    """Pure interpretation of a Geocoding API response body."""
    results = payload.get("results") or []
    if payload.get("status") != "OK" or not results:
        return {
            "resolved": False,
            "formatted_address": "",
            "location_type": "",
            "partial_match": False,
        }
    top = results[0]
    location_type = str((top.get("geometry") or {}).get("location_type") or "")
    partial = bool(top.get("partial_match"))
    return {
        "resolved": location_type in _RESOLVED_LOCATION_TYPES and not partial,
        "formatted_address": str(top.get("formatted_address") or ""),
        "location_type": location_type,
        "partial_match": partial,
    }


async def _fetch_geocode(address: str, api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(
            _GEOCODE_URL,
            params={"address": address, "key": api_key},
        )
        response.raise_for_status()
        return response.json()


async def validate_address(address: str, api_key: str) -> dict | None:
    """Best-effort validation. None means "could not validate" — callers must
    treat that as no signal, never as invalid."""
    address = (address or "").strip()
    if not api_key or not address:
        return None
    try:
        payload = await _fetch_geocode(address, api_key)
    except Exception as error:
        # Never log the address itself (caller PII).
        logger.warning(f"Address validation fetch failed (non-blocking): {error}")
        return None
    outcome = interpret_geocode_response(payload)
    logger.info(
        "address_validation",
        extra={
            "resolved": outcome["resolved"],
            "location_type": outcome["location_type"],
            "partial_match": outcome["partial_match"],
        },
    )
    return outcome
