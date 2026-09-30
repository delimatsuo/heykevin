"""Acquisition and activation measurement API router."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictStr, field_validator

from app.middleware.auth import verify_api_token
from app.services.acquisition import (
    check_contractor_acquisition_eligibility,
    process_apple_ads_attribution,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/acquisition", dependencies=[Depends(verify_api_token)])


class AppleAdsAttributionRequest(BaseModel):
    token: StrictStr = Field(min_length=1, max_length=8192)
    model_config = {"extra": "forbid"}

    @field_validator("token")
    @classmethod
    def _validate_token_bytes(cls, v: str) -> str:
        raw_bytes = v.encode("utf-8")
        if not (1 <= len(raw_bytes) <= 8192):
            raise ValueError("Token must be between 1 and 8192 UTF-8 bytes")
        return v


@router.get("/status")
async def get_acquisition_status(request: Request):
    """Return whether the authenticated contractor is eligible for attribution collection."""
    if getattr(request.state, "is_admin", False):
        return {"eligible": False}

    contractor_id = getattr(request.state, "contractor_id", None)
    if not contractor_id or not isinstance(contractor_id, str):
        return {"eligible": False}

    eligible = await check_contractor_acquisition_eligibility(contractor_id)
    return {"eligible": eligible}


@router.post("/apple-ads")
async def record_apple_ads_attribution(body: AppleAdsAttributionRequest, request: Request):
    """Record Apple Ads attribution token for an eligible contractor.

    Fails closed for admin callers. Returns structured status without identity
    or ad campaign metadata.
    """
    if getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin token cannot bind device attribution")

    contractor_id = getattr(request.state, "contractor_id", None)
    if not contractor_id or not isinstance(contractor_id, str):
        raise HTTPException(status_code=401, detail="Authentication required")

    result = await process_apple_ads_attribution(contractor_id, body.token)
    return result
