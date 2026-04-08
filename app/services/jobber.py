"""Jobber GraphQL API client for FSM integration."""

import httpx
from typing import Optional
from app.utils.logging import get_logger

logger = get_logger(__name__)

JOBBER_GRAPHQL_URL = "https://api.getjobber.com/api/graphql"


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
            logger.error(f"Jobber API error: {response.status_code}")
    except Exception as e:
        logger.error(f"Jobber request failed: {e}")
    return None


async def lookup_customer(access_token: str, phone: str) -> Optional[dict]:
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
    data = await _graphql_request(access_token, query, {"phone": phone})
    if data and data.get("clients", {}).get("nodes"):
        return data["clients"]["nodes"][0]
    return None


async def get_available_slots(access_token: str, days_ahead: int = 7) -> list[dict]:
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
    data = await _graphql_request(access_token, query, {"startDate": start, "endDate": end})
    if data:
        return data.get("calendarEvents", {}).get("nodes", [])
    return []


async def create_job(access_token: str, job_data: dict) -> Optional[str]:
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

    data = await _graphql_request(access_token, query, {"input": input_data})
    if data and data.get("jobCreate", {}).get("job"):
        return data["jobCreate"]["job"]["id"]
    return None


async def create_quote(access_token: str, quote_data: dict) -> Optional[str]:
    """Create a quote in Jobber. Returns the quote ID."""
    query = """
    mutation CreateQuote($input: QuoteCreateInput!) {
        quoteCreate(input: $input) {
            quote { id quoteNumber }
            userErrors { message path }
        }
    }
    """
    data = await _graphql_request(access_token, query, {"input": quote_data})
    if data and data.get("quoteCreate", {}).get("quote"):
        return data["quoteCreate"]["quote"]["id"]
    return None
