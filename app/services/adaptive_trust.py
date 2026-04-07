"""Adaptive trust — adjust contact trust scores based on call outcomes.

Score changes are capped at +/- 5 per call to prevent gaming.
"""

from app.db.contacts import get_contact, upsert_contact
from app.utils.logging import get_logger

logger = get_logger(__name__)

MAX_DELTA = 5

# Outcome → trust delta
OUTCOME_DELTAS = {
    "picked_up": +5,
    "callback": +3,
    "texted": +1,
    "text_replied": +1,
    "voicemail": 0,
    "ignored": -3,
    "blocked": -5,
}


async def adjust_trust_after_call(caller_phone: str, outcome: str):
    """Adjust a contact's trust level based on call outcome."""
    delta = OUTCOME_DELTAS.get(outcome, 0)
    if delta == 0:
        return

    contact = await get_contact(caller_phone)

    if contact:
        current_trust = contact.get("trust_level", 50)
        # Count outcome occurrences
        key = f"times_{outcome}" if outcome in ("picked_up", "ignored") else None
        updates = {
            "trust_level": max(0, min(100, current_trust + delta)),
        }
        if key:
            updates[key] = contact.get(key, 0) + 1
        await upsert_contact(caller_phone, updates)
        logger.info(
            f"Trust adjusted: {current_trust} → {updates['trust_level']} ({outcome})",
            extra={"caller_phone": caller_phone},
        )
    else:
        # First time seeing this number — create contact with adjusted trust
        new_trust = max(0, min(100, 50 + delta))
        await upsert_contact(caller_phone, {
            "trust_level": new_trust,
            "name": "",
        })
        logger.info(
            f"New contact created with trust {new_trust} ({outcome})",
            extra={"caller_phone": caller_phone},
        )
