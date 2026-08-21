#!/usr/bin/env python3
"""Enable or disable one gated action for a single contractor.

`check_gated_action` reads two nested maps on the contractor document:

* `gated_actions[<action>] is True`      — the feature flag (`requires_flag`)
* `automation_approvals[<action>] is True` — standing owner authorization,
  the alternative to a per-call `owner_confirmed` context

Both are in `PROTECTED_FIELDS`, so no client can set them: enabling an action
is deliberately an operator act. Nothing else in this repository writes them,
which is why a fully built feature can sit dark in production indefinitely —
`ESTIMATE_TOKEN_CREATE` did exactly that, and a production audit on
2026-08-20 found 0 of 113 contractors with either map populated.

Deliberately **single-target** and **dry-run by default**. There is no bulk
mode: turning a side effect on across a tenant base nobody reviewed is how a
feature reaches real callers before anyone has watched it work once.

Output names the matched document id and business name so the operator can
confirm the target. It never prints phone numbers, tokens, or transcripts.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Iterator

COLLECTION = "contractors"
FIRESTORE_STREAM_TIMEOUT_SECONDS = 15

# Mirrors ActionKey in app/services/gated_actions.py. Duplicated rather than
# imported so the script stays runnable without app settings/env configured.
KNOWN_ACTIONS = (
    "caller_text_reply",
    "caller_auto_reply",
    "caller_confirmation_sms",
    "caller_confirmation_mms",
    "caller_vcard_mms",
    "estimate_token_create",
    "estimate_result_sms",
    "jobber_create_job",
    "jobber_create_quote",
    "google_create_event",
    "twilio_call_redirect",
    "twilio_conference_mutation",
    "twilio_number_provision",
    # twilio_number_release and account_delete are deliberately absent:
    # their GatePolicy sets requires_flag=False (deletion is an App Store
    # requirement that must work for every account), so a gated_actions
    # flag written here would never be consulted — offering them would
    # fabricate a kill switch that does not exist.
    "push_lock_screen_context",
)


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
    action: str = "",
) -> list[dict[str, Any]]:
    """Return [{"id", "business_name", "flag", "approval"}] for matches."""
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
                "flag": (data.get("gated_actions") or {}).get(action),
                "approval": (data.get("automation_approvals") or {}).get(action),
                "services_count": len(data.get("services") or []),
            }
        )
    return matches


def apply_action(
    client: Any,
    contractor_id: str,
    action: str,
    enabled: bool,
    *,
    approve_automation: bool,
    note: str = "",
) -> dict[str, Any]:
    """Set the flag (and optionally the standing approval) for one action.

    Dotted field paths so sibling actions in the same map are untouched — a
    whole-map write would silently disable everything else already enabled.
    """
    updates: dict[str, Any] = {
        f"gated_actions.{action}": enabled,
        "gated_actions_updated_at": time.time(),
        "gated_actions_source": f"cli:{note}" if note else "cli",
    }
    if approve_automation:
        updates[f"automation_approvals.{action}"] = enabled
    client.collection(COLLECTION).document(contractor_id).update(updates)
    return updates


def main(argv: list[str] | None = None, client_factory: Any | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enable or disable one gated action for one contractor.",
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
        "--action",
        required=True,
        choices=KNOWN_ACTIONS,
        help="Gated action key to set.",
    )
    parser.add_argument(
        "--disable",
        action="store_true",
        help="Set the action to False instead of True.",
    )
    parser.add_argument(
        "--approve-automation",
        action="store_true",
        help=(
            "Also set automation_approvals for the action. Required for actions "
            "whose policy sets requires_owner_confirmation and that run without "
            "a per-call owner tap (e.g. estimate_token_create in post-call)."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the write. Without this flag the script only reports.",
    )
    parser.add_argument(
        "--note",
        default="",
        help="Short provenance note recorded alongside the change.",
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
            action=args.action,
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
    enabled = not args.disable
    print(f"contractor_id:  {target['id']}")
    print(f"business_name:  {target['business_name']}")
    print(f"action:         {args.action}")
    print(f"current flag:     {target['flag']!r}  ->  {enabled!r}")
    if args.approve_automation:
        print(f"current approval: {target['approval']!r}  ->  {enabled!r}")
    else:
        print(f"current approval: {target['approval']!r}  (unchanged)")

    # A surfaced precondition, not a gate: the post-call offer is skipped
    # entirely when the contractor has no services, and the analyzer needs
    # them to match a diagnosis to a price. Silent either way, so say it.
    if enabled and args.action == "estimate_token_create" and target["services_count"] == 0:
        print(
            "WARNING: this contractor has no services configured. The photo/video "
            "offer is skipped when services is empty, so enabling this alone will "
            "not produce an estimate link.",
            file=sys.stderr,
        )

    if target["flag"] == enabled and (
        not args.approve_automation or target["approval"] == enabled
    ):
        print("Already in the desired state. Nothing to do.")
        return 0

    if not args.apply:
        print("Dry run — no write performed. Re-run with --apply to write.")
        return 0

    try:
        apply_action(
            client,
            target["id"],
            args.action,
            enabled,
            approve_automation=args.approve_automation,
            note=args.note,
        )
    except Exception:
        print("Write failed. Verify write access to the requested project.", file=sys.stderr)
        return 1

    print(f"Wrote gated_actions.{args.action}={enabled!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
