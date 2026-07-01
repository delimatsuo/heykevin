#!/usr/bin/env python3
"""Seed and smoke-test Phase 0 safety behavior against staging.

The script is intentionally staging-only. It creates or refreshes one disposable
contractor document, calls safe deployed API surfaces with that contractor's
scoped token, and can run synthetic mutable checks only when the deployed SHA
matches the expected PR SHA.

It must not print generated tokens, phone numbers, customer data, transcripts,
or service environment variables.
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

import requests
from google.cloud import firestore


DEFAULT_STAGING_URL = "https://kevin-api-staging-l63rergg7a-uc.a.run.app"
DEFAULT_STAGING_PROJECT = "kevin-staging-491315"
DEFAULT_CONTRACTOR_ID = "codex_phase0_smoke"
PRODUCTION_PROJECT = "kevin-491315"
PRODUCTION_URL = "https://kevin-api-752910912062.us-central1.run.app"

HTTP_TIMEOUT_SECONDS = 20
SYNTHETIC_CALL_SID = "CAcodexphase0smoke000000000000000001"
SYNTHETIC_OTHER_CALL_SID = "CAcodexphase0other0000000000000001"


@dataclass
class Step:
    name: str
    status: str
    detail: str = ""


class SmokeFailure(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_STAGING_URL)
    parser.add_argument("--project", default=DEFAULT_STAGING_PROJECT)
    parser.add_argument("--contractor-id", default=DEFAULT_CONTRACTOR_ID)
    parser.add_argument("--expected-sha", default="")
    parser.add_argument(
        "--require-expected-sha",
        action="store_true",
        help="fail if /health deploy_sha does not match --expected-sha",
    )
    parser.add_argument(
        "--mutable-checks",
        action="store_true",
        help="run synthetic gate/ownership checks that write staging-only RTDB/Firestore docs",
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="staging Firebase RTDB URL; required for synthetic text-reply gate smoke",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete the seeded contractor and synthetic smoke docs at the end",
    )
    return parser.parse_args()


def fail_if_not_staging(project: str, base_url: str, database_url: str = "") -> None:
    normalized_url = base_url.rstrip("/")
    if project == PRODUCTION_PROJECT:
        raise SmokeFailure("refusing to run against production Firestore project")
    if normalized_url == PRODUCTION_URL or "staging" not in normalized_url:
        raise SmokeFailure("refusing to run against a non-staging base URL")
    if database_url and "staging" not in database_url:
        raise SmokeFailure("refusing to run against a non-staging RTDB URL")


def print_step(step: Step) -> None:
    suffix = f" - {step.detail}" if step.detail else ""
    print(f"{step.status}: {step.name}{suffix}")


def request_json(
    method: str,
    base_url: str,
    path: str,
    *,
    token: str = "",
    payload: dict[str, Any] | None = None,
    expected_status: int = 200,
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.request(
        method,
        f"{base_url.rstrip('/')}{path}",
        headers=headers,
        json=payload,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != expected_status:
        detail = body.get("detail") or body.get("error") or body.get("message") or "unexpected response"
        raise SmokeFailure(f"{method} {path} returned HTTP {response.status_code}: {detail}")
    return response.status_code, body


def generate_contractor_token(contractor_id: str) -> tuple[str, str]:
    secret = secrets.token_urlsafe(32)
    raw_token = f"kv_ct_{contractor_id[:8]}_{secret}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, token_hash


def seed_contractor(db: firestore.Client, contractor_id: str) -> str:
    raw_token, token_hash = generate_contractor_token(contractor_id)
    now = time.time()
    doc_ref = db.collection("contractors").document(contractor_id)
    snapshot = doc_ref.get()
    existing = snapshot.to_dict() if snapshot.exists else {}

    data: dict[str, Any] = {
        "active": True,
        "api_token_hash": token_hash,
        "apple_user_id": f"{contractor_id}_apple_user",
        "business_address": "Staging only",
        "business_city": "Staging",
        "business_country_name": "United States",
        "business_hours_end": "18:00",
        "business_hours_start": "07:00",
        "business_name": "Codex Phase 0 Smoke",
        "calendar_type": "none",
        "callback_sla_minutes": 15,
        "codex_managed": True,
        "codex_purpose": "phase0_staging_smoke",
        "country_code": "US",
        "created_at": existing.get("created_at", now),
        "deleted_app_detected_at": None,
        "dial_in_pin": "000000",
        "home_base_address": "Staging only",
        "mode": "business",
        "owner_name": "Codex Smoke",
        "owner_phone": "+15005550006",
        "service_area_zips": ["94105"],
        "service_fee_cents": 0,
        "service_type": "plumbing",
        "subscription_expires": now + 7 * 86400,
        "subscription_status": "trial",
        "subscription_tier": "business",
        "subscription_uuid": existing.get("subscription_uuid") or str(uuid.uuid4()),
        "timezone": "America/Los_Angeles",
        "twilio_number": "",
        "updated_at": now,
        "voice_engine": "elevenlabs",
    }
    doc_ref.set(data, merge=True)

    # Reset safety gates to the default-off release posture on every run.
    doc_ref.update(
        {
            "automation_approvals": firestore.DELETE_FIELD,
            "gated_actions": firestore.DELETE_FIELD,
            "google_calendar_access_token": firestore.DELETE_FIELD,
            "google_calendar_refresh_token": firestore.DELETE_FIELD,
            "integration_write_status": firestore.DELETE_FIELD,
            "jobber_access_token": firestore.DELETE_FIELD,
            "jobber_refresh_token": firestore.DELETE_FIELD,
            "sms_compliance_status": firestore.DELETE_FIELD,
        }
    )
    return raw_token


def cleanup_firestore(db: firestore.Client, contractor_id: str) -> None:
    doc_ref = db.collection("contractors").document(contractor_id)
    snapshot = doc_ref.get()
    if snapshot.exists and snapshot.to_dict().get("codex_managed") is True:
        prefs = doc_ref.collection("settings").document("preferences")
        prefs.delete()
        doc_ref.delete()

    for collection, doc_id in (
        ("calls", SYNTHETIC_OTHER_CALL_SID),
        ("calls", SYNTHETIC_CALL_SID),
    ):
        db.collection(collection).document(doc_id).delete()

    estimates = db.collection("estimates").where("contractor_id", "==", contractor_id).limit(20).stream()
    for estimate in estimates:
        data = estimate.to_dict()
        if data.get("codex_managed") is True or data.get("call_sid") == SYNTHETIC_CALL_SID:
            estimate.reference.delete()


def health_check(base_url: str, expected_sha: str, require_expected_sha: bool) -> tuple[dict[str, Any], bool]:
    _, body = request_json("GET", base_url, "/health")
    if body.get("status") != "ok":
        raise SmokeFailure("/health did not return status=ok")
    if body.get("environment") != "staging":
        raise SmokeFailure("/health did not report environment=staging")

    deployed_sha = str(body.get("deploy_sha") or "")
    sha_matches = not expected_sha or deployed_sha == expected_sha
    if expected_sha and not sha_matches and require_expected_sha:
        raise SmokeFailure("staging deploy_sha does not match expected SHA")
    return body, sha_matches


def read_only_api_smoke(base_url: str, contractor_id: str, token: str) -> list[Step]:
    steps: list[Step] = []

    _, profile = request_json("GET", base_url, f"/api/contractors/{contractor_id}", token=token)
    if profile.get("contractor_id") != contractor_id:
        raise SmokeFailure("contractor profile response did not match seeded contractor")
    if "api_token_hash" in profile:
        raise SmokeFailure("contractor profile leaked api_token_hash")
    steps.append(Step("scoped contractor profile", "PASS"))

    request_json("GET", base_url, "/api/contractors/not_the_owner", token=token, expected_status=403)
    steps.append(Step("cross-contractor profile denied", "PASS"))

    _, active = request_json("GET", base_url, f"/api/active-call?contractor_id={contractor_id}", token=token)
    if active.get("active") is not False:
        raise SmokeFailure("active-call response should be inactive before synthetic calls")
    steps.append(Step("active-call empty state", "PASS"))

    _, calls = request_json("GET", base_url, f"/api/calls?contractor_id={contractor_id}", token=token)
    if not isinstance(calls.get("calls"), list):
        raise SmokeFailure("calls endpoint did not return a list")
    steps.append(Step("calls work queue", "PASS", f"count={calls.get('count', len(calls.get('calls', [])))}"))

    _, jobs = request_json("GET", base_url, f"/api/jobs?contractor_id={contractor_id}", token=token)
    if not isinstance(jobs.get("jobs"), list):
        raise SmokeFailure("jobs endpoint did not return a list")
    steps.append(Step("jobs work queue", "PASS", f"count={len(jobs.get('jobs', []))}"))

    _, settings = request_json("GET", base_url, f"/api/settings?contractor_id={contractor_id}", token=token)
    if "text_reply_message" not in settings:
        raise SmokeFailure("settings endpoint did not return defaults")
    steps.append(Step("settings defaults", "PASS"))

    _, jobber = request_json(
        "GET",
        base_url,
        f"/api/integrations/jobber/status?contractor_id={contractor_id}",
        token=token,
    )
    if jobber.get("connected") is not False:
        raise SmokeFailure("jobber status should be disconnected")
    steps.append(Step("jobber status disconnected", "PASS"))

    _, calendar = request_json(
        "GET",
        base_url,
        f"/api/integrations/google-calendar/status?contractor_id={contractor_id}",
        token=token,
    )
    if calendar.get("connected") is not False:
        raise SmokeFailure("google calendar status should be disconnected")
    steps.append(Step("google calendar status disconnected", "PASS"))

    return steps


def seed_cross_tenant_call(db: firestore.Client) -> None:
    db.collection("calls").document(SYNTHETIC_OTHER_CALL_SID).set(
        {
            "call_sid": SYNTHETIC_OTHER_CALL_SID,
            "contractor_id": "codex_phase0_other_owner",
            "timestamp": time.time(),
            "read": False,
            "codex_managed": True,
        },
        merge=True,
    )


def mutable_firestore_gate_smoke(
    db: firestore.Client,
    base_url: str,
    contractor_id: str,
    token: str,
) -> list[Step]:
    steps: list[Step] = []

    _, estimate_denied = request_json(
        "POST",
        base_url,
        "/api/estimates/create-token",
        token=token,
        payload={
            "contractor_id": contractor_id,
            "caller_phone": "+15005550006",
            "call_sid": SYNTHETIC_CALL_SID,
        },
        expected_status=403,
    )
    detail = estimate_denied.get("detail") or {}
    if detail.get("reason") != "feature_disabled":
        raise SmokeFailure("estimate create-token was not denied by the feature gate")
    steps.append(Step("estimate token gate disabled", "PASS"))

    seed_cross_tenant_call(db)
    request_json(
        "POST",
        base_url,
        f"/api/call-action?contractor_id={contractor_id}",
        token=token,
        payload={"call_sid": SYNTHETIC_OTHER_CALL_SID, "action": "decline"},
        expected_status=403,
    )
    steps.append(Step("cross-tenant call action denied", "PASS"))

    return steps


def mutable_text_reply_smoke(
    base_url: str,
    database_url: str,
    contractor_id: str,
    token: str,
) -> Step:
    if not database_url:
        return Step("text reply gate disabled", "SKIP", "no staging RTDB URL provided")

    try:
        import firebase_admin
        from firebase_admin import db as rtdb
    except ImportError:
        return Step("text reply gate disabled", "SKIP", "firebase_admin unavailable")

    app_name = f"phase0-smoke-{int(time.time())}"
    try:
        app = firebase_admin.initialize_app(None, {"databaseURL": database_url}, name=app_name)
        ref = rtdb.reference(f"/active_calls/{SYNTHETIC_CALL_SID}", app=app)
        ref.set(
            {
                "call_sid": SYNTHETIC_CALL_SID,
                "caller_phone": "+15005550006",
                "state": "screening",
                "contractor_id": contractor_id,
                "state_updated_at": time.time(),
                "transcript_buffer": "synthetic staging smoke",
            }
        )
        _, body = request_json(
            "POST",
            base_url,
            f"/api/call-action?contractor_id={contractor_id}",
            token=token,
            payload={
                "call_sid": SYNTHETIC_CALL_SID,
                "action": "text_reply",
                "message": "Synthetic staging smoke.",
            },
        )
        if body.get("status") != "error" or "not enabled" not in str(body.get("message", "")):
            raise SmokeFailure("text reply was not denied by the account gate")
        return Step("text reply gate disabled", "PASS")
    except SmokeFailure:
        raise
    except Exception as exc:
        return Step("text reply gate disabled", "SKIP", f"RTDB unavailable: {exc.__class__.__name__}")
    finally:
        try:
            ref.delete()  # type: ignore[name-defined]
        except Exception:
            pass
        try:
            firebase_admin.delete_app(app)  # type: ignore[name-defined]
        except Exception:
            pass


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    steps: list[Step] = []

    try:
        fail_if_not_staging(args.project, base_url, args.database_url)
        db = firestore.Client(project=args.project)

        health, sha_matches = health_check(
            base_url,
            args.expected_sha,
            args.require_expected_sha,
        )
        steps.append(
            Step(
                "staging health",
                "PASS",
                f"revision={health.get('revision', 'unknown')} sha_match={sha_matches}",
            )
        )

        token = seed_contractor(db, args.contractor_id)
        steps.append(Step("staging contractor seeded", "PASS", f"contractor_id={args.contractor_id}"))

        steps.extend(read_only_api_smoke(base_url, args.contractor_id, token))

        if args.mutable_checks:
            if not args.expected_sha:
                steps.append(
                    Step(
                        "mutable safety checks",
                        "SKIP",
                        "pass --expected-sha to prove staging is running the intended revision",
                    )
                )
            elif not sha_matches:
                steps.append(
                    Step(
                        "mutable safety checks",
                        "SKIP",
                        "deployed SHA does not match expected SHA",
                    )
                )
            else:
                steps.extend(mutable_firestore_gate_smoke(db, base_url, args.contractor_id, token))
                steps.append(
                    mutable_text_reply_smoke(
                        base_url,
                        args.database_url,
                        args.contractor_id,
                        token,
                    )
                )
        else:
            steps.append(Step("mutable safety checks", "SKIP", "pass --mutable-checks after staging deploy"))

        if args.cleanup:
            cleanup_firestore(db, args.contractor_id)
            steps.append(Step("staging cleanup", "PASS"))

        for step in steps:
            print_step(step)

        failures = [step for step in steps if step.status == "FAIL"]
        return 1 if failures else 0
    except SmokeFailure as exc:
        for step in steps:
            print_step(step)
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
