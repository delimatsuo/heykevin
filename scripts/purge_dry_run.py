#!/usr/bin/env python3
"""Dry-run for the data purge: report WHAT WOULD be purged, delete nothing.

Prints aggregate counts only — never document contents, never PII. Run and
review before the first PURGE_ENABLED flip (spec §4 safety rails):

    CLOUDSDK_CORE_ACCOUNT=... .venv/bin/python scripts/purge_dry_run.py \
        --project kevin-491315 [--grace-days 30]
"""

from __future__ import annotations

import argparse
import sys
import time

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

SUBCOLLECTIONS = (
    "contacts", "caller_contacts", "service_requests", "inbound_messages",
    "devices", "settings", "knowledge_base", "customer_memory",
)
BY_CONTRACTOR = ("calls", "jobs", "post_call_handoffs", "estimates", "conference_bindings")


def count_query(q) -> int:
    return sum(1 for _ in q.stream())


def main(argv: list[str] | None = None, client_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--grace-days", type=int, default=30)
    args = parser.parse_args(argv)

    client = (client_factory or (lambda: firestore.Client(project=args.project)))()
    # Mirror purge_sweep: the 6h sweep interval comes out of the window.
    cutoff = time.time() - (args.grace_days * 24 * 3600 - 6 * 3600)

    candidates = list(
        client.collection("contractors")
        .where(filter=FieldFilter("deletion_requested_at", "<", cutoff))
        .stream()
    )
    eligible = 0
    for snap in candidates:
        data = snap.to_dict() or {}
        if data.get("purged_at") or data.get("active") is not False:
            continue
        eligible += 1
        doc_ref = snap.reference
        counts: dict[str, int] = {}
        for name in SUBCOLLECTIONS:
            counts[name] = count_query(doc_ref.collection(name))
        counts["command_receipts"] = sum(
            count_query(m.reference.collection("command_receipts"))
            for m in doc_ref.collection("customer_memory").stream()
        )
        for name in BY_CONTRACTOR:
            counts[name] = count_query(
                client.collection(name).where(
                    filter=FieldFilter("contractor_id", "==", snap.id)
                )
            )
        nonzero = {k: v for k, v in counts.items() if v}
        print(f"contractor={snap.id[:8]}… would purge: {nonzero or '(tombstone only)'}")

    print(f"\n{eligible} account(s) past the {args.grace_days}-day grace period; "
          f"{len(candidates) - eligible} already purged. Nothing was deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
