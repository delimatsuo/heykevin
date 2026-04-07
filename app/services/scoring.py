"""Trust score engine with breakdown logging.

Score ranges:
  90-100: Whitelist/VIP — forward directly
  70-89:  Likely known — ring user, then screen
  30-69:  Unknown — Kevin screens immediately
  0-29:   Likely spam — block or silent voicemail
"""

from app.utils.logging import get_logger

logger = get_logger(__name__)

# Max trust score change per call for adaptive trust (prevents gaming)
MAX_SCORE_DELTA = 5


def calculate_trust_score(phone: str, lookups: dict) -> tuple[int, dict]:
    """Calculate trust score (0-100) with breakdown.

    Returns (score, breakdown) where breakdown shows which signals contributed what.
    """
    breakdown = {}
    score = 50  # baseline for unknown

    contact = lookups.get("contact")
    history = lookups.get("history", {})
    twilio = lookups.get("twilio", {})
    nomorobo = lookups.get("nomorobo", {})

    # Whitelist/blacklist — immediate decision
    if contact:
        if contact.get("is_whitelisted"):
            breakdown["whitelist"] = 100
            return 100, breakdown
        if contact.get("is_blacklisted"):
            breakdown["blacklist"] = 0
            return 0, breakdown

        # Known contact with trust level
        trust = contact.get("trust_level")
        if trust is not None:
            score = trust
            breakdown["contact_trust"] = trust - 50  # delta from baseline

    # Call history signals
    if history:
        picked_up = history.get("times_picked_up", 0)
        ignored = history.get("times_ignored", 0)

        if picked_up > 2:
            delta = min(30, picked_up * 10)
            score += delta
            breakdown["history_picked_up"] = delta

        if ignored > 3:
            delta = min(20, ignored * 5)
            score -= delta
            breakdown["history_ignored"] = -delta

    # Spam signals
    spam_score = nomorobo.get("spam_score", 0)
    if spam_score and spam_score > 0.7:
        delta = -40
        score += delta
        breakdown["nomorobo_spam"] = delta
    elif spam_score and spam_score > 0.3:
        delta = -15
        score += delta
        breakdown["nomorobo_suspicious"] = delta

    # Line type signals
    line_type = twilio.get("line_type", "")
    if line_type == "voip":
        score -= 10
        breakdown["line_type_voip"] = -10
    elif line_type == "landline":
        score += 5
        breakdown["line_type_landline"] = 5

    # Carrier name (having one is a positive signal)
    if twilio.get("carrier"):
        score += 5
        breakdown["has_carrier"] = 5

    # Clamp to 0-100
    final_score = max(0, min(100, score))
    breakdown["final"] = final_score

    logger.info(
        f"Trust score computed: {final_score}",
        extra={"trust_score": final_score, "caller_phone": phone},
    )

    return final_score, breakdown
