"""Jobber GraphQL API client for FSM integration."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import httpx
from typing import Optional
from app.utils.logging import get_logger

logger = get_logger(__name__)

JOBBER_GRAPHQL_URL = "https://api.getjobber.com/api/graphql"
JOBBER_TOKEN_URL = "https://api.getjobber.com/api/oauth/token"
JOBBER_GRAPHQL_VERSION = "2025-04-16"
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


def _connection_nodes(connection: dict | None) -> list[dict]:
    if not isinstance(connection, dict):
        return []
    nodes = connection.get("nodes") or []
    return [node for node in nodes if isinstance(node, dict)]


def _compact_text(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _last_four(value: object) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 4:
        return ""
    return digits[-4:]


def _redact_phone_numbers(text: str) -> str:
    return re.sub(r"\+?\d[\d\s().-]{7,}\d", "[phone redacted]", text)


def _format_address(address: dict | None) -> str:
    if not isinstance(address, dict):
        return ""

    street = address.get("street") or " ".join(
        part for part in [address.get("street1"), address.get("street2")] if part
    )
    city = address.get("city", "")
    province = address.get("province", "")
    postal_code = address.get("postalCode", "")
    country = address.get("country", "")

    locality = " ".join(part for part in [province, postal_code] if part)
    parts = [part for part in [street, city, locality, country] if part]
    return ", ".join(parts)


def _normalize_customer_memory(client: dict | None) -> Optional[dict]:
    """Return the small Jobber customer slice Kevin can safely use as context."""
    if not isinstance(client, dict) or not client.get("id"):
        return None

    properties = []
    for prop in _connection_nodes(client.get("clientProperties")):
        properties.append({
            "id": prop.get("id", ""),
            "name": prop.get("name", ""),
            "jobberWebUri": prop.get("jobberWebUri", ""),
            "address": prop.get("address") or {},
        })

    notes = []
    for note in _connection_nodes(client.get("notes")):
        notes.append({
            "id": note.get("id", ""),
            "message": _compact_text(note.get("message"), 500),
            "createdAt": note.get("createdAt", ""),
            "pinned": bool(note.get("pinned", False)),
        })

    jobs = []
    for job in _connection_nodes(client.get("jobs")):
        visits = []
        for visit in _connection_nodes(job.get("visits")):
            visits.append({
                "title": visit.get("title", ""),
                "completedAt": visit.get("completedAt", ""),
                "isComplete": bool(visit.get("isComplete", False)),
                "visitStatus": visit.get("visitStatus", ""),
            })
        job_property = job.get("property") if isinstance(job.get("property"), dict) else {}
        jobs.append({
            "id": job.get("id", ""),
            "title": job.get("title", ""),
            "jobNumber": job.get("jobNumber", ""),
            "jobStatus": job.get("jobStatus", ""),
            "completedAt": job.get("completedAt", ""),
            "instructions": _compact_text(job.get("instructions"), 280),
            "jobberWebUri": job.get("jobberWebUri", ""),
            "property": {
                "id": job_property.get("id", ""),
                "name": job_property.get("name", ""),
                "address": job_property.get("address") or {},
            },
            "visits": visits,
        })

    requests = []
    for request in _connection_nodes(client.get("requests")):
        request_property = request.get("property") if isinstance(request.get("property"), dict) else {}
        requests.append({
            "id": request.get("id", ""),
            "title": request.get("title", ""),
            "requestStatus": request.get("requestStatus", ""),
            "createdAt": request.get("createdAt", ""),
            "updatedAt": request.get("updatedAt", ""),
            "jobberWebUri": request.get("jobberWebUri", ""),
            "property": {
                "id": request_property.get("id", ""),
                "name": request_property.get("name", ""),
                "address": request_property.get("address") or {},
            },
        })

    phone_last4s = []
    for phone in client.get("phones") or []:
        last4 = _last_four((phone or {}).get("normalizedPhoneNumber") or (phone or {}).get("number"))
        if last4 and last4 not in phone_last4s:
            phone_last4s.append(last4)

    return {
        "client": {
            "id": client.get("id", ""),
            "name": client.get("name", ""),
            "firstName": client.get("firstName", ""),
            "lastName": client.get("lastName", ""),
            "isLead": client.get("isLead", False),
            "leadSource": client.get("leadSource", ""),
            "jobberWebUri": client.get("jobberWebUri", ""),
        },
        "phone_last4s": phone_last4s,
        "properties": properties,
        "notes": notes,
        "jobs": jobs,
        "requests": requests,
    }


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


async def lookup_customer_memory(auth: str | dict, phone: str) -> Optional[dict]:
    """Look up a Jobber client and recent context for receptionist memory."""
    if not phone:
        return None

    query = """
    query LookupCustomerMemory($phone: String!) {
        clients(searchTerm: $phone, searchFields: [PHONES], first: 1) {
            nodes {
                id
                name
                firstName
                lastName
                isLead
                leadSource
                jobberWebUri
                phones { number normalizedPhoneNumber primary smsAllowed }
                clientProperties(first: 3) {
                    nodes {
                        id
                        name
                        jobberWebUri
                        address { street street1 street2 city province postalCode country }
                    }
                }
                notes(first: 3) {
                    nodes { id message createdAt pinned }
                }
                jobs(first: 3) {
                    nodes {
                        id
                        title
                        jobNumber
                        jobStatus
                        completedAt
                        instructions
                        jobberWebUri
                        property {
                            id
                            name
                            address { street street1 street2 city province postalCode country }
                        }
                        visits(first: 2) {
                            nodes { title completedAt isComplete visitStatus }
                        }
                    }
                }
                requests(first: 3) {
                    nodes {
                        id
                        title
                        requestStatus
                        createdAt
                        updatedAt
                        jobberWebUri
                        property {
                            id
                            name
                            address { street street1 street2 city province postalCode country }
                        }
                    }
                }
            }
        }
    }
    """
    data = await _graphql_request_with_refresh(auth, query, {"phone": phone})
    nodes = ((data or {}).get("clients") or {}).get("nodes") or []
    if not nodes:
        return None
    return _normalize_customer_memory(nodes[0])


def format_customer_memory_for_prompt(memory: Optional[dict], caller_phone: str = "") -> str:
    """Format Jobber memory as private, compact prompt context for Kevin."""
    if not isinstance(memory, dict):
        return ""
    client = memory.get("client") if isinstance(memory.get("client"), dict) else {}
    client_name = _compact_text(client.get("name"), 120)
    if not client_name:
        return ""

    caller_last4 = _last_four(caller_phone)
    if not caller_last4:
        phone_last4s = memory.get("phone_last4s") or []
        caller_last4 = phone_last4s[0] if phone_last4s else ""

    lines = [
        "CUSTOMER MEMORY FROM JOBBER (background context only):",
        "Use this private context to avoid asking for known name/address details. Do not recite it.",
    ]
    phone_suffix = f"; caller ID ending in {caller_last4}" if caller_last4 else ""
    lines.append(f"- Matched existing Jobber client: {client_name}{phone_suffix}.")

    for prop in (memory.get("properties") or [])[:2]:
        prop_name = _compact_text(prop.get("name"), 80)
        address = _format_address(prop.get("address"))
        if prop_name and address:
            lines.append(f"- Known property: {prop_name}, {address}.")
        elif address:
            lines.append(f"- Known property address: {address}.")
        elif prop_name:
            lines.append(f"- Known property: {prop_name}.")

    for job in (memory.get("jobs") or [])[:2]:
        title = _compact_text(job.get("title"), 140)
        if not title:
            continue
        number = f"#{job.get('jobNumber')} " if job.get("jobNumber") else ""
        status = f"; status {job.get('jobStatus')}" if job.get("jobStatus") else ""
        visits = job.get("visits") or []
        visit_status = ""
        if visits:
            status_text = visits[0].get("visitStatus") or ("complete" if visits[0].get("isComplete") else "")
            if status_text:
                visit_status = f"; visit {status_text}"
        lines.append(f"- Recent job: {number}{title}{status}{visit_status}.")

    for note in (memory.get("notes") or [])[:2]:
        message = _compact_text(note.get("message"), 260)
        if message:
            lines.append(f"- Recent note: {message}")

    for request in (memory.get("requests") or [])[:3]:
        title = _compact_text(request.get("title"), 150)
        if not title:
            continue
        status = f"; status {request.get('requestStatus')}" if request.get("requestStatus") else ""
        lines.append(f"- Recent request: {title}{status}.")

    lines.append(
        "If the caller asks for service, use this context naturally, but verify before assuming "
        "the current issue is at the same property."
    )

    context = _redact_phone_numbers("\n".join(lines))
    return context[:2500].rstrip()


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
