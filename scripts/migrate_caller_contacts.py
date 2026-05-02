"""F-14 one-time migration: copy global ``caller_contacts/{phone}`` docs into
per-contractor subcollections ``contractors/{contractor_id}/caller_contacts/{phone}``.

Idempotent and safe to run multiple times: an existing per-contractor doc is
left alone (we never overwrite). Source docs in the global collection are NOT
deleted by this script — operators should keep the legacy data around for at
least one rollout window in case of regression, then run ``--purge`` to clean
up after the new code path has been live and confirmed working.

Mapping strategy
----------------
Each legacy caller-contact doc records a series of calls but does not carry a
contractor_id. To rebuild the (caller_phone, contractor_id) tuples we walk the
``calls`` collection (which IS contractor-scoped), bucketing by caller phone.
A given caller phone may have called multiple contractors over time, so the
migration replicates the legacy doc into each contractor that has spoken to
that caller. This intentionally over-replicates rather than guessing — better
to seed every legitimate tenant's view than to silently lose a known caller.

Usage
-----
    # Dry-run (default): print what would happen, change nothing.
    python scripts/migrate_caller_contacts.py

    # Actually write per-contractor copies.
    python scripts/migrate_caller_contacts.py --apply

    # After verifying the new code is healthy, delete the legacy global docs.
    python scripts/migrate_caller_contacts.py --apply --purge

This relies on application default credentials and the same project ID as
``app.db.firestore_client``. Run from a workstation with ``gcloud auth
application-default login`` against the production GCP project.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from typing import Dict, Iterable, Set


def _setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return logging.getLogger("migrate_caller_contacts")


def _build_caller_to_contractor_map(db) -> Dict[str, Set[str]]:
    """Walk the ``calls`` collection to build {caller_phone_key: {contractor_id, ...}}.

    Reads can be large; we stream and keep only the keys we need in memory.
    """
    from app.db.contacts import caller_contact_key

    log = logging.getLogger("migrate_caller_contacts")
    mapping: Dict[str, Set[str]] = defaultdict(set)
    docs = db.collection("calls").stream()
    examined = 0
    for snap in docs:
        examined += 1
        data = snap.to_dict() or {}
        caller = data.get("caller_phone") or data.get("from_number") or ""
        contractor_id = data.get("contractor_id") or ""
        if not caller or not contractor_id:
            continue
        mapping[caller_contact_key(caller)].add(contractor_id)
        if examined % 5000 == 0:
            log.info("calls scanned=%d unique_callers_so_far=%d", examined, len(mapping))
    log.info("calls scanned total=%d unique_callers=%d", examined, len(mapping))
    return mapping


def _migrate(db, *, apply: bool, purge: bool, log: logging.Logger) -> dict:
    from app.db.contacts import (
        CALLER_CONTACTS_SUBCOLLECTION,
        LEGACY_CALLER_CONTACTS_COLLECTION,
    )

    caller_to_contractors = _build_caller_to_contractor_map(db)

    legacy_iter: Iterable = db.collection(LEGACY_CALLER_CONTACTS_COLLECTION).stream()
    stats = {
        "legacy_docs_seen": 0,
        "legacy_docs_with_no_contractor_match": 0,
        "per_contractor_writes": 0,
        "per_contractor_skipped_existing": 0,
        "legacy_docs_purged": 0,
    }

    for snap in legacy_iter:
        stats["legacy_docs_seen"] += 1
        phone_key = snap.id
        data = snap.to_dict() or {}
        contractors = caller_to_contractors.get(phone_key, set())
        if not contractors:
            stats["legacy_docs_with_no_contractor_match"] += 1
            log.warning(
                "no contractor found for legacy caller_contacts/%s — skipping (no calls reference this caller)",
                phone_key,
            )
            continue

        for contractor_id in sorted(contractors):
            target_ref = (
                db.collection("contractors")
                .document(contractor_id)
                .collection(CALLER_CONTACTS_SUBCOLLECTION)
                .document(phone_key)
            )
            existing = target_ref.get()
            if existing.exists:
                stats["per_contractor_skipped_existing"] += 1
                log.info(
                    "skip existing contractors/%s/caller_contacts/%s",
                    contractor_id,
                    phone_key,
                )
                continue

            log.info(
                "%s contractors/%s/caller_contacts/%s",
                "WRITE" if apply else "DRYRUN",
                contractor_id,
                phone_key,
            )
            if apply:
                target_ref.set(dict(data), merge=True)
            stats["per_contractor_writes"] += 1

        if purge and apply:
            log.info("PURGE caller_contacts/%s", phone_key)
            snap.reference.delete()
            stats["legacy_docs_purged"] += 1

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform writes. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="After successful per-contractor copy, delete the legacy global doc. "
        "Only acts when --apply is also set.",
    )
    args = parser.parse_args()

    log = _setup_logging()
    log.info("migrate_caller_contacts starting (apply=%s, purge=%s)", args.apply, args.purge)

    # Lazy import so unit tests can import this module without firestore creds.
    from app.db.firestore_client import get_firestore_client

    db = get_firestore_client()
    stats = _migrate(db, apply=args.apply, purge=args.purge, log=log)
    log.info("migrate_caller_contacts done: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
