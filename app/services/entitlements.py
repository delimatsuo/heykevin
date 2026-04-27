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
    return data
