#!/usr/bin/env python3
"""Purge ONE contractor — the spec's single-target manual test tool.

Dry-run by default: prints what WOULD be purged (aggregate counts, no PII).
--apply executes the real purge through the same app.db.purge code the sweep
uses, so a manual test exercises the production logic, guards included: the
target must carry an explicit active=False AND deletion_requested_at, or the
purge refuses. No bulk mode — bulk erasure only ever happens through the
flag-gated sweep.

    .venv/bin/python scripts/purge_one.py --project kevin-491315 \
        --contractor-id <ID> [--apply] [--media-bucket kevin-estimate-media]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--contractor-id", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="Execute the purge. Without it: report only.")
    parser.add_argument("--media-bucket", default="",
                        help="GCS estimate-media bucket (blank = degraded mode).")
    args = parser.parse_args(argv)

    os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
    # app.config requires these to instantiate; none are used by the purge.
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER",
                 "TELEGRAM_BOT_TOKEN", "USER_PHONE"):
        os.environ.setdefault(name, "unused-by-purge")
    os.environ["ESTIMATE_MEDIA_BUCKET"] = args.media_bucket

    from app.db.purge import purge_contractor  # noqa: E402  (env must precede)

    cid = args.contractor_id
    if not args.apply:
        # Reuse the dry-run counter for a single target.
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "purge_dry_run", Path(__file__).parent / "purge_dry_run.py"
        )
        dry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dry)

        from google.cloud import firestore
        client = firestore.Client(project=args.project)
        snap = client.collection("contractors").document(cid).get()
        if not snap.exists:
            print(f"contractor {cid[:8]}… not found")
            return 1
        data = snap.to_dict() or {}
        print(f"active={data.get('active')} "
              f"deletion_requested_at={data.get('deletion_requested_at')} "
              f"purged_at={data.get('purged_at')}")
        doc_ref = snap.reference
        counts = {}
        for name in dry.SUBCOLLECTIONS:
            counts[name] = dry.count_query(doc_ref.collection(name))
        counts["command_receipts"] = sum(
            dry.count_query(m.collection("command_receipts"))
            for m in doc_ref.collection("customer_memory").list_documents()
        )
        for name in dry.BY_CONTRACTOR:
            counts[name] = dry.count_query(
                client.collection(name).where(
                    filter=firestore.FieldFilter("contractor_id", "==", cid)
                )
            )
        nonzero = {k: v for k, v in counts.items() if v}
        print(f"DRY RUN — would purge: {nonzero or '(tombstone only)'}")
        print("Re-run with --apply to execute.")
        return 0

    result = asyncio.run(purge_contractor(cid))
    if "refused" in result:
        print(f"REFUSED: {result['refused']} — the guards hold; nothing was deleted.")
        return 1
    print(f"PURGED: deleted={result['deleted']} purged_at={result['purged_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
