"""App version-gate endpoint.

Public, unauthenticated. Called by the iOS client on cold launch to decide
whether to (a) block the user with a forced update screen, (b) prompt them
with an optional "update available" sheet, or (c) proceed normally.

Configured via environment variables so values can be tuned at runtime via
`gcloud run services update kevin-api --update-env-vars=...` without a
code deploy.

  IOS_MIN_VERSION       — versions below this are force-blocked. Default "1.0.0".
  IOS_LATEST_VERSION    — current App Store release. Default mirrors min_version.
  IOS_FORCE_UPDATE      — "true"/"false". Hard switch to force everyone below
                          latest_version onto the latest, not just below
                          min_version. Default false.
  IOS_UPDATE_MESSAGE    — optional human-readable message shown on the gate.
  IOS_APP_STORE_URL     — link the client opens when the user taps "Update".
"""

import os
from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/app", tags=["app"])


class VersionResponse(BaseModel):
    platform: str
    min_version: str
    latest_version: str
    force_update: bool
    update_message: Optional[str]
    store_url: str


_DEFAULT_APP_STORE_URL = "https://apps.apple.com/app/id6761427495"


@router.get("/version", response_model=VersionResponse)
async def get_version_info(platform: str = "ios") -> VersionResponse:
    """Return version-gate config for the requesting platform.

    Currently only iOS is supported; the `platform` query param is reserved
    for future Android / web clients without breaking the response shape.
    """
    min_version = os.environ.get("IOS_MIN_VERSION", "1.0.0").strip()
    latest_version = os.environ.get("IOS_LATEST_VERSION", min_version).strip()
    force = os.environ.get("IOS_FORCE_UPDATE", "false").strip().lower() in ("1", "true", "yes")
    message = os.environ.get("IOS_UPDATE_MESSAGE", "").strip() or None
    store_url = os.environ.get("IOS_APP_STORE_URL", _DEFAULT_APP_STORE_URL).strip() or _DEFAULT_APP_STORE_URL

    return VersionResponse(
        platform=platform,
        min_version=min_version,
        latest_version=latest_version,
        force_update=force,
        update_message=message,
        store_url=store_url,
    )
