"""Jobber GraphQL API client for FSM integration."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

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


def _extract_exp_from_jwt(access_token: str) -> float | None:
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


async def _read_jobber_tokens(contractor_id: str) -> dict[str, Any] | None:
    """Read latest stored Jobber tokens from durable Firestore store."""
    from app.services.integration_token_mutations import load_durable_provider_snapshot
    return await load_durable_provider_snapshot(contractor_id, provider="jobber")


async def refresh_access_token(contractor: dict, *, force: bool = False) -> str | None:
    """Refresh and persist Jobber OAuth tokens for a contractor with durable CAS."""
    from app.config import settings
    from app.services.integration_token_mutations import (
        IntegrationTokenLeaseError,
        acquire_refresh_claim_cas,
        check_and_recover_expired_intent_preflight_cas,
        persist_refreshed_tokens_cas,
        quarantine_provider_reauth_cas,
        release_refresh_claim_cas,
        transition_refresh_claim_to_started_cas,
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

    preflight_status, preflight_msg = await check_and_recover_expired_intent_preflight_cas(
        contractor_id=valid_cid,
        provider="jobber",
    )
    if preflight_status != "proceed":
        logger.warning(
            "Jobber token refresh preflight blocked: provider=jobber operation=refresh result=blocked reason=%s",
            preflight_msg or "blocked",
        )
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
        claim_id: str | None = None
        claim_phase: str | None = None
        try:
            claim_id, _ = await acquire_refresh_claim_cas(
                contractor_id=valid_cid,
                provider="jobber",
                observed_generation=snapshot["generation"],
                observed_access_raw=snapshot["access_token_raw"],
                observed_refresh_raw=snapshot["refresh_token_raw"],
            )
            claim_phase = "reserved"
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
            # Transition claim to started phase before provider HTTP request
            try:
                claim_id, _ = await transition_refresh_claim_to_started_cas(
                    contractor_id=valid_cid,
                    provider="jobber",
                    claim_id=claim_id,
                    observed_generation=snapshot["generation"],
                    observed_access_raw=snapshot["access_token_raw"],
                    observed_refresh_raw=snapshot["refresh_token_raw"],
                )
                claim_phase = "provider_request_started"
            except Exception:
                logger.error("Failed to transition Jobber refresh lease to started")
                return None

            http_success = False
            response = None
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
                if response.status_code == 200:
                    http_success = True
            except Exception:
                logger.error(
                    "Jobber token refresh failed: provider=jobber operation=refresh result=error"
                )

            if not http_success or response is None or response.status_code != 200:
                logger.error(
                    "Jobber token refresh failed in started phase: provider=jobber operation=refresh status_code=%s",
                    getattr(response, "status_code", "network_error"),
                )
                await quarantine_provider_reauth_cas(
                    contractor_id=valid_cid,
                    provider="jobber",
                    claim_id=claim_id,
                    observed_generation=snapshot["generation"],
                    observed_lifecycle_epoch=snapshot.get("lifecycle_epoch", snapshot.get("generation", 0)),
                    observed_access_raw=snapshot["access_token_raw"],
                    observed_refresh_raw=snapshot["refresh_token_raw"],
                )
                return None

            try:
                tokens = response.json()
            except Exception:
                logger.error(
                    "Jobber token refresh invalid payload: provider=jobber operation=refresh result=invalid_json"
                )
                await quarantine_provider_reauth_cas(
                    contractor_id=valid_cid,
                    provider="jobber",
                    claim_id=claim_id,
                    observed_generation=snapshot["generation"],
                    observed_lifecycle_epoch=snapshot.get("lifecycle_epoch", snapshot.get("generation", 0)),
                    observed_access_raw=snapshot["access_token_raw"],
                    observed_refresh_raw=snapshot["refresh_token_raw"],
                )
                return None

            if type(tokens) is not dict:
                logger.error(
                    "Jobber token refresh invalid payload: provider=jobber operation=refresh result=invalid_type"
                )
                await quarantine_provider_reauth_cas(
                    contractor_id=valid_cid,
                    provider="jobber",
                    claim_id=claim_id,
                    observed_generation=snapshot["generation"],
                    observed_lifecycle_epoch=snapshot.get("lifecycle_epoch", snapshot.get("generation", 0)),
                    observed_access_raw=snapshot["access_token_raw"],
                    observed_refresh_raw=snapshot["refresh_token_raw"],
                )
                return None

            access_token = tokens.get("access_token")
            if type(access_token) is not str or len(access_token) == 0:
                logger.error(
                    "Jobber token refresh returned no access token: provider=jobber operation=refresh"
                )
                await quarantine_provider_reauth_cas(
                    contractor_id=valid_cid,
                    provider="jobber",
                    claim_id=claim_id,
                    observed_generation=snapshot["generation"],
                    observed_lifecycle_epoch=snapshot.get("lifecycle_epoch", snapshot.get("generation", 0)),
                    observed_access_raw=snapshot["access_token_raw"],
                    observed_refresh_raw=snapshot["refresh_token_raw"],
                )
                return None

            try:
                validate_token_string(access_token, name="access_token")
            except Exception:
                logger.error("Jobber token refresh returned invalid access token")
                await quarantine_provider_reauth_cas(
                    contractor_id=valid_cid,
                    provider="jobber",
                    claim_id=claim_id,
                    observed_generation=snapshot["generation"],
                    observed_lifecycle_epoch=snapshot.get("lifecycle_epoch", snapshot.get("generation", 0)),
                    observed_access_raw=snapshot["access_token_raw"],
                    observed_refresh_raw=snapshot["refresh_token_raw"],
                )
                return None

            new_refresh_token = tokens.get("refresh_token")
            if type(new_refresh_token) is not str or len(new_refresh_token) == 0:
                logger.error(
                    "Jobber token refresh returned no new refresh token: provider=jobber operation=refresh"
                )
                await quarantine_provider_reauth_cas(
                    contractor_id=valid_cid,
                    provider="jobber",
                    claim_id=claim_id,
                    observed_generation=snapshot["generation"],
                    observed_lifecycle_epoch=snapshot.get("lifecycle_epoch", snapshot.get("generation", 0)),
                    observed_access_raw=snapshot["access_token_raw"],
                    observed_refresh_raw=snapshot["refresh_token_raw"],
                )
                return None

            try:
                validate_token_string(new_refresh_token, name="refresh_token")
            except Exception:
                logger.error("Jobber token refresh returned invalid new refresh token")
                await quarantine_provider_reauth_cas(
                    contractor_id=valid_cid,
                    provider="jobber",
                    claim_id=claim_id,
                    observed_generation=snapshot["generation"],
                    observed_lifecycle_epoch=snapshot.get("lifecycle_epoch", snapshot.get("generation", 0)),
                    observed_access_raw=snapshot["access_token_raw"],
                    observed_refresh_raw=snapshot["refresh_token_raw"],
                )
                return None

            expires_in = tokens.get("expires_in")
            expires_at = tokens.get("expires_at")
            token_expires_in = None
            token_expires_at = None
            if expires_in is not None:
                try:
                    token_expires_in = validate_token_expires_in(expires_in)
                except Exception:
                    logger.error("Jobber token refresh invalid expires_in: provider=jobber")
                    await quarantine_provider_reauth_cas(
                        contractor_id=valid_cid,
                        provider="jobber",
                        claim_id=claim_id,
                        observed_generation=snapshot["generation"],
                        observed_lifecycle_epoch=snapshot.get("lifecycle_epoch", snapshot.get("generation", 0)),
                        observed_access_raw=snapshot["access_token_raw"],
                        observed_refresh_raw=snapshot["refresh_token_raw"],
                    )
                    return None
            elif expires_at is not None:
                try:
                    token_expires_at = validate_token_expires_at(expires_at)
                except Exception:
                    logger.error("Jobber token refresh invalid expires_at: provider=jobber")
                    await quarantine_provider_reauth_cas(
                        contractor_id=valid_cid,
                        provider="jobber",
                        claim_id=claim_id,
                        observed_generation=snapshot["generation"],
                        observed_lifecycle_epoch=snapshot.get("lifecycle_epoch", snapshot.get("generation", 0)),
                        observed_access_raw=snapshot["access_token_raw"],
                        observed_refresh_raw=snapshot["refresh_token_raw"],
                    )
                    return None

            # Persist first with durable CAS; do NOT mutate contractor if persistence fails
            try:
                updates, next_gen = await persist_refreshed_tokens_cas(
                    contractor_id=valid_cid,
                    provider="jobber",
                    new_access_token=access_token,
                    new_refresh_token=new_refresh_token,
                    observed_generation=snapshot["generation"],
                    observed_lifecycle_epoch=snapshot.get("lifecycle_epoch", snapshot.get("generation", 0)),
                    observed_access_raw=snapshot["access_token_raw"],
                    observed_refresh_raw=snapshot["refresh_token_raw"],
                    expires_in=token_expires_in,
                    expires_at=token_expires_at,
                    claim_id=claim_id,
                )
                claim_id = None  # Cleared by atomic CAS commit!
            except Exception:
                logger.error(
                    "Jobber token persistence failed after refresh: provider=jobber operation=persist result=error"
                )
                try:
                    await quarantine_provider_reauth_cas(
                        contractor_id=valid_cid,
                        provider="jobber",
                        claim_id=claim_id,
                        observed_generation=snapshot["generation"],
                        observed_lifecycle_epoch=snapshot.get("lifecycle_epoch", snapshot.get("generation", 0)),
                        observed_access_raw=snapshot["access_token_raw"],
                        observed_refresh_raw=snapshot["refresh_token_raw"],
                    )
                except Exception:
                    logger.error("Jobber quarantine failed after persistence failure; started claim retained")
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

            logger.info("Jobber token refreshed: provider=jobber operation=refresh result=success")
            return access_token
        finally:
            if claim_id is not None and claim_phase == "reserved":
                await release_refresh_claim_cas(
                    contractor_id=valid_cid,
                    provider="jobber",
                    claim_id=claim_id,
                )


async def _resolve_access_token(auth: str | dict) -> str:
    if type(auth) is not dict:
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


def _timestamp_expires_soon(expires_at: Any) -> bool:
    """Return True if Unix timestamp expires within REFRESH_BUFFER_SECONDS."""
    if type(expires_at) not in (int, float) or type(expires_at) is bool:
        return False
    return expires_at <= time.time() + REFRESH_BUFFER_SECONDS


class JobberNetworkError(Exception):
    """Raised when a Jobber HTTP request fails with network error, timeout, rate limit, or server error."""


async def _graphql_request(access_token: str, query: str, variables: dict = None) -> dict | None:
    """Execute raw Jobber GraphQL request with access token, raising JobberAuthError on 401 and JobberNetworkError on ambiguous failure."""
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
                try:
                    data = response.json()
                except Exception as exc:
                    logger.error("Jobber GraphQL response is invalid JSON: provider=jobber operation=graphql_request result=invalid_json")
                    raise JobberNetworkError("Invalid JSON in Jobber GraphQL response") from exc
                if type(data) is not dict:
                    logger.error("Jobber GraphQL response is non-dict: provider=jobber operation=graphql_request result=invalid_payload")
                    raise JobberNetworkError("Non-dict JSON payload in Jobber GraphQL response")
                if "errors" in data:
                    errors = data["errors"]
                    error_count = len(errors) if isinstance(errors, list) else 1
                    logger.warning(
                        "Jobber GraphQL errors: operation=graphql_request status_code=200 error_count=%s",
                        error_count,
                    )
                return data.get("data")
            if response.status_code == 401:
                raise JobberAuthError("Jobber access token rejected")
            if response.status_code in (408, 425, 429) or response.status_code >= 500:
                logger.error("Jobber API server error: status_code=%s", response.status_code)
                raise JobberNetworkError(f"Jobber HTTP server status {response.status_code}")
            logger.error("Jobber API error: status_code=%s", response.status_code)
            return None
    except (JobberAuthError, JobberNetworkError):
        raise
    except Exception as e:
        logger.error("Jobber request failed: provider=jobber operation=graphql_request result=error")
        raise JobberNetworkError("Jobber request transport exception") from e


async def _graphql_request_with_refresh(auth: str | dict, query: str, variables: dict = None) -> dict | None:
    """Execute a Jobber request, authorizing via fresh durable snapshot and refreshing contractor tokens once on 401."""
    from app.services.integration_token_mutations import load_durable_provider_snapshot
    from app.services.integration_tokens import validate_token_string

    if type(auth) is not dict:
        return None

    if "contractor_id" in auth:
        raw_cid = auth["contractor_id"]
    elif "id" in auth:
        raw_cid = auth["id"]
    else:
        raw_cid = None

    if type(raw_cid) is not str:
        return None

    try:
        valid_cid = validate_token_string(raw_cid, name="contractor_id")
    except Exception:
        return None
    if valid_cid is None:
        return None

    # Invariant 1: Durable snapshot authorization gate immediately before provider call
    snap = await load_durable_provider_snapshot(valid_cid, provider="jobber")
    if snap is None:
        refreshed = await refresh_access_token(auth)
        if not refreshed:
            return None
        snap = await load_durable_provider_snapshot(valid_cid, provider="jobber")
        if snap is None:
            return None

    in_memory_expires_at = auth.get("jobber_token_expires_at")
    auth["jobber_access_token"] = snap.get("access_token_raw")
    auth["jobber_refresh_token"] = snap.get("refresh_token_raw")
    auth["jobber_generation"] = snap.get("generation", 0)
    if snap.get("expires_at") is not None:
        auth["jobber_token_expires_at"] = snap["expires_at"]
    elif in_memory_expires_at is not None:
        auth["jobber_token_expires_at"] = in_memory_expires_at
    else:
        auth.pop("jobber_token_expires_at", None)

    # Proactive refresh check
    if (auth.get("jobber_token_expires_at") is not None and _timestamp_expires_soon(auth["jobber_token_expires_at"])) or (
        snap.get("access_token") and _token_expires_soon(snap["access_token"])
    ):
        refreshed = await refresh_access_token(auth)
        if not refreshed:
            # Proactive refresh failed: fail closed without dispatching expired access token
            return None
        # Read fresh durable snapshot after proactive refresh
        fresh_snap = await load_durable_provider_snapshot(valid_cid, provider="jobber")
        if fresh_snap is None:
            return None
        snap = fresh_snap

    access_token = snap["access_token"]
    claim_id = None
    from app.services.integration_token_mutations import (
        acquire_provider_operation_intent_cas,
        terminalize_provider_operation_intent_cas,
        transition_provider_operation_intent_to_started_cas,
    )

    terminal_outcome = False
    try:
        claim_id, _ = await acquire_provider_operation_intent_cas(
            contractor_id=valid_cid,
            provider="jobber",
            kind="business",
            observed_generation=snap.get("generation"),
            observed_lifecycle_epoch=snap.get("lifecycle_epoch"),
        )
        await transition_provider_operation_intent_to_started_cas(
            contractor_id=valid_cid,
            provider="jobber",
            claim_id=claim_id,
            kind="business",
        )
        res = await _graphql_request(access_token, query, variables)
        terminal_outcome = True
        return res
    except JobberAuthError:
        terminal_outcome = True
    except JobberNetworkError:
        raise
    finally:
        if terminal_outcome and claim_id is not None:
            await terminalize_provider_operation_intent_cas(
                contractor_id=valid_cid,
                provider="jobber",
                claim_id=claim_id,
                kind="business",
            )

    refreshed = await refresh_access_token(auth, force=True)
    if not refreshed:
        logger.error("Jobber API error: 401")
        return None

    # Read fresh durable snapshot after 401 forced refresh
    retry_snap = await load_durable_provider_snapshot(valid_cid, provider="jobber")
    if retry_snap is None:
        return None

    retry_claim_id = None
    retry_terminal_outcome = False
    try:
        retry_claim_id, _ = await acquire_provider_operation_intent_cas(
            contractor_id=valid_cid,
            provider="jobber",
            kind="business",
            observed_generation=retry_snap.get("generation"),
            observed_lifecycle_epoch=retry_snap.get("lifecycle_epoch"),
        )
        await transition_provider_operation_intent_to_started_cas(
            contractor_id=valid_cid,
            provider="jobber",
            claim_id=retry_claim_id,
            kind="business",
        )
        retry_res = await _graphql_request(retry_snap["access_token"], query, variables)
        retry_terminal_outcome = True
        return retry_res
    except JobberAuthError:
        retry_terminal_outcome = True
        logger.error("Jobber API error: 401 after forced refresh")
        return None
    except JobberNetworkError:
        raise
    finally:
        if retry_terminal_outcome and retry_claim_id is not None:
            await terminalize_provider_operation_intent_cas(
                contractor_id=valid_cid,
                provider="jobber",
                claim_id=retry_claim_id,
                kind="business",
            )
    return None


def _extract_mutation_object(data: dict | None, mutation_name: str, object_name: str) -> dict | None:
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


async def lookup_customer(auth: str | dict, phone: str) -> dict | None:
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


async def create_client(auth: str | dict, job_data: dict) -> dict | None:
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


async def create_request(auth: str | dict, request_data: dict) -> dict | None:
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


async def create_request_note(auth: str | dict, request_id: str, message: str) -> str | None:
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


async def create_job(auth: str | dict, job_data: dict) -> str | None:
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


async def create_quote(auth: str | dict, quote_data: dict) -> str | None:
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
