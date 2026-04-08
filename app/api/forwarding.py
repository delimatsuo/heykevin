"""Per-country call forwarding instructions served to the iOS app during onboarding."""

from fastapi import APIRouter, Depends, Query

from app.middleware.auth import verify_api_token

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
