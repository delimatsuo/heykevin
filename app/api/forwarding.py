"""Per-country call forwarding instructions served to the iOS app during onboarding.

This module also exposes the dial-in PIN rate limiter used by the Twilio
voice webhook (F-15). The actual ``/webhooks/twilio/dial-in`` handler lives in
``app/webhooks/twilio_incoming.py`` and imports
:func:`check_dial_in_pin_attempt` and :func:`record_dial_in_pin_failure` from
here. Keeping the limiter helpers in this module — rather than the webhook
file — lets us unit-test them in isolation and keeps the persistent-store
contract (``app/db/rate_limits.py``) co-located with other dial-in surface
area.
"""

from fastapi import APIRouter, Depends, Query

from app.config import settings
from app.db.rate_limits import RateLimitResult, check_and_increment
from app.middleware.auth import verify_api_token
from app.utils.logging import get_logger, redact_phone

logger = get_logger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_token)])

# Standard GSM/carrier forwarding codes per country.
FORWARDING_CODES = {
    "US": {
        "forward_all": "*72{number}",
        "forward_unanswered": "*71{number}",
        "disable": "*73",
        "notes": "Works on all major US carriers (AT&T, Verizon, T-Mobile).",
        "recommended": "forward_unanswered",
    },
    "CA": {
        "forward_all": "*72{number}",
        "forward_unanswered": "*71{number}",
        "disable": "*73",
        "notes": "Works on all major Canadian carriers (Bell, Rogers, Telus).",
        "recommended": "forward_unanswered",
    },
    "BR": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on Vivo, Claro, TIM, Oi.",
        "recommended": "forward_unanswered",
    },
    "GB": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on EE, Vodafone, Three, O2.",
        "recommended": "forward_unanswered",
    },
    "DE": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on Telekom, Vodafone, O2.",
        "recommended": "forward_unanswered",
    },
    "FR": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on Orange, SFR, Bouygues, Free.",
        "recommended": "forward_unanswered",
    },
    "IT": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on TIM, Vodafone, WindTre, Iliad.",
        "recommended": "forward_unanswered",
    },
    "ES": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on Movistar, Vodafone, Orange.",
        "recommended": "forward_unanswered",
    },
    "PT": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on MEO, NOS, Vodafone.",
        "recommended": "forward_unanswered",
    },
}

FALLBACK_MESSAGE = (
    "If these codes don't work with your carrier, "
    "search for 'call forwarding' in your carrier's app or contact their support."
)


# ---------------------------------------------------------------------------
# F-15: dial-in PIN brute-force protection.
# ---------------------------------------------------------------------------

DIAL_IN_PIN_SCOPE = "dial_in_pin"


def _pin_rate_key(caller_phone: str, contractor_id: str = "") -> str:
    """Compose the rate-limit key for a dial-in attempt.

    PINs are global (any contractor's PIN can be tried from any caller line),
    so the most defensible scoping is ``caller_phone`` alone. When the caller
    later resolves to a specific contractor we additionally bump a per-tuple
    key so a single spoofed line can't burn one tenant's budget for everyone.
    """
    base = (caller_phone or "anon").lstrip("+") or "anon"
    if contractor_id:
        return f"{contractor_id}__{base}"
    return base


async def check_dial_in_pin_attempt(
    caller_phone: str,
    *,
    contractor_id: str = "",
) -> RateLimitResult:
    """Decide whether to allow another PIN attempt from this source.

    Wraps :func:`app.db.rate_limits.check_and_increment` with the configured
    PIN limit/window and the dial-in scope. Each call counts as one attempt.

    On exceed the underlying limiter returns ``allowed=False`` and a
    ``retry_after_seconds`` value covering the rest of the rolling window —
    so callers can simply hang up the leg with a generic "too many attempts"
    message without echoing remaining-budget information back to the caller.
    """
    limit = max(1, int(settings.pin_rate_limit))
    window = max(60, int(settings.pin_rate_window_seconds))
    key = _pin_rate_key(caller_phone, contractor_id)
    result = await check_and_increment(
        scope=DIAL_IN_PIN_SCOPE,
        key=key,
        limit=limit,
        window_seconds=window,
    )
    if not result.allowed:
        logger.warning(
            "dial_in_pin: rate-limited caller=%s contractor=%s retry_after=%ds",
            redact_phone(caller_phone),
            contractor_id or "-",
            result.retry_after_seconds,
        )
    return result


async def record_dial_in_pin_failure(
    caller_phone: str,
    *,
    contractor_id: str = "",
) -> None:
    """Optional: explicitly mark a failed PIN as a strike.

    ``check_dial_in_pin_attempt`` already burns one slot per call. Call this
    helper only when the existing handler has a separate code path for
    "attempt was made but didn't pre-check". It is a no-op when the limit
    has already been reached, so it is always safe to call.
    """
    await check_and_increment(
        scope=DIAL_IN_PIN_SCOPE,
        key=_pin_rate_key(caller_phone, contractor_id),
        limit=max(1, int(settings.pin_rate_limit)),
        window_seconds=max(60, int(settings.pin_rate_window_seconds)),
    )


@router.get("/forwarding-instructions")
async def get_forwarding_instructions(country_code: str = Query("US", max_length=2)):
    """Return call forwarding instructions for a country."""
    instructions = FORWARDING_CODES.get(country_code.upper())
    if not instructions:
        return {
            "supported": False,
            "country_code": country_code.upper(),
            "message": f"Call forwarding instructions not available for {country_code}. {FALLBACK_MESSAGE}",
        }
    return {
        "supported": True,
        "country_code": country_code.upper(),
        **instructions,
        "fallback_message": FALLBACK_MESSAGE,
    }
