"""Market availability discovery API."""

from fastapi import APIRouter
from app.services.country_policy import get_markets_payload

router = APIRouter(prefix="/api/markets", tags=["markets"])


@router.get("")
async def get_markets():
    """Return static market availability and qualification order for Hey Kevin."""
    return get_markets_payload()
