#!/usr/bin/env python3
"""Prepare, enable, or disable the test-restricted staging observation shadow."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
import os
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STAGING_PROJECT_ID = "kevin-staging-491315"
STAGING_HEALTH_HOST = "kevin-api-staging-l63rergg7a-uc.a.run.app"
HMAC_KEY_ENV = "RECEPTIONIST_OBSERVATION_SHADOW_CALLER_HMAC_KEY"
ENABLE_CONFIRMATION = "enable-staging-observation-shadow"
DISABLE_CONFIRMATION = "disable-staging-observation-shadow"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
CONTRACTOR_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")


class AuthorizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EnablementPlan:
    project: str
    health_url: str
    expected_sha: str
    contractor_id: str
    contractor_label: str
    ttl_seconds: int
    fields: dict[str, Any]

    def redacted_summary(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "project": self.project,
            "expected_sha": self.expected_sha,
            "contractor_label": self.contractor_label,
            "ttl_seconds": self.ttl_seconds,
            "caller_digest_count": len(
                self.fields["receptionist_observation_shadow_caller_digests"]
            ),
            "writes_authorized": False,
        }


def _contractor_label(contractor_id: str) -> str:
    return sha256(contractor_id.encode("utf-8")).hexdigest()[:12]


def _caller_hmac_digest(caller_identifier: str, key: str) -> str:
    if not caller_identifier.strip():
        raise AuthorizationError("caller identifier is required")
    if len(key.encode("utf-8")) < 32:
        raise AuthorizationError("caller HMAC input is invalid")
    return hmac.new(
        key.encode("utf-8"),
        caller_identifier.strip().encode("utf-8"),
        sha256,
    ).hexdigest()


def _validate_staging_health_url(value: str) -> str:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise AuthorizationError("health URL must be valid HTTPS") from exc
    if parsed.scheme != "https":
        raise AuthorizationError("health URL must use HTTPS")
    if (
        parsed.hostname != STAGING_HEALTH_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise AuthorizationError("health URL must use the staging host")
    if (
        parsed.path.rstrip("/") != "/health"
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise AuthorizationError("health URL must target the staging /health path")
    return value


def build_enablement_plan(
    *,
    project: str,
    health_url: str,
    expected_sha: str,
    contractor_id: str,
    caller_identifier: str,
    hmac_key: str,
    ttl_seconds: int,
    now: float | None = None,
) -> EnablementPlan:
    if project != STAGING_PROJECT_ID:
        raise AuthorizationError("project must be the dedicated staging project")
    validated_url = _validate_staging_health_url(health_url)
    if not isinstance(expected_sha, str) or SHA_PATTERN.fullmatch(expected_sha) is None:
        raise AuthorizationError("expected SHA must be an exact 40-character lowercase SHA")
    if (
        not isinstance(contractor_id, str)
        or CONTRACTOR_ID_PATTERN.fullmatch(contractor_id.strip()) is None
    ):
        raise AuthorizationError("contractor ID must be one safe document segment")
    if not isinstance(caller_identifier, str) or not caller_identifier.strip():
        raise AuthorizationError("caller identifier is required")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise AuthorizationError("TTL must be an integer")
    if not 60 <= ttl_seconds <= 3_600:
        raise AuthorizationError("TTL must be between 60 and 3600 seconds")
    observed_now = int(time.time() if now is None else now)
    if observed_now <= 0:
        raise AuthorizationError("current time must be positive")
    caller_digest = _caller_hmac_digest(caller_identifier, hmac_key)

    normalized_contractor = contractor_id.strip()
    return EnablementPlan(
        project=project,
        health_url=validated_url,
        expected_sha=expected_sha,
        contractor_id=normalized_contractor,
        contractor_label=_contractor_label(normalized_contractor),
        ttl_seconds=ttl_seconds,
        fields={
            "receptionist_observation_shadow_enabled": True,
            "receptionist_observation_shadow_authorized_sha": expected_sha,
            "receptionist_observation_shadow_expires_at": observed_now + ttl_seconds,
            "receptionist_observation_shadow_caller_digests": [caller_digest],
            "receptionist_observation_shadow_authorized_at": observed_now,
        },
    )


def verify_staging_health(plan: EnablementPlan, health: object) -> None:
    if not isinstance(health, dict):
        raise AuthorizationError("staging health response must be an object")
    if health.get("environment") != "staging" or health.get("service") != (
        "kevin-api-staging"
    ):
        raise AuthorizationError("staging identity mismatch")
    if health.get("deploy_sha") != plan.expected_sha:
        raise AuthorizationError("staging deploy SHA mismatch")


def _document_for(firestore_client, contractor_id: str):
    document = firestore_client.collection("contractors").document(contractor_id)
    snapshot = document.get()
    if not getattr(snapshot, "exists", False):
        raise AuthorizationError("staging contractor does not exist")
    return document


def apply_enablement(plan: EnablementPlan, firestore_client) -> dict[str, Any]:
    document = _document_for(firestore_client, plan.contractor_id)
    document.update(plan.fields)
    summary = plan.redacted_summary()
    summary["status"] = "enabled"
    summary["writes_authorized"] = True
    return summary


def apply_disable(
    *,
    contractor_id: str,
    firestore_client,
    delete_field,
) -> dict[str, Any]:
    if (
        not isinstance(contractor_id, str)
        or CONTRACTOR_ID_PATTERN.fullmatch(contractor_id.strip()) is None
    ):
        raise AuthorizationError("contractor ID must be one safe document segment")
    normalized = contractor_id.strip()
    document = _document_for(firestore_client, normalized)
    document.update(
        {
            "receptionist_observation_shadow_enabled": False,
            "receptionist_observation_shadow_authorized_sha": delete_field,
            "receptionist_observation_shadow_expires_at": delete_field,
            "receptionist_observation_shadow_caller_digests": delete_field,
            "receptionist_observation_shadow_authorized_at": delete_field,
        }
    )
    return {
        "status": "disabled",
        "project": STAGING_PROJECT_ID,
        "contractor_label": _contractor_label(normalized),
        "writes_authorized": True,
    }


def _read_caller_identifier() -> str:
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass("Synthetic test caller identifier: ").strip()
    return sys.stdin.readline().strip()


def _fetch_health(url: str) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(  # noqa: S310 - HTTPS validated by plan construction
            request,
            timeout=10,
        ) as response:
            if response.geturl() != url:
                raise AuthorizationError("staging health request redirected")
            body = response.read(64 * 1024 + 1)
    except (HTTPError, URLError, OSError) as exc:
        raise AuthorizationError("staging health request failed") from exc
    if len(body) > 64 * 1024:
        raise AuthorizationError("staging health response is too large")
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorizationError("staging health response is invalid") from exc
    if not isinstance(decoded, dict):
        raise AuthorizationError("staging health response must be an object")
    return decoded


def _add_enablement_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--contractor-id", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan", help="validate and print a redacted plan")
    _add_enablement_arguments(plan)
    enable = subparsers.add_parser("enable", help="verify health and write authorization")
    _add_enablement_arguments(enable)
    enable.add_argument("--confirm", required=True)
    disable = subparsers.add_parser("disable", help="remove contractor authorization")
    disable.add_argument("--project", required=True)
    disable.add_argument("--contractor-id", required=True)
    disable.add_argument("--confirm", required=True)
    return parser


def _plan_from_args(args: argparse.Namespace) -> EnablementPlan:
    key = os.getenv(HMAC_KEY_ENV, "")
    return build_enablement_plan(
        project=args.project,
        health_url=args.health_url,
        expected_sha=args.expected_sha,
        contractor_id=args.contractor_id,
        caller_identifier=_read_caller_identifier(),
        hmac_key=key,
        ttl_seconds=args.ttl_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            print(json.dumps(_plan_from_args(args).redacted_summary(), sort_keys=True))
            return 0

        if args.project != STAGING_PROJECT_ID:
            raise AuthorizationError("project must be the dedicated staging project")
        if args.command == "disable":
            if args.confirm != DISABLE_CONFIRMATION:
                raise AuthorizationError("disable confirmation does not match")
            from google.cloud import firestore

            client = firestore.Client(project=args.project)
            summary = apply_disable(
                contractor_id=args.contractor_id,
                firestore_client=client,
                delete_field=firestore.DELETE_FIELD,
            )
        else:
            if args.confirm != ENABLE_CONFIRMATION:
                raise AuthorizationError("enable confirmation does not match")
            plan = _plan_from_args(args)
            verify_staging_health(plan, _fetch_health(plan.health_url))
            from google.cloud import firestore

            client = firestore.Client(project=args.project)
            summary = apply_enablement(plan, client)
        print(json.dumps(summary, sort_keys=True))
        return 0
    except AuthorizationError as error:
        print(
            json.dumps({"status": "blocked", "reason": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "exception_type": type(error).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
