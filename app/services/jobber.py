"""Jobber GraphQL API client for FSM integration."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import httpx
from typing import Optional
from app.utils.logging import get_logger

logger = get_logger(__name__)

JOBBER_GRAPHQL_URL = "https://api.getjobber.com/api/graphql"
JOBBER_TOKEN_URL = "https://api.getjobber.com/api/oauth/token"
_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


class JobberAuthError(Exception):
    """Raised when Jobber rejects the current access token."""


def _token_expires_soon(access_token: str, leeway_seconds: int = 120) -> bool:
    """Return True when a Jobber JWT is expired or close to expiring."""
    try:
        payload_segment = access_token.split(".")[1]
        payload_segment += "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment.encode()).decode())
        exp = payload.get("exp")
        return isinstance(exp, (int, float)) and exp <= time.time() + leeway_seconds
    except Exception:
        return False


async def _write_jobber_tokens(contractor_id: str, updates: dict):
    """Persist refreshed Jobber tokens on the contractor document."""
    if not contractor_id:
        return
    from app.db.firestore_client import get_firestore_client

    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: db.collection("contractors").document(contractor_id).update(updates),
    )


async def _read_jobber_tokens(contractor_id: str) -> dict:
    """Read latest stored Jobber tokens to avoid reusing rotated refresh tokens."""
    if not contractor_id:
        return {}
    from app.db.firestore_client import get_firestore_client

    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc = await loop.run_in_executor(
        None,
        lambda: db.collection("contractors").document(contractor_id).get(),
    )
    if not doc.exists:
        return {}
    data = doc.to_dict() or {}
    return {
        "jobber_access_token": data.get("jobber_access_token", ""),
        "jobber_refresh_token": data.get("jobber_refresh_token", ""),
    }


async def refresh_access_token(contractor: dict, *, force: bool = False) -> Optional[str]:
    """Refresh and persist Jobber OAuth tokens for a contractor."""
    from app.config import settings

    contractor_id = contractor.get("contractor_id", "")
    lock_key = contractor_id or contractor.get("jobber_refresh_token", "")
    lock = _REFRESH_LOCKS.setdefault(lock_key, asyncio.Lock())

    async with lock:
        stale_token = contractor.get("jobber_access_token", "")
        latest = await _read_jobber_tokens(contractor_id)
        if latest:
            contractor.update({k: v for k, v in latest.items() if v})

        current_token = contractor.get("jobber_access_token", "")
        if current_token and current_token != stale_token and not _token_expires_soon(current_token):
            return current_token
        if current_token and not force and not _token_expires_soon(current_token):
            return current_token

        refresh_token = contractor.get("jobber_refresh_token", "")
        if not refresh_token or not settings.jobber_client_id:
            return None

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    JOBBER_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": settings.jobber_client_id,
                        "client_secret": settings.jobber_client_secret,
                    },
                    timeout=10.0,
                )
            if response.status_code != 200:
                logger.error(f"Jobber token refresh failed: {response.status_code} {response.text[:200]}")
                return None

            tokens = response.json()
            access_token = tokens.get("access_token", "")
            new_refresh_token = tokens.get("refresh_token", refresh_token)
            if not access_token:
                logger.error("Jobber token refresh returned no access token")
                return None

            updates = {
                "jobber_access_token": access_token,
                "jobber_refresh_token": new_refresh_token,
                "jobber_token_refreshed_at": time.time(),
            }
            if tokens.get("expires_at"):
                updates["jobber_token_expires_at"] = tokens["expires_at"]

            contractor.update(updates)
            try:
                await _write_jobber_tokens(contractor_id, updates)
            except Exception as e:
                logger.error(f"Jobber token persistence failed after refresh: {e}")
            logger.info(f"Jobber token refreshed for contractor {contractor_id[:8] or 'unknown'}")
            return access_token
        except Exception as e:
            logger.error(f"Jobber token refresh error: {e}")
            return None


async def _resolve_access_token(auth: str | dict) -> str:
    if isinstance(auth, dict):
        access_token = auth.get("jobber_access_token", "")
        if access_token and _token_expires_soon(access_token):
            refreshed = await refresh_access_token(auth)
            if refreshed:
                return refreshed
        return access_token
    return auth


async def _graphql_request(access_token: str, query: str, variables: dict = None) -> Optional[dict]:
    """Execute a Jobber GraphQL request."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                JOBBER_GRAPHQL_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": variables or {}},
                timeout=5.0,
            )
            if response.status_code == 200:
                data = response.json()
                if "errors" in data:
                    logger.warning(f"Jobber GraphQL errors: {data['errors']}")
                return data.get("data")
            if response.status_code == 401:
                raise JobberAuthError("Jobber access token rejected")
            logger.error(f"Jobber API error: {response.status_code}")
    except JobberAuthError:
        raise
    except Exception as e:
        logger.error(f"Jobber request failed: {e}")
    return None


async def _graphql_request_with_refresh(auth: str | dict, query: str, variables: dict = None) -> Optional[dict]:
    """Execute a Jobber request, refreshing contractor tokens once on 401."""
    access_token = await _resolve_access_token(auth)
    if not access_token:
        return None

    try:
        return await _graphql_request(access_token, query, variables)
    except JobberAuthError:
        if not isinstance(auth, dict):
            logger.error("Jobber API error: 401")
            return None

    refreshed = await refresh_access_token(auth, force=True)
    if not refreshed:
        logger.error("Jobber API error: 401")
        return None

    try:
        return await _graphql_request(refreshed, query, variables)
    except JobberAuthError:
        logger.error("Jobber API error: 401")
        return None


async def lookup_customer(auth: str | dict, phone: str) -> Optional[dict]:
    """Look up a Jobber customer by phone number."""
    query = """
    query LookupClient($phone: String!) {
        clients(filter: {phone: $phone}, first: 1) {
            nodes {
                id
                name
                firstName
                lastName
                phones { number }
                emails { address }
                billingAddress { street city province postalCode }
            }
        }
    }
    """
    data = await _graphql_request_with_refresh(auth, query, {"phone": phone})
    if data and data.get("clients", {}).get("nodes"):
        return data["clients"]["nodes"][0]
    return None


async def get_available_slots(auth: str | dict, days_ahead: int = 7) -> list[dict]:
    """Get available appointment slots from the contractor's Jobber schedule."""
    from datetime import datetime, timedelta
    start = datetime.utcnow().isoformat() + "Z"
    end = (datetime.utcnow() + timedelta(days=days_ahead)).isoformat() + "Z"

    query = """
    query GetVisits($startDate: ISO8601DateTime!, $endDate: ISO8601DateTime!) {
        calendarEvents(filter: {startAt: {gte: $startDate}, endAt: {lte: $endDate}}) {
            nodes {
                ... on Visit {
                    id
                    title
                    startAt
                    endAt
                }
            }
        }
    }
    """
    data = await _graphql_request_with_refresh(auth, query, {"startDate": start, "endDate": end})
    if data:
        return data.get("calendarEvents", {}).get("nodes", [])
    return []


async def create_job(auth: str | dict, job_data: dict) -> Optional[str]:
    """Create a new job in Jobber. Returns the job ID."""
    query = """
    mutation CreateJob($input: JobCreateInput!) {
        jobCreate(input: $input) {
            job { id title }
            userErrors { message path }
        }
    }
    """
    input_data = {
        "title": job_data.get("title", "Phone inquiry"),
        "instructions": job_data.get("instructions", ""),
    }
    # Attach to existing client if we have their Jobber ID
    if job_data.get("client_id"):
        input_data["clientId"] = job_data["client_id"]

    data = await _graphql_request_with_refresh(auth, query, {"input": input_data})
    if data and data.get("jobCreate", {}).get("job"):
        return data["jobCreate"]["job"]["id"]
    return None


async def create_quote(auth: str | dict, quote_data: dict) -> Optional[str]:
    """Create a quote in Jobber. Returns the quote ID."""
    query = """
    mutation CreateQuote($input: QuoteCreateInput!) {
        quoteCreate(input: $input) {
            quote { id quoteNumber }
            userErrors { message path }
        }
    }
    """
    data = await _graphql_request_with_refresh(auth, query, {"input": quote_data})
    if data and data.get("quoteCreate", {}).get("quote"):
        return data["quoteCreate"]["quote"]["id"]
    return None
