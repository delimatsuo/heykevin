"""Jobber GraphQL API client for FSM integration."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Optional

import httpx

from app.utils.logging import get_logger

logger = get_logger(__name__)

JOBBER_GRAPHQL_URL = "https://api.getjobber.com/api/graphql"
JOBBER_TOKEN_URL = "https://api.getjobber.com/api/oauth/token"
JOBBER_GRAPHQL_VERSION = "2025-04-16"
REFRESH_BUFFER_SECONDS = 120
_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


def _get_refresh_lock(contractor_id: str) -> asyncio.Lock:
    """Return a deterministic per-contractor asyncio.Lock from _REFRESH_LOCKS."""
    if hasattr(_REFRESH_LOCKS, "setdefault"):
        return _REFRESH_LOCKS.setdefault(contractor_id, asyncio.Lock())
    if contractor_id not in _REFRESH_LOCKS:
        _REFRESH_LOCKS[contractor_id] = asyncio.Lock()
    return _REFRESH_LOCKS[contractor_id]


class JobberAuthError(Exception):
    """Raised when Jobber rejects the current access token."""


def _token_expires_soon(access_token: str, leeway_seconds: int = 120) -> bool:
    """Return True when a Jobber JWT is expired or close to expiring."""
    try:
        payload_segment = access_token.split(".")[1]
        payload_segment += "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment.encode()).decode())
        exp = payload.get("exp")
        return (type(exp) in (int, float) and type(exp) is not bool) and exp <= time.time() + leeway_seconds
    except Exception:
        return False


def _extract_exp_from_jwt(access_token: str) -> Optional[float]:
    """Extract exp timestamp from Jobber JWT payload if valid numeric exp present."""
    try:
        payload_segment = access_token.split(".")[1]
        payload_segment += "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment.encode()).decode())
        exp = payload.get("exp")
        if (type(exp) in (int, float) and type(exp) is not bool) and exp > 0:
            return float(exp)
        return None
    except Exception:
        return None


def _timestamp_expires_soon(expires_at: float, leeway_seconds: int = 120) -> bool:
    """Return True when an explicit expiration timestamp is expired or close to expiring."""
    return (type(expires_at) in (int, float) and type(expires_at) is not bool) and expires_at <= time.time() + leeway_seconds


async def _read_jobber_tokens(contractor_id: str) -> Optional[dict]:
    """Read latest stored Jobber tokens, decrypting envelopes to memory strings.

    Returns None if reading fails, document is missing, provider is disconnected,
    generation is malformed, or stored refresh token is corrupt/missing.
    """
    from app.services.integration_tokens import (
        MAX_KEY_VERSION,
        determine_write_format,
        resolve_usable_token_pair,
        validate_token_expires_at,
        validate_token_string,
    )

    try:
        valid_cid = validate_token_string(contractor_id, name="contractor_id")
    except Exception:
        return None
    if valid_cid is None:
        return None

    try:
        from app.db.firestore_client import get_firestore_client

        db = get_firestore_client()
        if db is None:
            return None
        loop = asyncio.get_event_loop()
        doc = await loop.run_in_executor(
            None,
            lambda: db.collection("contractors").document(valid_cid).get(),
        )
        if not getattr(doc, "exists", False):
            return None
        data = doc.to_dict() or {}
    except Exception:
        return None

    cur_gen_raw = data.get("jobber_generation")
    if cur_gen_raw is None:
        cur_gen = 0
    elif type(cur_gen_raw) is int and type(cur_gen_raw) is not bool and 0 <= cur_gen_raw <= MAX_KEY_VERSION:
        cur_gen = cur_gen_raw
    else:
        return None

    connected = data.get("jobber_connected")
    if connected is False:
        return None
    if connected is not None and type(connected) is not bool:
        return None

    access_token, refresh_token = resolve_usable_token_pair(
        data,
        provider="jobber",
        contractor_id=valid_cid,
    )
    if not access_token or not refresh_token:
        return None

    raw_access = data.get("jobber_access_token")
    raw_refresh = data.get("jobber_refresh_token")

    try:
        determine_write_format(
            contractor_id=valid_cid,
            provider="jobber",
            stored_access=raw_access,
            stored_refresh=raw_refresh,
            envelope_required=data.get("jobber_token_envelope_required"),
        )
    except Exception:
        return None

    exp = data.get("jobber_token_expires_at")
    if exp is not None:
        try:
            validate_token_expires_at(exp)
        except Exception:
            return None

    return {
        "generation": cur_gen,
        "access_token_raw": raw_access,
        "refresh_token_raw": raw_refresh,
        "jobber_access_token": access_token,
        "jobber_refresh_token": refresh_token,
        "jobber_token_expires_at": exp,
        "connected": True,
    }


async def refresh_access_token(contractor: dict, *, force: bool = False) -> Optional[str]:
    """Refresh and persist Jobber OAuth tokens for a contractor with durable CAS."""
    from app.config import settings
    from app.services.integration_token_mutations import (
        IntegrationTokenLeaseError,
        acquire_refresh_claim_cas,
        persist_refreshed_tokens_cas,
        release_refresh_claim_cas,
    )
    from app.services.integration_tokens import (
        validate_token_expires_at,
        validate_token_expires_in,
        validate_token_string,
    )

    if type(contractor) is not dict:
        return None

    if "contractor_id" in contractor:
        raw_id = contractor.get("contractor_id")
    elif "id" in contractor:
        raw_id = contractor.get("id")
    else:
        raw_id = None

    try:
        valid_cid = validate_token_string(raw_id, name="contractor_id")
    except Exception:
        return None
    if valid_cid is None:
        return None

    snapshot = await _read_jobber_tokens(valid_cid)
    if not snapshot:
        return None

    def _hydrate_and_return(s: dict) -> str:
        contractor["jobber_access_token"] = s.get("access_token_raw")
        contractor["jobber_refresh_token"] = s.get("refresh_token_raw")
        contractor["jobber_generation"] = s.get("generation", 0)
        if s.get("jobber_token_expires_at") is not None:
            contractor["jobber_token_expires_at"] = s["jobber_token_expires_at"]
        else:
            contractor.pop("jobber_token_expires_at", None)
        return s["jobber_access_token"]

    # Check expiration unless force is requested
    if not force and snapshot.get("jobber_token_expires_at") is not None:
        expires_at = snapshot["jobber_token_expires_at"]
        if time.time() < expires_at - REFRESH_BUFFER_SECONDS:
            return _hydrate_and_return(snapshot)
    elif not force and snapshot.get("jobber_access_token"):
        # Fallback to decoding JWT exp if token_expires_at was not stored
        exp = _extract_exp_from_jwt(snapshot["jobber_access_token"])
        if exp and time.time() < exp - REFRESH_BUFFER_SECONDS:
            return _hydrate_and_return(snapshot)

    async with _get_refresh_lock(valid_cid):
        # Double check after acquiring in-process lock
        snapshot = await _read_jobber_tokens(valid_cid)
        if not snapshot:
            return None

        if not force and snapshot.get("jobber_token_expires_at") is not None:
            expires_at = snapshot["jobber_token_expires_at"]
            if time.time() < expires_at - REFRESH_BUFFER_SECONDS:
                return _hydrate_and_return(snapshot)
        elif not force and snapshot.get("jobber_access_token"):
            exp = _extract_exp_from_jwt(snapshot["jobber_access_token"])
            if exp and time.time() < exp - REFRESH_BUFFER_SECONDS:
                return _hydrate_and_return(snapshot)

        # Acquire multi-instance cross-process refresh lease before provider HTTP call
        claim_id: Optional[str] = None
        try:
            claim_id, _ = await acquire_refresh_claim_cas(
                contractor_id=valid_cid,
                provider="jobber",
                observed_generation=snapshot["generation"],
                observed_access_raw=snapshot["access_token_raw"],
                observed_refresh_raw=snapshot["refresh_token_raw"],
            )
        except IntegrationTokenLeaseError:
            # Contender instance: another worker is actively refreshing this contractor.
            # Re-read durable store: if winner already advanced generation, reload winner's tokens!
            latest_snap = await _read_jobber_tokens(valid_cid)
            if latest_snap and latest_snap["generation"] > snapshot["generation"]:
                winner_token = latest_snap.get("jobber_access_token")
                if winner_token:
                    contractor["jobber_access_token"] = latest_snap.get("access_token_raw")
                    contractor["jobber_refresh_token"] = latest_snap.get("refresh_token_raw")
                    contractor["jobber_generation"] = latest_snap["generation"]
                    if latest_snap.get("jobber_token_expires_at") is not None:
                        contractor["jobber_token_expires_at"] = latest_snap["jobber_token_expires_at"]
                    else:
                        contractor.pop("jobber_token_expires_at", None)
                    return winner_token
            logger.warning("Jobber token refresh actively locked by concurrent lease")
            return None
        except Exception:
            logger.error("Failed to acquire Jobber refresh lease")
            return None

        try:
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        JOBBER_TOKEN_URL,
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": snapshot["jobber_refresh_token"],
                            "client_id": settings.jobber_client_id,
                            "client_secret": settings.jobber_client_secret,
                        },
                        timeout=10.0,
                    )
            except Exception:
                logger.error(
                    "Jobber token refresh failed: provider=jobber operation=refresh result=error"
                )
                return None

            if response.status_code != 200:
                logger.error(
                    "Jobber token refresh failed: provider=jobber operation=refresh status_code=%s",
                    response.status_code,
                )
                return None

            try:
                tokens = response.json()
            except Exception:
                logger.error(
                    "Jobber token refresh invalid payload: provider=jobber operation=refresh result=invalid_json"
                )
                return None

            if type(tokens) is not dict:
                logger.error(
                    "Jobber token refresh invalid payload: provider=jobber operation=refresh result=invalid_type"
                )
                return None

            access_token = tokens.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                logger.error(
                    "Jobber token refresh returned no access token: provider=jobber operation=refresh"
                )
                return None

            try:
                validate_token_string(access_token, name="access_token")
            except Exception:
                logger.error("Jobber token refresh returned invalid access token")
                return None

            new_refresh_token = tokens.get("refresh_token")
            if not isinstance(new_refresh_token, str) or not new_refresh_token:
                logger.error(
                    "Jobber token refresh returned no new refresh token: provider=jobber operation=refresh"
                )
                return None

            try:
                validate_token_string(new_refresh_token, name="refresh_token")
            except Exception:
                logger.error("Jobber token refresh returned invalid new refresh token")
                return None

            expires_in = tokens.get("expires_in")
            expires_at = tokens.get("expires_at")
            token_expires_at = None
            if expires_at is not None:
                try:
                    token_expires_at = validate_token_expires_at(expires_at)
                except Exception:
                    logger.error("Jobber token refresh invalid expires_at: provider=jobber")
                    return None
            elif expires_in is not None:
                try:
                    exp_duration = validate_token_expires_in(expires_in)
                    token_expires_at = time.time() + exp_duration if exp_duration is not None else None
                except Exception:
                    logger.error("Jobber token refresh invalid expires_in: provider=jobber")
                    return None

            # Persist first with durable CAS; do NOT mutate contractor if persistence fails
            try:
                updates, next_gen = await persist_refreshed_tokens_cas(
                    contractor_id=valid_cid,
                    provider="jobber",
                    new_access_token=access_token,
                    new_refresh_token=new_refresh_token,
                    observed_generation=snapshot["generation"],
                    observed_access_raw=snapshot["access_token_raw"],
                    observed_refresh_raw=snapshot["refresh_token_raw"],
                    expires_at=token_expires_at,
                    claim_id=claim_id,
                )
                claim_id = None  # Cleared by atomic CAS commit!
            except Exception:
                logger.error(
                    "Jobber token persistence failed after refresh: provider=jobber operation=persist result=error"
                )
                return None

            # Mutate in-memory contractor only on verified durable success
            contractor["jobber_access_token"] = updates["jobber_access_token"]
            contractor["jobber_refresh_token"] = updates["jobber_refresh_token"]
            contractor["jobber_generation"] = next_gen
            from google.cloud.firestore_v1 import DELETE_FIELD
            if "jobber_token_expires_at" in updates and updates["jobber_token_expires_at"] is not DELETE_FIELD:
                contractor["jobber_token_expires_at"] = updates["jobber_token_expires_at"]
            else:
                contractor.pop("jobber_token_expires_at", None)

            logger.info("Jobber token refreshed for contractor %s", valid_cid[:8] or "unknown")
            return access_token
        finally:
            if claim_id is not None:
                await release_refresh_claim_cas(
                    contractor_id=valid_cid,
                    provider="jobber",
                    claim_id=claim_id,
                )


async def _resolve_access_token(auth: str | dict) -> str:
    if not isinstance(auth, dict):
        return ""

    from app.services.integration_tokens import resolve_usable_token

    access_token = resolve_usable_token(
        auth,
        provider="jobber",
        token_kind="access",
    ) or ""

    if access_token and _token_expires_soon(access_token):
        refreshed = await refresh_access_token(auth)
        if refreshed:
            return refreshed
    return access_token


async def _graphql_request(access_token: str, query: str, variables: dict = None) -> Optional[dict]:
    """Execute a Jobber GraphQL request."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                JOBBER_GRAPHQL_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "X-JOBBER-GRAPHQL-VERSION": JOBBER_GRAPHQL_VERSION,
                },
                json={"query": query, "variables": variables or {}},
                timeout=5.0,
            )
            if response.status_code == 200:
                data = response.json()
                if "errors" in data:
                    errors = data["errors"]
                    error_count = len(errors) if isinstance(errors, list) else 1
                    logger.warning(
                        f"Jobber GraphQL errors: operation=graphql_request "
                        f"status_code=200 error_count={error_count}"
                    )
                return data.get("data")
            if response.status_code == 401:
                raise JobberAuthError("Jobber access token rejected")
            logger.error(f"Jobber API error: {response.status_code}")
    except JobberAuthError:
        raise
    except Exception as e:
        logger.error(f"Jobber request failed: exception_type={type(e).__name__}")
    return None


async def _graphql_request_with_refresh(auth: str | dict, query: str, variables: dict = None) -> Optional[dict]:
    """Execute a Jobber request, refreshing contractor tokens once on 401."""
    if not isinstance(auth, dict):
        return None

    access_token = await _resolve_access_token(auth)
    if not access_token:
        return None

    try:
        return await _graphql_request(access_token, query, variables)
    except JobberAuthError:
        pass

    refreshed = await refresh_access_token(auth, force=True)
    if not refreshed:
        logger.error("Jobber API error: 401")
        return None

    try:
        return await _graphql_request(refreshed, query, variables)
    except JobberAuthError:
        logger.error("Jobber API error: 401")
        return None


def _extract_mutation_object(data: Optional[dict], mutation_name: str, object_name: str) -> Optional[dict]:
    """Return a mutation object only when Jobber accepted the mutation."""
    payload = (data or {}).get(mutation_name) or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        logger.warning(
            "Jobber mutation returned user errors: mutation=%s error_count=%s",
            mutation_name,
            len(user_errors),
        )
        return None
    obj = payload.get(object_name)
    if not obj:
        logger.warning("Jobber mutation returned no object: mutation=%s object=%s", mutation_name, object_name)
        return None
    return obj


def _split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "Unknown", "Caller"
    if len(parts) == 1:
        return parts[0], "Caller"
    return parts[0], " ".join(parts[1:])


def _first_property_id(client: dict) -> str:
    nodes = ((client or {}).get("clientProperties") or {}).get("nodes") or []
    if nodes:
        return nodes[0].get("id", "")
    return ""


def _build_client_create_input(job_data: dict) -> dict:
    first_name, last_name = _split_name(job_data.get("caller_name", ""))
    payload: dict = {
        "firstName": first_name,
        "lastName": last_name,
        "sourceAttribution": {"sourceText": "Hey Kevin"},
    }
    phone = job_data.get("caller_phone", "")
    if phone:
        payload["phones"] = [{"number": phone, "primary": True}]
    address = (job_data.get("address") or "").strip()
    if address:
        payload["properties"] = [{"address": {"street1": address}}]
    return payload


def _normalize_client_address(client: dict) -> dict:
    address = (client or {}).get("billingAddress")
    if not isinstance(address, dict) or address.get("street"):
        return client

    street = " ".join(filter(None, [address.get("street1", ""), address.get("street2", "")]))
    if street:
        address["street"] = street
    return client


async def lookup_customer(auth: str | dict, phone: str) -> Optional[dict]:
    """Look up a Jobber client by phone number."""
    if not phone:
        return None
    query = """
    query LookupClient($phone: String!) {
        clients(searchTerm: $phone, searchFields: [PHONES], first: 1) {
            nodes {
                id
                name
                firstName
                lastName
                phones { number }
                emails { address }
                billingAddress { street1 street2 city province postalCode }
                clientProperties(first: 1) { nodes { id } }
            }
        }
    }
    """
    data = await _graphql_request_with_refresh(auth, query, {"phone": phone})
    if data and data.get("clients", {}).get("nodes"):
        return _normalize_client_address(data["clients"]["nodes"][0])
    return None


async def create_client(auth: str | dict, job_data: dict) -> Optional[dict]:
    """Create a Jobber client for an unknown caller."""
    query = """
    mutation CreateClient($input: ClientCreateInput!) {
        clientCreate(input: $input) {
            client {
                id
                name
                clientProperties(first: 1) { nodes { id } }
            }
            userErrors { message path }
        }
    }
    """
    data = await _graphql_request_with_refresh(auth, query, {"input": _build_client_create_input(job_data)})
    client = _extract_mutation_object(data, "clientCreate", "client")
    if not client:
        return None
    return {
        "id": client.get("id", ""),
        "name": client.get("name", ""),
        "property_id": _first_property_id(client),
    }


async def create_request(auth: str | dict, request_data: dict) -> Optional[dict]:
    """Create a Jobber Request for a captured lead."""
    query = """
    mutation CreateRequest($input: RequestCreateInput!) {
        requestCreate(input: $input) {
            request { id title jobberWebUri }
            userErrors { message path }
        }
    }
    """
    input_data = {
        "clientId": request_data["client_id"],
        "title": request_data.get("title", "Phone inquiry from Hey Kevin")[:100],
    }
    if request_data.get("property_id"):
        input_data["propertyId"] = request_data["property_id"]
    data = await _graphql_request_with_refresh(auth, query, {"input": input_data})
    return _extract_mutation_object(data, "requestCreate", "request")


async def create_request_note(auth: str | dict, request_id: str, message: str) -> Optional[str]:
    """Attach Kevin call details to a Jobber Request."""
    query = """
    mutation CreateRequestNote($requestId: EncodedId!, $input: RequestCreateNoteInput!) {
        requestCreateNote(requestId: $requestId, input: $input) {
            request { id }
            requestNote { id }
            userErrors { message path }
        }
    }
    """
    data = await _graphql_request_with_refresh(
        auth,
        query,
        {"requestId": request_id, "input": {"message": message[:5000], "pinned": False}},
    )
    note = _extract_mutation_object(data, "requestCreateNote", "requestNote")
    if not note:
        return None
    return note.get("id", "")


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
    job = _extract_mutation_object(data, "jobCreate", "job")
    if job:
        return job["id"]
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
    quote = _extract_mutation_object(data, "quoteCreate", "quote")
    if quote:
        return quote["id"]
    return None
