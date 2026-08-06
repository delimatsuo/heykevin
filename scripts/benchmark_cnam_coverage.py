#!/usr/bin/env python3
"""Measure real CNAM coverage against Kevin's own caller population.

CNAM is already wired up: `app/services/lookup.py:_lookup_twilio(include_cnam=True)`
is called from the post-routing background task in `app/webhooks/twilio_incoming.py`,
gated per contractor by `cnam_lookup_enabled` (default False). This script answers
the only open question before turning that flag on more widely: what fraction of
*our* callers does CNAM actually name, versus billing us $0.01 for "WIRELESS CALLER"?

Industry coverage is widely quoted at ~50%, but that average is dominated by
landline traffic. Kevin screens mobile-heavy unknown callers, so the number that
matters is the one measured here, not the one in a vendor blog post.

Privacy: this script reads customer phone numbers in order to look them up, but
must never print phone numbers, caller names, contractor IDs, or call SIDs. Output
is aggregate counts and classification buckets only (same rule as
`scripts/phase0_account_audit.py`).

Cost: Twilio bills per Lookup request even when no data is returned, so the spend
is deterministic in the sample size. The script prints an estimate and requires
confirmation before spending anything.

Usage:
    python scripts/benchmark_cnam_coverage.py --limit 200
    python scripts/benchmark_cnam_coverage.py --limit 200 --yes
    python scripts/benchmark_cnam_coverage.py --dry-run     # sample only, $0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# Twilio Lookup v2 list pricing (https://www.twilio.com/en-us/lookup/pricing).
# Both packages bill per request whether or not data comes back.
COST_CALLER_NAME = 0.01
COST_LINE_TYPE = 0.008

# Concurrency against the Twilio Lookup API. Deliberately modest: this is a
# one-off measurement, not a throughput test, and we share an account with
# production call routing.
MAX_CONCURRENCY = 5

# Call records are retained 90 days (app/db/calls.py RETENTION_DAYS).
DEFAULT_LOOKBACK_DAYS = 90

# Placeholder strings carriers return instead of a name. These are the whole
# reason raw coverage numbers are misleading: they are billed as successful
# lookups but carry no identifying information.
JUNK_EXACT = {
    "WIRELESS CALLER",
    "WIRELESS CALL",
    "CELLULAR CALLER",
    "CELL PHONE",
    "CELLPHONE",
    "UNKNOWN",
    "UNKNOWN CALLER",
    "UNKNOWN NAME",
    "UNAVAILABLE",
    "NAME UNAVAILABLE",
    "NOT AVAILABLE",
    "NO NAME",
    "TOLL FREE",
    "TOLLFREE",
    "TOLL FREE CALLER",
    "PAYPHONE",
    "PAY PHONE",
    "PRIVATE",
    "PRIVATE CALLER",
    "RESTRICTED",
    "ANONYMOUS",
    "OUT OF AREA",
    "V-",
}

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "VI", "GU", "AS", "MP",
}

_DIGITS_ONLY = re.compile(r"^[\d\s\-\(\)\+\.]+$")

# Classification buckets, ordered for reporting.
BUCKETS = (
    "named",
    "junk_wireless",
    "junk_geographic",
    "junk_generic",
    "empty",
    "error",
)


def classify_caller_name(raw: Optional[str]) -> str:
    """Bucket a CNAM string into one of BUCKETS.

    A name is only `named` if it identifies someone. Carrier placeholders and
    city/state strings are billed the same as a real name but are useless to
    the caller-screening product, so they are counted separately.
    """
    if raw is None:
        return "empty"
    name = " ".join(str(raw).strip().upper().split())
    if not name:
        return "empty"

    if name in JUNK_EXACT:
        # "WIRELESS CALLER" and friends: the number resolved, the person didn't.
        return "junk_wireless" if "WIRELESS" in name or "CELL" in name else "junk_generic"

    if _DIGITS_ONLY.match(name):
        # Some carriers echo the number back as the name.
        return "junk_generic"

    parts = name.split()
    if len(parts) >= 2 and parts[-1] in _US_STATES:
        # "MIAMI FL", "NEW YORK NY" — rate-center geography, not an identity.
        return "junk_geographic"

    if len(name) <= 2:
        return "junk_generic"

    return "named"


# ---- Sampling ---------------------------------------------------------------


def sample_caller_numbers(limit: int, lookback_days: int, scan_cap: int) -> tuple[list[dict], dict]:
    """Return up to `limit` distinct caller records plus sampling stats.

    Each record is {"phone": str, "contractor_id": str}. Deduplicated by phone,
    because we pay per distinct number and a repeat caller would otherwise skew
    the coverage rate toward whoever calls most.
    """
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    from app.db.calls import COLLECTION
    from app.db.firestore_client import get_firestore_client

    db = get_firestore_client()
    cutoff = time.time() - (lookback_days * 86400)

    query = (
        db.collection(COLLECTION)
        .where(filter=FieldFilter("timestamp", ">=", cutoff))
        .order_by("timestamp", direction=firestore.Query.DESCENDING)
        .limit(scan_cap)
    )

    seen: set[str] = set()
    sampled: list[dict] = []
    scanned = 0
    missing_phone = 0

    for doc in query.stream():
        scanned += 1
        record = doc.to_dict() or {}
        phone = str(record.get("caller_phone") or "").strip()
        if not phone:
            missing_phone += 1
            continue
        if phone in seen:
            continue
        seen.add(phone)
        sampled.append({"phone": phone, "contractor_id": str(record.get("contractor_id") or "")})
        if len(sampled) >= limit:
            break

    stats = {
        "docs_scanned": scanned,
        "docs_missing_phone": missing_phone,
        "distinct_numbers": len(seen),
        "scan_cap_hit": scanned >= scan_cap,
    }
    return sampled, stats


async def filter_to_unknown_callers(records: list[dict]) -> tuple[list[dict], int]:
    """Drop numbers that already resolve to a saved contact.

    Known contacts ring straight through via CallKit and are never sent to AI
    screening, so in production they would never trigger a CNAM lookup. Leaving
    them in would inflate the measured hit rate with numbers we already have
    names for.
    """
    from app.db.contacts import get_contact

    kept: list[dict] = []
    dropped = 0
    for record in records:
        try:
            contact = await get_contact(record["phone"], record.get("contractor_id", ""))
        except Exception:
            contact = None
        if contact:
            dropped += 1
            continue
        kept.append(record)
    return kept, dropped


# ---- Lookup -----------------------------------------------------------------


async def fetch_lookup(phone: str, with_line_type: bool) -> dict[str, Any]:
    """Fetch CNAM (and optionally line type) for one number.

    Mirrors `app.services.lookup._lookup_twilio` but makes the requested field
    set configurable, so a caller-name-only run isn't billed for line type
    intelligence it does not measure.
    """
    from twilio.rest import Client

    from app.config import settings

    fields = "caller_name"
    if with_line_type:
        fields = "caller_name,line_type_intelligence"

    try:
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: client.lookups.v2.phone_numbers(phone).fetch(fields=fields),
            ),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        return {"bucket": "error", "reason": "timeout"}
    except Exception as e:
        return {"bucket": "error", "reason": type(e).__name__}

    caller_info = getattr(result, "caller_name", None) or {}
    raw_name = caller_info.get("caller_name")
    caller_type = (caller_info.get("caller_type") or "").upper()
    lookup_error = caller_info.get("error_code")

    line_type = ""
    if with_line_type:
        line_info = getattr(result, "line_type_intelligence", None) or {}
        line_type = (line_info.get("type") or "").lower()

    return {
        "bucket": classify_caller_name(raw_name),
        "caller_type": caller_type if caller_type in {"BUSINESS", "CONSUMER"} else "",
        "line_type": line_type,
        "lookup_error_code": lookup_error,
    }


async def run_lookups(records: list[dict], with_line_type: bool) -> list[dict]:
    """Look up every sampled number, bounded by MAX_CONCURRENCY."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    completed = 0
    total = len(records)

    async def one(record: dict) -> dict:
        nonlocal completed
        async with semaphore:
            result = await fetch_lookup(record["phone"], with_line_type)
        completed += 1
        if completed % 25 == 0 or completed == total:
            print(f"  ...{completed}/{total}", file=sys.stderr)
        return result

    return await asyncio.gather(*(one(r) for r in records))


# ---- Reporting --------------------------------------------------------------


def _pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "  n/a"
    return f"{100.0 * numerator / denominator:5.1f}%"


def build_report(results: list[dict], with_line_type: bool, sampling: dict) -> str:
    total = len(results)
    buckets = Counter(r["bucket"] for r in results)
    named = buckets.get("named", 0)

    lines: list[str] = []
    lines.append("=" * 58)
    lines.append("CNAM COVERAGE BENCHMARK")
    lines.append("=" * 58)
    lines.append("")
    lines.append(f"sample size          {total} distinct numbers")
    lines.append(f"call docs scanned    {sampling.get('docs_scanned', 0)}")
    if sampling.get("known_contacts_dropped"):
        lines.append(f"known contacts excl. {sampling['known_contacts_dropped']}")
    if sampling.get("scan_cap_hit"):
        lines.append("note: scan cap reached — sample is the most recent slice, not random")
    lines.append("")

    lines.append(f"{'RESULT':<24}{'COUNT':>7}{'PCT':>8}")
    lines.append("-" * 39)
    labels = {
        "named": "named (usable)",
        "junk_wireless": "junk: wireless caller",
        "junk_geographic": "junk: city/state",
        "junk_generic": "junk: generic",
        "empty": "empty",
        "error": "lookup error",
    }
    for bucket in BUCKETS:
        count = buckets.get(bucket, 0)
        lines.append(f"{labels[bucket]:<24}{count:>7}{_pct(count, total):>8}")
    lines.append("-" * 39)

    caller_types = Counter(
        r["caller_type"] for r in results if r["bucket"] == "named" and r["caller_type"]
    )
    if caller_types:
        lines.append("")
        lines.append("named breakdown")
        for label, count in caller_types.most_common():
            lines.append(f"  {label.lower():<22}{count:>7}{_pct(count, total):>8}")

    lines.append("")
    lines.append(f"USABLE NAME RATE     {_pct(named, total).strip()}")

    per_lookup = COST_CALLER_NAME + (COST_LINE_TYPE if with_line_type else 0.0)
    spend = total * per_lookup
    if named:
        lines.append(f"cost per usable name ${total * COST_CALLER_NAME / named:.4f}")
    else:
        lines.append("cost per usable name n/a — no usable names returned")

    if with_line_type:
        by_line: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in results:
            key = r["line_type"] or "unknown"
            by_line[key][0] += 1
            if r["bucket"] == "named":
                by_line[key][1] += 1
        lines.append("")
        lines.append(f"{'BY LINE TYPE':<16}{'N':>6}{'NAMED':>7}{'PCT':>8}")
        lines.append("-" * 37)
        for key, (n, hit) in sorted(by_line.items(), key=lambda kv: -kv[1][0]):
            lines.append(f"{key:<16}{n:>6}{hit:>7}{_pct(hit, n):>8}")

    lines.append("")
    lines.append("SPEND")
    lines.append(f"  caller_name    {total:>5} x ${COST_CALLER_NAME:.4f} = ${total * COST_CALLER_NAME:.2f}")
    if with_line_type:
        lines.append(f"  line_type      {total:>5} x ${COST_LINE_TYPE:.4f} = ${total * COST_LINE_TYPE:.2f}")
    lines.append(f"  total                          ${spend:.2f}")
    lines.append("")
    lines.append("Twilio bills per request even when no name is returned, so the")
    lines.append("'cost per usable name' figure above is the real unit economics.")
    lines.append("=" * 58)
    return "\n".join(lines)


# ---- Entry point ------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure CNAM hit rate against Kevin's real caller population.",
    )
    parser.add_argument("--limit", type=int, default=200, help="distinct numbers to sample (default 200)")
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help=f"how far back to sample calls (default {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--scan-cap", type=int, default=5000,
        help="max call docs to read while collecting distinct numbers (default 5000)",
    )
    parser.add_argument(
        "--include-known", action="store_true",
        help="include numbers that match a saved contact (default: exclude, since "
             "known contacts never reach AI screening in production)",
    )
    parser.add_argument(
        "--no-line-type", action="store_true",
        help=f"skip line type intelligence, saving ${COST_LINE_TYPE:.3f}/number "
             "(loses the mobile-vs-landline breakdown)",
    )
    parser.add_argument("--dry-run", action="store_true", help="sample and report cost, spend nothing")
    parser.add_argument("--yes", action="store_true", help="skip the spend confirmation prompt")
    parser.add_argument("--json-out", type=str, default="", help="write aggregate results to this path")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    with_line_type = not args.no_line_type

    print(f"Sampling up to {args.limit} distinct numbers from the last {args.lookback_days} days...")
    try:
        records, sampling = sample_caller_numbers(args.limit, args.lookback_days, args.scan_cap)
    except ImportError as e:
        # Almost always an environment problem, not a credentials one.
        print(f"ERROR: could not import application modules ({e}).", file=sys.stderr)
        print("Run from the repo root with the project venv: uv run python scripts/...", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: could not read call records ({type(e).__name__}: {e}).", file=sys.stderr)
        print("Check GOOGLE_APPLICATION_CREDENTIALS and FIRESTORE_PROJECT_ID.", file=sys.stderr)
        return 1

    if not records:
        print("No call records with a caller phone found in the lookback window.", file=sys.stderr)
        return 1

    if not args.include_known:
        records, dropped = await filter_to_unknown_callers(records)
        sampling["known_contacts_dropped"] = dropped
        if not records:
            print("Every sampled number matched a saved contact; nothing to measure.", file=sys.stderr)
            return 1

    total = len(records)
    per_lookup = COST_CALLER_NAME + (COST_LINE_TYPE if with_line_type else 0.0)
    estimate = total * per_lookup

    print(f"Sampled {total} distinct numbers (from {sampling['docs_scanned']} call docs).")
    print(f"Estimated spend: {total} x ${per_lookup:.4f} = ${estimate:.2f}")

    if args.dry_run:
        print("\n--dry-run: stopping before any billable lookup.")
        return 0

    if not args.yes:
        answer = input(f"Proceed and spend ~${estimate:.2f}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Aborted; nothing spent.")
            return 0

    print(f"\nLooking up {total} numbers (concurrency {MAX_CONCURRENCY})...", file=sys.stderr)
    results = await run_lookups(records, with_line_type)

    report = build_report(results, with_line_type, sampling)
    print()
    print(report)

    if args.json_out:
        payload = {
            "sample_size": total,
            "with_line_type": with_line_type,
            "buckets": dict(Counter(r["bucket"] for r in results)),
            "caller_types": dict(
                Counter(r["caller_type"] for r in results if r["bucket"] == "named" and r["caller_type"])
            ),
            "line_types": dict(Counter(r["line_type"] for r in results if r["line_type"])),
            "sampling": sampling,
            "spend_usd": round(total * per_lookup, 2),
        }
        with open(args.json_out, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        print(f"\nAggregates written to {args.json_out}")

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
