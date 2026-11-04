"""Subscription entitlement helpers."""

from __future__ import annotations

import time


BUSINESS_TIERS = frozenset({"business", "businessPro"})


def has_active_subscription(contractor: dict | None) -> bool:
    """Return whether the account has a usable subscription/trial window."""
    if not contractor:
        return False

    status = contractor.get("subscription_status", "")
    if status in ("trial", "active"):
        return True

    try:
        expires = float(contractor.get("subscription_expires") or 0)
    except (TypeError, ValueError):
        expires = 0
    return status == "expired" and expires > time.time()


def has_business_entitlement(contractor: dict | None) -> bool:
    """Business mode requires both an active account window and a business tier."""
    if not contractor:
        return False
    return has_active_subscription(contractor) and contractor.get("subscription_tier") in BUSINESS_TIERS


def has_business_pro_entitlement(contractor: dict | None) -> bool:
    if not contractor:
        return False
    return has_active_subscription(contractor) and contractor.get("subscription_tier") == "businessPro"


def _connected_before_cutoff(contractor: dict, provider: str, cutoff: float) -> bool:
    for field in (f"{provider}_first_connected_at", f"{provider}_connected_at"):
        value = contractor.get(field)
        if type(value) in (int, float) and 0 < value < cutoff:
            return True
    return False


def can_use_integration(contractor: dict | None, provider: str) -> bool:
    """Jobber and Google Calendar need Business Pro once the cutoff is set.

    Business accounts already connected before the cutoff keep that connection,
    including through a reauthorization, until they disconnect it. A config
    without subscription fields is not an entitlement answer, so it stays allowed.
    """
    from app.config import settings

    cutoff = settings.pro_integrations_cutoff
    if not cutoff or not contractor or "subscription_tier" not in contractor:
        return True
    if has_business_pro_entitlement(contractor):
        return True
    return has_business_entitlement(contractor) and _connected_before_cutoff(contractor, provider, cutoff)


def has_entitled_token(
    contractor: dict | None,
    provider: str,
    token_kind: str = "access",
    *,
    contractor_id: str | None = None,
) -> bool:
    """Return True if the account may use this integration and holds a usable token."""
    from app.services.integration_tokens import has_usable_token

    return can_use_integration(contractor, provider) and has_usable_token(
        contractor, provider, token_kind, contractor_id=contractor_id
    )


def effective_mode(contractor: dict | None) -> str:
    """Return the runtime mode allowed by the account's entitlement."""
    if not contractor:
        return "business"

    requested = contractor.get("mode", "personal")
    if requested in ("business", "businessPro") and not has_business_entitlement(contractor):
        return "personal"
    return "personal" if requested == "personal" else "business"


def with_entitlement_flags(contractor: dict) -> dict:
    """Copy contractor data and add read-only entitlement/effective-mode fields."""
    data = dict(contractor)
    data["business_entitlement_active"] = has_business_entitlement(contractor)
    data["business_pro_entitlement_active"] = has_business_pro_entitlement(contractor)
    data["effective_mode"] = effective_mode(contractor)
    data["jobber_available"] = can_use_integration(contractor, "jobber")
    data["google_calendar_available"] = can_use_integration(contractor, "google_calendar")
    return data
