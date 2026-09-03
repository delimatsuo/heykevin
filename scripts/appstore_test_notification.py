#!/usr/bin/env python3
"""Ask Apple to send a TEST App Store Server Notification and report the result.

Owner-run. Reads the App Store Server API credentials from the production
Cloud Run service (kevin-api) with your own gcloud identity, signs the API
JWT locally, calls Apple's "Request a Test Notification" endpoint, then
polls "Get Test Notification Status" until Apple reports the delivery
attempt. Nothing is written anywhere and no secret is printed.

Apple docs:
  POST https://api.storekit.apple.com/inApps/v1/notifications/test
  GET  https://api.storekit.apple.com/inApps/v1/notifications/test/{token}
  (sandbox host: api.storekit-sandbox.apple.com)

Run from the repo root:

    .venv/bin/python scripts/appstore_test_notification.py            # production
    .venv/bin/python scripts/appstore_test_notification.py --staging  # sandbox -> staging URL

Expected on success: sendAttemptResult == "SUCCESS" from Apple, and in Cloud
Logging a 200 on /webhooks/appstore/notifications with the handler line
"App Store notification: TEST" (followed by a benign warning that the TEST
payload carries no transaction, which the handler ignores).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

import jwt  # PyJWT[crypto]

PROD_HOST = "https://api.storekit.apple.com"
SANDBOX_HOST = "https://api.storekit-sandbox.apple.com"


def cloud_run_env(service: str) -> dict[str, str]:
    out = subprocess.check_output(
        [
            "gcloud", "run", "services", "describe", service,
            "--region", "us-central1", "--project", "kevin-491315", "--format", "json",
        ],
        text=True,
    )
    spec = json.loads(out)["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", "") for e in spec.get("env", []) if "value" in e}


def make_jwt(env: dict[str, str]) -> str:
    key = env["APPSTORE_PRIVATE_KEY"]
    if "|" in key:
        key = key.replace("|", "\n")
    elif "\\n" in key:
        key = key.replace("\\n", "\n")
    now = int(time.time())
    return jwt.encode(
        {
            "iss": env["APPSTORE_ISSUER_ID"],
            "iat": now,
            "exp": now + 1200,
            "aud": "appstoreconnect-v1",
            "bid": env["APPSTORE_BUNDLE_ID"],
        },
        key,
        algorithm="ES256",
        headers={"kid": env["APPSTORE_KEY_ID"]},
    )


def call(method: str, url: str, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode() or "{}"
            return r.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staging", action="store_true", help="use the sandbox host (delivers to the sandbox/staging URL)")
    args = ap.parse_args()

    service = "kevin-api-staging" if args.staging else "kevin-api"
    host = SANDBOX_HOST if args.staging else PROD_HOST
    env = cloud_run_env(service)
    missing = [k for k in ("APPSTORE_KEY_ID", "APPSTORE_ISSUER_ID", "APPSTORE_BUNDLE_ID", "APPSTORE_PRIVATE_KEY") if not env.get(k)]
    if missing:
        print(f"missing on {service}: {missing}", file=sys.stderr)
        return 2
    print(f"service={service} key_id={env['APPSTORE_KEY_ID']} bundle={env['APPSTORE_BUNDLE_ID']} host={host}")

    token = make_jwt(env)
    status, body = call("POST", f"{host}/inApps/v1/notifications/test", token)
    print(f"request test notification -> HTTP {status} {json.dumps(body)[:200]}")
    if status != 200 or "testNotificationToken" not in body:
        return 1
    test_token = body["testNotificationToken"]

    for attempt in range(12):
        time.sleep(10)
        status, body = call("GET", f"{host}/inApps/v1/notifications/test/{test_token}", token)
        attempts = body.get("sendAttempts") or []
        results = [(a.get("attemptDate"), a.get("sendAttemptResult")) for a in attempts]
        print(f"poll {attempt + 1}: HTTP {status} sendAttempts={results}")
        if any(r == "SUCCESS" for _, r in results):
            print("RESULT: Apple reports SUCCESS — the webhook accepted a genuine Apple-signed notification.")
            return 0
        if attempts and all(r not in (None, "SUCCESS") for _, r in results) and len(attempts) >= 1 and attempt >= 3:
            print("RESULT: Apple reports a failed delivery; check the Cloud Run log for the matching request.")
            return 1
    print("RESULT: no delivery attempt reported yet; re-run the status check later with the same token:", test_token)
    return 1


if __name__ == "__main__":
    sys.exit(main())
