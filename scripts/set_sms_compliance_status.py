#!/usr/bin/env python3
"""Set `sms_compliance_status` on a single contractor document.

`sms_compliance_status` gates every caller-facing SMS action in
`app/services/gated_actions.py` (`check_gated_action` fails closed when it is
not exactly "approved"), but nothing else in this repository writes it:

* the contractor PATCH endpoint drops it — `ContractorUpdate` never declares
  the field, so pydantic discards it before `PROTECTED_FIELDS` is consulted;
* the admin UI never references it;
* `scripts/phase0_staging_smoke.py` only ever deletes it.

That left the gate unsettable except by hand-editing Firestore. This script is
the supported writer.

It is deliberately **single-target** and **dry-run by default**. There is no
"update every contractor" mode: the value is a per-tenant compliance
attestation, and asserting it in bulk would attest on behalf of accounts whose
compliance posture nobody has actually checked.

Carrier A2P/10DLC approval is a *separate* thing and does not set this field.
Approving a campaign with the carriers does not make this value "approved".

Output names the matched document id and business name so the operator can
confirm the target before applying. It never prints phone numbers, tokens,
transcripts, or message bodies.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Iterator

COLLECTION = "contractors"
FIELD = "sms_compliance_status"
FIRESTORE_STREAM_TIMEOUT_SECONDS = 15

# "missing" is an audit bucket for an absent field, not a value we ever write.
SETTABLE_STATUSES = ("approved", "pending", "rejected")


def _stream_contractors(client: Any) -> Iterator[Any]:
    yield from client.collection(COLLECTION).stream(
        retry=None,
        timeout=FIRESTORE_STREAM_TIMEOUT_SECONDS,
    )


def find_targets(
    client: Any,
    *,
    contractor_id: str | None = None,
    business_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return [{"id", "business_name", "current"}] for documents matching the selector."""
    matches: list[dict[str, Any]] = []
    for doc in _stream_contractors(client):
        data = doc.to_dict() or {}
        if contractor_id is not None and doc.id != contractor_id:
            continue
        if business_name is not None and data.get("business_name") != business_name:
            continue
        matches.append(
            {
                "id": doc.id,
                "business_name": data.get("business_name", ""),
                "current": data.get(FIELD),
            }
        )
    return matches


def apply_status(client: Any, contractor_id: str, status: str) -> None:
    client.collection(COLLECTION).document(contractor_id).update({FIELD: status})


def main(argv: list[str] | None = None, client_factory: Any | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set sms_compliance_status on one contractor document.",
    )
    parser.add_argument("--project", required=True, help="Firestore project ID.")
    parser.add_argument(
        "--database",
        default="(default)",
        help="Firestore database ID. Defaults to '(default)'.",
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--contractor-id", help="Exact contractor document ID.")
    selector.add_argument("--business-name", help="Exact business_name to match.")
    parser.add_argument(
        "--status",
        required=True,
        choices=SETTABLE_STATUSES,
        help="Value to write.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the write. Without this flag the script only reports.",
    )
    args = parser.parse_args(argv)

    if client_factory is None:
        from google.cloud import firestore

        client_factory = firestore.Client

    client = client_factory(project=args.project, database=args.database)

    try:
        matches = find_targets(
            client,
            contractor_id=args.contractor_id,
            business_name=args.business_name,
        )
    except Exception:
        print(
            "Lookup failed while reading Firestore. Reauthenticate ADC and verify "
            "read access to the requested project.",
            file=sys.stderr,
        )
        return 1

    if not matches:
        print("No contractor matched the selector. Nothing was written.", file=sys.stderr)
        return 1

    if len(matches) > 1:
        print(
            f"Selector matched {len(matches)} contractors. Refusing to guess; "
            "re-run with --contractor-id. Nothing was written.",
            file=sys.stderr,
        )
        return 1

    target = matches[0]
    print(f"contractor_id:  {target['id']}")
    print(f"business_name:  {target['business_name']}")
    print(f"current {FIELD}: {target['current']!r}")
    print(f"desired {FIELD}: {args.status!r}")

    if target["current"] == args.status:
        print("Already set to the desired value. Nothing to do.")
        return 0

    if not args.apply:
        print("Dry run — no write performed. Re-run with --apply to write.")
        return 0

    try:
        apply_status(client, target["id"], args.status)
    except Exception:
        print("Write failed. Verify write access to the requested project.", file=sys.stderr)
        return 1

    print(f"Wrote {FIELD}={args.status!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
