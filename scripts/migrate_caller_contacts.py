"""Inventory quarantined global ``caller_contacts`` records without copying them.

Legacy records have no trustworthy contractor binding. Copying one record into
every contractor called by the same phone can leak one tenant's extracted name,
business, issue summary, or history into another tenant. Runtime code therefore
ignores the global collection and this script is intentionally inventory-only.

Rebuild tenant records from tenant-bound source calls or discard the legacy
records under a separately reviewed migration. Do not infer ownership from a
phone-number match.

Usage
-----
    # Print aggregate quarantine inventory only; changes nothing.
    python scripts/migrate_caller_contacts.py

This relies on application default credentials and the same project ID as
``app.db.firestore_client``. Run from a workstation with ``gcloud auth
application-default login`` against the production GCP project.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable


def _setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return logging.getLogger("migrate_caller_contacts")


def _inventory(db, *, log: logging.Logger) -> dict:
    legacy_iter: Iterable = db.collection("caller_contacts").stream()
    stats = {"legacy_docs_quarantined": 0}

    for _snap in legacy_iter:
        stats["legacy_docs_quarantined"] += 1

    log.warning("global caller_contacts are quarantined; no tenant copy or purge was performed")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    log = _setup_logging()
    log.info("caller_contacts quarantine inventory starting")

    # Lazy import so unit tests can import this module without firestore creds.
    from app.db.firestore_client import get_firestore_client

    db = get_firestore_client()
    stats = _inventory(db, log=log)
    log.info("caller_contacts quarantine inventory done: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
