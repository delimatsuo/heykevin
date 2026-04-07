"""Call routing decisions based on trust score."""

from enum import Enum

from app.utils.logging import get_logger

logger = get_logger(__name__)


class Route(str, Enum):
    WHITELIST_FORWARD = "whitelist_forward"   # Score 90-100: forward directly
    RING_THEN_SCREEN = "ring_then_screen"     # Score 70-89: ring user, then Kevin
    AI_SCREENING = "ai_screening"             # Score 30-69: Kevin answers immediately
    SPAM_BLOCK = "spam_block"                 # Score 0-29: block or silent voicemail


def determine_route(trust_score: int) -> Route:
    """Determine call routing based on trust score."""
    if trust_score >= 90:
        route = Route.WHITELIST_FORWARD
    elif trust_score >= 70:
        route = Route.RING_THEN_SCREEN
    elif trust_score >= 30:
        route = Route.AI_SCREENING
    else:
        route = Route.SPAM_BLOCK

    logger.info(f"Route determined: {route.value}", extra={"trust_score": trust_score, "route": route.value})
    return route
