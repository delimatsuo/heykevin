"""Parallel number lookups with 3-second per-lookup timeout."""

import asyncio
from typing import Optional

from twilio.rest import Client

from app.config import settings
from app.db.contacts import get_contact
from app.db.calls import get_call_history
from app.utils.logging import get_logger

logger = get_logger(__name__)

LOOKUP_TIMEOUT = 3.0  # seconds per lookup


async def _lookup_twilio(phone: str) -> dict:
    """Twilio Lookup API — carrier and line type."""
    try:
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        # Run synchronous Twilio API call in executor to avoid blocking
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: client.lookups.v2.phone_numbers(phone).fetch(
                    fields="line_type_intelligence"
                ),
            ),
            timeout=LOOKUP_TIMEOUT,
        )
        line_type_info = getattr(result, "line_type_intelligence", {}) or {}
        return {
            "carrier": line_type_info.get("carrier_name", ""),
            "line_type": line_type_info.get("type", ""),
        }
    except asyncio.TimeoutError:
        logger.warning("Twilio lookup timed out")
        return {}
    except Exception as e:
        logger.warning(f"Twilio lookup failed: {e}")
        return {}


async def _lookup_nomorobo(phone: str, twilio_addon_data: Optional[dict] = None) -> dict:
    """Nomorobo spam score — extracted from Twilio add-on data in webhook payload."""
    # Nomorobo data comes from Twilio add-on in the webhook payload, not a separate API call.
    # If available, it's passed in via twilio_addon_data.
    if twilio_addon_data:
        try:
            nomorobo = twilio_addon_data.get("nomorobo_spamscore", {})
            result = nomorobo.get("result", {})
            score = result.get("score", 0)
            return {"spam_score": score}
        except Exception as e:
            logger.warning(f"Nomorobo parse failed: {e}")
    return {"spam_score": 0}


async def _lookup_contact(phone: str) -> Optional[dict]:
    """Local contact database lookup."""
    try:
        return await asyncio.wait_for(get_contact(phone), timeout=LOOKUP_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Contact lookup timed out")
        return None
    except Exception as e:
        logger.warning(f"Contact lookup failed: {e}")
        return None


async def _lookup_history(phone: str) -> dict:
    """Call history lookup — how many times picked up, ignored, etc."""
    try:
        calls = await asyncio.wait_for(get_call_history(phone, limit=20), timeout=LOOKUP_TIMEOUT)
        if not calls:
            return {}
        picked_up = sum(1 for c in calls if c.get("outcome") == "picked_up")
        ignored = sum(1 for c in calls if c.get("outcome") == "ignored")
        blocked = sum(1 for c in calls if c.get("outcome") == "blocked")
        return {
            "total_calls": len(calls),
            "times_picked_up": picked_up,
            "times_ignored": ignored,
            "times_blocked": blocked,
        }
    except asyncio.TimeoutError:
        logger.warning("History lookup timed out")
        return {}
    except Exception as e:
        logger.warning(f"History lookup failed: {e}")
        return {}


async def run_lookups(phone: str, twilio_addon_data: Optional[dict] = None) -> dict:
    """Run all lookups in parallel. Returns partial results if some fail."""
    results = await asyncio.gather(
        _lookup_twilio(phone),
        _lookup_nomorobo(phone, twilio_addon_data),
        _lookup_contact(phone),
        _lookup_history(phone),
        return_exceptions=True,
    )

    twilio_data = results[0] if isinstance(results[0], dict) else {}
    nomorobo_data = results[1] if isinstance(results[1], dict) else {}
    contact = results[2] if isinstance(results[2], dict) else None
    history = results[3] if isinstance(results[3], dict) else {}

    return {
        "twilio": twilio_data,
        "nomorobo": nomorobo_data,
        "contact": contact,
        "history": history,
    }
