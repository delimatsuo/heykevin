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
right.** Dry runs need read-only Firestore access (Application Default
Credentials) too -- see the stale-deactivation guard below. Applying
additionally writes: it replays each verified payload through the same
handler the live webhook calls, which writes contractor and
apple_transactions records.

**Applying is NOT a general notification dedupe.** ``claim_transaction``
(inside the handler) is an ownership *binding*, not a replay guard: once a
contractor already owns a given original_transaction_id, a repeat
notification for it is a same-contractor no-op for that binding, but the
handler still runs its full state transition every time it is invoked --
including, for EXPIRED / DID_FAIL_TO_RENEW / REFUND / REVOKE, an
unconditional "mark this contractor expired" write and a re-sent "your
subscription has ended" push. This script includes one targeted guard for
the worst case of that: a deactivating notification whose implicated term
has already been superseded by a newer paid term on file is skipped (logged
as "STALE ... skipped", counted separately, never applied) in both dry-run
and --apply. That guard covers exactly one regression, not general
idempotency -- **run --apply once per incident, read the totals, and stop.**
Re-running --apply on the same window re-does every non-stale write and
re-sends every non-stale expiry push again.

``--all`` (fetch everything in the window, not just onlyFailures=true) is
higher risk to combine with --apply than the default: it pulls in
notifications Apple already delivered successfully the first time, which
means the deactivating writes/pushes above fire again for those too. Use it
only to double-check a period's completeness, and dry-run it first.

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

``--start``/``--end`` are both inclusive (UTC calendar days); ``--days`` and
``--start``/``--end`` are mutually exclusive.

Exit codes: 0 success, 1 API/verification-infrastructure error, 2
configuration error (missing credentials, bad dates, conflicting date flags).
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

import httpx

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
             "Mutually exclusive with --start/--end (exit 2 if both are given).",
    )
    parser.add_argument(
        "--start", default=None,
        help="Window start, YYYY-MM-DD (UTC), inclusive. Requires --end. Mutually exclusive with --days.",
    )
    parser.add_argument(
        "--end", default=None,
        help="Window end, YYYY-MM-DD (UTC), inclusive (covers the whole day). Requires --start. "
             "Mutually exclusive with --days.",
    )
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
    parser.add_argument(
        "--debug", action="store_true",
        help="Re-raise the original exception after reporting, for a full traceback. "
             "May print the exception's own message, so use it locally rather than "
             "pasting the output anywhere.",
    )
    return parser


def _parse_ymd(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _resolve_window(args: argparse.Namespace, now: datetime) -> tuple[datetime, datetime] | None:
    """Return (start, end) as timezone-aware UTC datetimes, or None on bad input (caller exits 2).

    --start/--end are both inclusive UTC calendar days, so --end is
    resolved to the instant *after* that day ends (start of the next day)
    -- otherwise a notification signed on the --end date itself would be
    silently excluded from the window.
    """
    if args.days is not None and (args.start or args.end):
        print("error: --days is mutually exclusive with --start/--end", file=sys.stderr)
        return None

    if args.start or args.end:
        if not (args.start and args.end):
            print("error: --start and --end must be given together", file=sys.stderr)
            return None
        try:
            start_dt = _parse_ymd(args.start)
            end_dt_raw = _parse_ymd(args.end)
        except ValueError as e:
            print(f"error: invalid date (expected YYYY-MM-DD): {e}", file=sys.stderr)
            return None
        if start_dt > end_dt_raw:
            print("error: --start must not be after --end", file=sys.stderr)
            return None
        if (now - start_dt).days > MAX_HISTORY_DAYS:
            print(f"error: --start must be within the past {MAX_HISTORY_DAYS} days", file=sys.stderr)
            return None
        # Inclusive end: cover the entire --end calendar day.
        end_dt = end_dt_raw + timedelta(days=1)
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
    # --start/--end covers whole UTC calendar days -- "inclusive" is
    # accurate there. --days ends at the current instant (see
    # _resolve_window: it returns `now` itself as the end), which is not a
    # whole day, so that banner says "now" plus the actual timestamp
    # instead of claiming "inclusive".
    explicit_dates = bool(args.start and args.end)
    if explicit_dates:
        # end_dt is an exclusive upper bound (the instant after the last
        # inclusive day ends) -- step back a moment before printing so the
        # displayed window shows the calendar day the caller actually asked for.
        display_end_date = (end_dt - timedelta(microseconds=1)).date().isoformat()
        window_desc = f"{start_dt.date().isoformat()} .. {display_end_date} (UTC, inclusive)"
    else:
        # start_dt is `now - N days` at the current time of day, not midnight
        # -- a bare date here would silently make the printed window look
        # like it covers the whole boundary day when --days actually
        # excludes its oldest hours. Print both ends as exact instants.
        start_iso = start_dt.isoformat().replace("+00:00", "Z")
        now_iso = end_dt.isoformat().replace("+00:00", "Z")
        window_desc = f"{start_iso} .. now ({now_iso}) (UTC)"
    print(
        f"window: {window_desc}  "
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
    except Exception as exc:  # noqa: BLE001 - every fetch failure (a non-200 from
        # Apple, a connection/DNS failure, a credentials problem raised while
        # signing the request, ...) must exit 1 with a usable message by default,
        # never a raw traceback -- see the runbook's "If the dry run fails to
        # connect" section. Which details are safe to print depends on which of
        # these this is; see the three branches below. --debug re-raises (bare
        # `raise`, preserving the original traceback) after those messages are
        # printed, for whichever branch this was -- not only the credential one,
        # since a transport or Apple-status failure can want a traceback too.
        if isinstance(exc, RuntimeError):
            # fetch_notification_history's own message already names Apple's
            # HTTP status and a truncated response body -- unchanged from
            # before this guard widened. That body comes straight from Apple
            # and is not filtered for PII by this script, same as always.
            print(f"error: {exc}", file=sys.stderr)
        elif isinstance(exc, (httpx.TransportError, OSError)):
            # A genuine connection/DNS/TLS/timeout failure -- the most likely
            # real-world cause is a wrong App Store host constant. These
            # exception messages are the diagnostic here and are safe to
            # print in full: they describe a socket/TLS/DNS failure and never
            # carry the Authorization header, the token, or a transaction id.
            print(
                f"error: Fetch failed against {base_url} ({environment}): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            print(
                "error: a name-resolution or connection error here most likely means "
                "the App Store host constant is wrong for this endpoint -- check "
                "APPSTORE_PRODUCTION_URL / APPSTORE_SANDBOX_URL in "
                "app/services/subscription.py.",
                file=sys.stderr,
            )
        else:
            # Not a connection failure -- most likely token_factory()
            # (_get_appstore_jwt) raised while signing the request, e.g. a
            # malformed APPSTORE_PRIVATE_KEY (--from-cloud-run copies it
            # pipe-separated; it must be un-mangled back into real newlines
            # before it is a usable PEM key). Only the exception type is
            # printed here -- unlike httpx.TransportError above, an arbitrary
            # exception's message is not proven credential-free.
            print(
                f"error: Fetch failed against {base_url} ({environment}): {type(exc).__name__}",
                file=sys.stderr,
            )
            print(
                "error: this does not look like a connection failure -- check the App "
                "Store credentials, especially APPSTORE_PRIVATE_KEY (must be a PEM "
                "private key; --from-cloud-run copies it pipe-separated and it must be "
                "un-mangled back into real newlines). Re-run with --debug for a full "
                "traceback (may print the exception's own message, so use it locally "
                "rather than pasting the output anywhere).",
                file=sys.stderr,
            )
        if args.debug:
            raise
        return 1

    try:
        report = asyncio.run(replay(items, apply=args.apply))
    except Exception as e:  # noqa: BLE001 - keep totals visible even on an unexpected failure
        print("-" * 72)
        print(f"fetched={len(items)} (replay aborted before totals were available)")
        print(f"error: replay failed: {type(e).__name__}", file=sys.stderr)
        if args.debug:
            raise
        return 1

    print("-" * 72)
    print(f"fetched={report.fetched}  rejected={report.rejected}  stale_skipped={report.stale_skipped}")
    if report.stale_by_type:
        stale_by_type = "  ".join(f"{k}={v}" for k, v in sorted(report.stale_by_type.items()))
        print(f"stale_by_type: {stale_by_type}")
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
