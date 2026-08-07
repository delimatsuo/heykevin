#!/usr/bin/env python3
"""Probe Jobber OAuth + GraphQL schema without touching Kevin contractor records."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


AUTH_URL = "https://api.getjobber.com/api/oauth/authorize"
TOKEN_URL = "https://api.getjobber.com/api/oauth/token"
GRAPHQL_URL = "https://api.getjobber.com/api/graphql"
DEFAULT_REDIRECT_URI = "http://localhost:8080/api/integrations/jobber/callback"
DEFAULT_API_VERSION = "2025-04-16"
KEYWORDS = (
    "availability",
    "available",
    "booking",
    "calendar",
    "schedule",
    "scheduled",
    "visit",
    "assessment",
    "appointment",
    "job",
    "request",
    "client",
    "user",
)


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def require_env(name: str, env: dict[str, str]) -> str:
    value = os.environ.get(name) or env.get(name) or ""
    if not value:
        raise SystemExit(f"Missing {name}. Add it to .env or export it.")
    return value


def unwrap_type(type_ref: dict[str, Any] | None) -> dict[str, Any] | None:
    current = type_ref
    while current and current.get("ofType"):
        current = current["ofType"]
    return current


def type_name(type_ref: dict[str, Any] | None) -> str:
    if not type_ref:
        return ""
    kind = type_ref.get("kind", "")
    name = type_ref.get("name")
    of_type = type_ref.get("ofType")
    if kind == "NON_NULL":
        return f"{type_name(of_type)}!"
    if kind == "LIST":
        return f"[{type_name(of_type)}]"
    return name or kind


def field_names(type_map: dict[str, dict[str, Any]], type_name_value: str) -> set[str]:
    fields = type_map.get(type_name_value, {}).get("fields") or []
    return {field["name"] for field in fields}


def input_fields(type_map: dict[str, dict[str, Any]], type_name_value: str) -> list[dict[str, str]]:
    fields = type_map.get(type_name_value, {}).get("inputFields") or []
    return [{"name": field["name"], "type": type_name(field.get("type"))} for field in fields]


def output_fields(type_map: dict[str, dict[str, Any]], type_name_value: str) -> list[dict[str, str]]:
    fields = type_map.get(type_name_value, {}).get("fields") or []
    return [{"name": field["name"], "type": type_name(field.get("type"))} for field in fields]


class CallbackState:
    code: str = ""
    error: str = ""
    received_state: str = ""


def receive_oauth_code(redirect_uri: str, state: str, timeout_seconds: int) -> tuple[str, str]:
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    expected_path = parsed.path or "/"
    callback_state = CallbackState()
    ready = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib callback name
            request_url = urlparse(self.path)
            if request_url.path != expected_path:
                self.send_response(404)
                self.end_headers()
                return

            params = parse_qs(request_url.query)
            callback_state.code = params.get("code", [""])[0]
            callback_state.error = params.get("error", [""])[0]
            callback_state.received_state = params.get("state", [""])[0]

            if callback_state.code and callback_state.received_state == state:
                body = "Jobber connected for local probing. You can close this tab."
                self.send_response(200)
            else:
                body = "Jobber OAuth callback was received but did not validate."
                self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())
            ready.set()

        def log_message(self, *_args):
            return

    server = HTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not ready.wait(timeout_seconds):
            raise TimeoutError("Timed out waiting for OAuth callback.")
    finally:
        server.shutdown()
        server.server_close()

    if callback_state.error:
        raise RuntimeError(f"Jobber OAuth error: {callback_state.error}")
    if callback_state.received_state != state:
        raise RuntimeError("OAuth state mismatch.")
    if not callback_state.code:
        raise RuntimeError("OAuth callback did not include an authorization code.")
    return callback_state.code, callback_state.received_state


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict[str, Any]:
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=20.0,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Token exchange failed: HTTP {response.status_code} {response.text[:500]}")
    tokens = response.json()
    if not tokens.get("access_token"):
        raise RuntimeError("Token exchange response did not include an access_token.")
    return tokens


def graphql(access_token: str, api_version: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    response = httpx.post(
        GRAPHQL_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-JOBBER-GRAPHQL-VERSION": api_version,
        },
        json={"query": query, "variables": variables or {}},
        timeout=30.0,
    )
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text[:1000]}
    return {"status_code": response.status_code, "body": body}


INTROSPECTION_QUERY = """
query JobberSchemaProbe {
  __schema {
    queryType { fields { name args { name type { ...TypeRef } } type { ...TypeRef } } }
    mutationType { fields { name args { name type { ...TypeRef } } type { ...TypeRef } } }
    types {
      kind
      name
      fields {
        name
        args { name type { ...TypeRef } }
        type { ...TypeRef }
      }
      inputFields { name type { ...TypeRef } }
      enumValues { name }
      possibleTypes { name }
    }
  }
}

fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType { kind name }
      }
    }
  }
}
"""


def query_first_records(field: str, fields: list[str]) -> str:
    return f"""
query Probe{field[:1].upper() + field[1:]} {{
  {field}(first: 3) {{
    totalCount
    nodes {{
      __typename
      {' '.join(fields)}
    }}
  }}
}}
"""


def summarize_probe_result(result: dict[str, Any], root_field: str) -> dict[str, Any]:
    body = result["body"]
    data = body.get("data") or {}
    root = data.get(root_field)
    errors = body.get("errors") or []
    summary: dict[str, Any] = {
        "status_code": result["status_code"],
        "ok": result["status_code"] == 200 and not errors and root is not None,
        "errors": errors,
    }
    if isinstance(root, dict):
        if "totalCount" in root:
            summary["totalCount"] = root.get("totalCount")
        if isinstance(root.get("nodes"), list):
            summary["sample_count"] = len(root.get("nodes") or [])
            summary["sample_typenames"] = sorted(
                {
                    node.get("__typename")
                    for node in root.get("nodes") or []
                    if isinstance(node, dict) and node.get("__typename")
                }
            )
    return summary


def run_safe_read_probes(access_token: str, api_version: str, type_map: dict[str, dict[str, Any]], query_fields: set[str]) -> dict[str, Any]:
    probes: dict[str, str] = {}
    if "clients" in query_fields:
        client_fields = ["id"]
        names = field_names(type_map, "Client")
        client_fields += [field for field in ("name", "firstName", "lastName", "companyName") if field in names]
        probes["clients"] = query_first_records("clients", client_fields)
    if "jobs" in query_fields:
        job_fields = ["id"]
        names = field_names(type_map, "Job")
        job_fields += [field for field in ("jobNumber", "title", "status", "instructions") if field in names]
        probes["jobs"] = query_first_records("jobs", job_fields)
    if "requests" in query_fields:
        request_fields = ["id"]
        names = field_names(type_map, "Request")
        request_fields += [field for field in ("title", "status", "createdAt", "message") if field in names]
        probes["requests"] = query_first_records("requests", request_fields)
    if "users" in query_fields:
        probes["users"] = """
query ProbeUsers {
  users(first: 3) {
    totalCount
    nodes {
      __typename
      id
      availableForScheduling
      status
      timezone
      name { full }
      email { raw isValid }
    }
  }
}
"""
    if "visits" in query_fields:
        visit_fields = ["id"]
        names = field_names(type_map, "Visit")
        visit_fields += [field for field in ("title", "startAt", "endAt", "instructions") if field in names]
        probes["visits"] = query_first_records("visits", visit_fields)
    if "onlineBookingConfiguration" in query_fields:
        probes["onlineBookingConfiguration"] = """
query ProbeOnlineBookingConfiguration {
  onlineBookingConfiguration {
    id
    acceptingOnlineBookings
    bookingUrl
    bookingEmbedScript
  }
}
"""
    if "requestSettingsCollection" in query_fields:
        probes["requestSettingsCollection"] = """
query ProbeRequestSettingsCollection {
  requestSettingsCollection(first: 10, filter: {enabled: true}) {
    totalCount
    nodes {
      id
      name
      enabled
      bookingType
      requestUrl
      embeddedRequestUrl
      requiresBookingApproval
      intervalDurationMinutes
      bufferDurationMinutes
      earliestAvailabilityMinutes
      efficientSchedulingType
      serviceAreasEnabled
    }
  }
}
"""
    if "scheduledItems" in query_fields:
        probes["scheduledItems"] = """
query ProbeScheduledItems($startAt: ISO8601DateTime!, $endAt: ISO8601DateTime!) {
  scheduledItems(
    first: 20
    filter: {
      occursWithin: { startAt: $startAt, endAt: $endAt }
      includeUnscheduled: false
    }
  ) {
    totalCount
    nodes {
      __typename
      id
      title
      startAt
      endAt
      duration
      allDay
      assignedUsers(first: 5) {
        nodes {
          id
          availableForScheduling
          name { full }
        }
      }
      ... on Visit {
        visitStatus
        client { id name }
        job { id jobNumber title }
      }
      ... on Assessment {
        client { id name }
        request { id title }
      }
      ... on Event {
        description
      }
      ... on Task {
        instructions
      }
    }
  }
}
"""

    results: dict[str, Any] = {}
    now = datetime.now(timezone.utc)
    variables = {
        "scheduledItems": {
            "startAt": now.isoformat(),
            "endAt": (now + timedelta(days=14)).isoformat(),
        }
    }
    for name, query in probes.items():
        result = graphql(access_token, api_version, query, variables.get(name))
        results[name] = summarize_probe_result(result, name) | {"query": query}
    return results


def summarize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    query_fields = schema["queryType"]["fields"]
    mutation_fields = schema["mutationType"]["fields"]
    types = schema["types"]
    type_map = {item["name"]: item for item in types if item.get("name")}

    def relevant_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        relevant = []
        for field in fields:
            name = field["name"]
            if any(keyword in name.lower() for keyword in KEYWORDS):
                relevant.append(
                    {
                        "name": name,
                        "type": type_name(field.get("type")),
                        "args": [
                            {"name": arg["name"], "type": type_name(arg.get("type"))}
                            for arg in field.get("args") or []
                        ],
                    }
                )
        return relevant

    interesting_type_names = sorted(
        name
        for name in type_map
        if any(keyword in name.lower() for keyword in KEYWORDS)
        and not name.startswith("__")
    )
    create_or_schedule_inputs = {
        name: input_fields(type_map, name)
        for name in interesting_type_names
        if name.endswith("Input")
        or name.endswith("Attributes")
        or "Create" in name
        or "Schedule" in name
        or "Availability" in name
    }

    return {
        "query_fields": relevant_fields(query_fields),
        "mutation_fields": relevant_fields(mutation_fields),
        "interesting_types": {
            name: {
                "kind": type_map[name].get("kind"),
                "fields": output_fields(type_map, name)[:80],
                "inputFields": input_fields(type_map, name)[:80],
                "enumValues": [item["name"] for item in type_map[name].get("enumValues") or []],
                "possibleTypes": [item["name"] for item in type_map[name].get("possibleTypes") or []],
            }
            for name in interesting_type_names
        },
        "create_or_schedule_inputs": create_or_schedule_inputs,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Jobber OAuth and GraphQL schema.")
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    parser.add_argument("--api-version", default=os.environ.get("JOBBER_GRAPHQL_VERSION", DEFAULT_API_VERSION))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--report", default="secrets/jobber_probe_report.json")
    parser.add_argument("--schema", default="secrets/jobber_schema.json")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    env = load_dotenv(Path(args.env_file))
    client_id = require_env("JOBBER_CLIENT_ID", env)
    client_secret = require_env("JOBBER_CLIENT_SECRET", env)
    state = secrets.token_urlsafe(32)
    auth_url = AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": args.redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )

    print("Starting local Jobber OAuth listener.")
    print(f"Redirect URI: {args.redirect_uri}")
    print("Opening Jobber authorization URL in your browser.")
    if args.no_open:
        print(auth_url)
    else:
        subprocess.run(["open", auth_url], check=False)

    code, _received_state = receive_oauth_code(args.redirect_uri, state, args.timeout)
    print("OAuth callback received. Exchanging authorization code.")
    tokens = exchange_code(client_id, client_secret, args.redirect_uri, code)
    access_token = tokens["access_token"]

    print(f"Querying Jobber GraphQL schema with version {args.api_version}.")
    introspection = graphql(access_token, args.api_version, INTROSPECTION_QUERY)
    if introspection["status_code"] != 200 or introspection["body"].get("errors"):
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "api_version": args.api_version,
            "introspection": introspection,
        }
        write_report(Path(args.report), report)
        print(f"Introspection failed. Report written to {args.report}")
        return 1

    schema = introspection["body"]["data"]["__schema"]
    write_report(Path(args.schema), schema)
    summary = summarize_schema(schema)
    type_map = {item["name"]: item for item in schema["types"] if item.get("name")}
    query_fields = {field["name"] for field in schema["queryType"]["fields"]}
    read_probe_results = run_safe_read_probes(access_token, args.api_version, type_map, query_fields)

    now = datetime.now(timezone.utc)
    date_window = {
        "start": now.isoformat(),
        "end": (now + timedelta(days=14)).isoformat(),
    }
    report = {
        "generated_at": now.isoformat(),
        "api_version": args.api_version,
        "token_response_metadata": {
            "has_access_token": bool(tokens.get("access_token")),
            "has_refresh_token": bool(tokens.get("refresh_token")),
            "expires_in": tokens.get("expires_in"),
            "scope": tokens.get("scope"),
            "token_type": tokens.get("token_type"),
        },
        "version_extensions": introspection["body"].get("extensions"),
        "date_window_for_followup_queries": date_window,
        "schema_summary": summary,
        "safe_read_probe_results": read_probe_results,
    }
    write_report(Path(args.report), report)

    print(f"Report written to {args.report}")
    ok_reads = [name for name, result in read_probe_results.items() if result.get("ok")]
    failed_reads = [name for name, result in read_probe_results.items() if not result.get("ok")]
    print(f"Safe read probes OK: {', '.join(ok_reads) or 'none'}")
    if failed_reads:
        print(f"Safe read probes needing query adjustment: {', '.join(failed_reads)}")
    print(
        textwrap.dedent(
            """
            No create/update mutations were executed.
            Next step: inspect the report for scheduling mutations and input requirements.
            """
        ).strip()
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
