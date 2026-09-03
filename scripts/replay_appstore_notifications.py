#!/usr/bin/env python3
"""Owner-run replay of missed App Store Server Notifications.

Every Apple notification was answered HTTP 400 for 30+ days (fixed by PR
#215 on 2026-09-02). Apple retries a failed delivery for 72 hours only, so
any renewal, expiration, cancellation or refund that fired before the fix
never reached Firestore. Apple's Get Notification History endpoint keeps
**180 days** of history regardless of delivery outcome. This script fetches
that history, verifies each signedPayload with the same verifier the
production webhook uses, prints a PII-free summary line per notification,
and totals. It never writes anything unless you pass ``--apply``.

**Always dry-run first, then re-run with --apply once the totals look
right.** Applying replays each verified payload through the same handler
the live webhook calls, which requires Firestore access (Application
Default Credentials) because the handler writes contractor and
apple_transactions records. Applying is idempotent per transaction: the
handler dedupes through the ``apple_transactions`` collection, so re-running
with --apply on already-applied notifications is safe.

    # Dry run (default): fetch, verify, print, apply nothing.
    .venv/bin/python scripts/replay_appstore_notifications.py \\
        --environment production --days 180

    # Apply once the dry-run totals look right. Needs ADC for Firestore.
    .venv/bin/python scripts/replay_appstore_notifications.py \\
        --environment production --days 180 --apply

Credentials: reads the same APPSTORE_KEY_ID / APPSTORE_ISSUER_ID /
APPSTORE_PRIVATE_KEY / APPSTORE_BUNDLE_ID env vars as the production
service. ``--from-cloud-run SERVICE`` copies them (plus APPSTORE_ENVIRONMENT
and FIRESTORE_PROJECT_ID) from a live Cloud Run service's env into this
process instead of requiring them locally -- it never prints a value.

Exit codes: 0 success, 1 API/verification-infrastructure error, 2
configuration error (missing credentials, bad dates).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# app.config.settings freezes at first import of any app.* module, so the
# sys.path insertion happens here (safe -- no app.* import yet) and every
# app.* import is deferred until after --from-cloud-run has had a chance to
# populate os.environ. Mirrors scripts/reconcile_appstore_subscriptions.py.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MAX_HISTORY_DAYS = 180
CLOUD_RUN_REGION = "us-central1"
CLOUD_RUN_PROJECT = "kevin-491315"
CLOUD_RUN_ENV_KEYS = (
    "APPSTORE_KEY_ID",
    "APPSTORE_ISSUER_ID",
    "APPSTORE_PRIVATE_KEY",
    "APPSTORE_BUNDLE_ID",
    "APPSTORE_ENVIRONMENT",
    "FIRESTORE_PROJECT_ID",
)

# Lazily bound to app.services.appstore_replay's real implementations inside
# main(), after any --from-cloud-run env population. Tests monkeypatch these
# module attributes directly (before calling main()) to exercise the CLI
# without ever importing app.* or touching the network.
fetch_notification_history = None
replay = None


def _populate_env_from_cloud_run(service: str) -> None:
    """Copy App Store / Firestore env vars from a live Cloud Run service.

    Only fills in keys not already present in os.environ. Never prints any
    value -- only the fact that credentials were sourced from Cloud Run.
    """
    out = subprocess.check_output(
        [
            "gcloud", "run", "services", "describe", service,
            "--region", CLOUD_RUN_REGION, "--project", CLOUD_RUN_PROJECT,
            "--format", "json",
        ],
        text=True,
    )
    spec = json.loads(out)["spec"]["template"]["spec"]["containers"][0]
    remote_env = {e["name"]: e.get("value", "") for e in spec.get("env", []) if "value" in e}
    for key in CLOUD_RUN_ENV_KEYS:
        if key in remote_env and key not in os.environ:
            os.environ[key] = remote_env[key]
    print(f"credentials sourced from Cloud Run service={service} (values not printed)")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help=f"How many days of history to fetch, up to {MAX_HISTORY_DAYS} (default 180). "
             "Mutually exclusive with --start/--end.",
    )
    parser.add_argument("--start", default=None, help="Window start, YYYY-MM-DD (UTC). Requires --end.")
    parser.add_argument("--end", default=None, help="Window end, YYYY-MM-DD (UTC). Requires --start.")
    parser.add_argument(
        "--all", action="store_true",
        help="Fetch all notifications, not just ones Apple failed to deliver "
             "(default: onlyFailures=true).",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply each verified notification through the live handler (default: dry run).",
    )
    parser.add_argument(
        "--environment", choices=("production", "sandbox"), default=None,
        help="App Store environment (default: $APPSTORE_ENVIRONMENT).",
    )
    parser.add_argument(
        "--from-cloud-run", metavar="SERVICE", default=None,
        help="Source APPSTORE_*/FIRESTORE_PROJECT_ID credentials from this Cloud Run "
             "service's env before doing anything else. Never prints a value.",
    )
    return parser


def _parse_ymd(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _resolve_window(args: argparse.Namespace, now: datetime) -> tuple[datetime, datetime] | None:
    """Return (start, end) as timezone-aware UTC datetimes, or None on bad input (caller exits 2)."""
    if args.start or args.end:
        if not (args.start and args.end):
            print("error: --start and --end must be given together", file=sys.stderr)
            return None
        try:
            start_dt = _parse_ymd(args.start)
            end_dt = _parse_ymd(args.end)
        except ValueError as e:
            print(f"error: invalid date (expected YYYY-MM-DD): {e}", file=sys.stderr)
            return None
        if start_dt > end_dt:
            print("error: --start must not be after --end", file=sys.stderr)
            return None
        if (now - start_dt).days > MAX_HISTORY_DAYS:
            print(f"error: --start must be within the past {MAX_HISTORY_DAYS} days", file=sys.stderr)
            return None
        return start_dt, end_dt

    days = args.days if args.days is not None else MAX_HISTORY_DAYS
    if days <= 0 or days > MAX_HISTORY_DAYS:
        print(f"error: --days must be between 1 and {MAX_HISTORY_DAYS}", file=sys.stderr)
        return None
    return now - timedelta(days=days), now


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    window = _resolve_window(args, now)
    if window is None:
        return 2
    start_dt, end_dt = window
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    if args.from_cloud_run:
        try:
            _populate_env_from_cloud_run(args.from_cloud_run)
        except Exception as e:  # noqa: BLE001
            print(f"error: could not source credentials from Cloud Run: {type(e).__name__}", file=sys.stderr)
            return 2

    environment = args.environment or os.environ.get("APPSTORE_ENVIRONMENT", "")
    if environment not in ("production", "sandbox"):
        print(
            "error: --environment must be production or sandbox "
            "(or set APPSTORE_ENVIRONMENT)",
            file=sys.stderr,
        )
        return 2

    global fetch_notification_history, replay
    if fetch_notification_history is None or replay is None:
        from app.config import settings
        missing = [
            name for name, value in (
                ("APPSTORE_KEY_ID", settings.appstore_key_id),
                ("APPSTORE_ISSUER_ID", settings.appstore_issuer_id),
                ("APPSTORE_PRIVATE_KEY", settings.appstore_private_key),
                ("APPSTORE_BUNDLE_ID", settings.appstore_bundle_id),
            ) if not value
        ]
        if missing:
            print(f"error: missing App Store credentials: {', '.join(missing)}", file=sys.stderr)
            return 2

        from app.services.appstore_replay import (
            fetch_notification_history as _fetch,
            replay as _replay,
        )
        if fetch_notification_history is None:
            fetch_notification_history = _fetch
        if replay is None:
            replay = _replay

    from app.services.subscription import APPSTORE_PRODUCTION_URL, APPSTORE_SANDBOX_URL
    base_url = APPSTORE_PRODUCTION_URL if environment == "production" else APPSTORE_SANDBOX_URL

    only_failures = not args.all
    mode = "APPLY" if args.apply else "DRY RUN"
    print(
        f"window: {start_dt.date().isoformat()} .. {end_dt.date().isoformat()} (UTC)  "
        f"environment={environment}  onlyFailures={only_failures}  mode={mode}"
    )

    try:
        items = asyncio.run(
            fetch_notification_history(
                base_url=base_url,
                start_ms=start_ms,
                end_ms=end_ms,
                only_failures=only_failures,
            )
        )
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    report = asyncio.run(replay(items, apply=args.apply))

    print("-" * 72)
    print(f"fetched={report.fetched}  rejected={report.rejected}")
    if report.by_type:
        by_type = "  ".join(f"{k}={v}" for k, v in sorted(report.by_type.items()))
        print(f"by_type: {by_type}")
    if args.apply:
        print(
            f"applied={report.applied}  handler_false={report.handler_false}  "
            f"handler_error={report.handler_error}"
        )
    else:
        print(f"dry_run={report.dry_run} (nothing was applied -- re-run with --apply)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
