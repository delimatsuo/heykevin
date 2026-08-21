# Data-Purge Pipeline — Spec (for owner review, do not build yet)

Status: **SPEC ONLY.** Authorized 2026-08-20 as "spec (don't build) the
data-purge pipeline for my review." Nothing here is implemented. Building it,
deploying it, and any change to the deletion copy are owner-gated.

## 1. Problem

Account deletion is a soft deactivate (`active=False` + Twilio number
release). The UI promises "permanently delete your Kevin account…
deletes all data", and `NSContactsUsageDescription` promises synced contacts
are deleted with the account — but nothing is ever purged. This is an App
Store Guideline 5.1.1(v) exposure and a broken privacy promise on a live,
sold product. Surfaced by the PR #192 review; recorded in memory
(`project-account-deletion`).

## 2. What a deleted account currently retains (verified inventory)

| Store | Path | Retention today |
|---|---|---|
| Contractor doc | `contractors/{id}` | forever (all business info, owner phone, knowledge text) |
| Synced device contacts | `contractors/{id}/contacts/*` | forever |
| Caller contacts | `contractors/{id}/caller_contacts/*` | forever |
| Customer memory | `contractors/{id}/customer_memory/*`, `command_receipts` | forever |
| Service requests | `contractors/{id}/service_requests/*` | forever |
| Inbound SMS | `contractors/{id}/inbound_messages/*` | forever |
| Device tokens | `contractors/{id}/devices/primary` | forever |
| Knowledge base | `knowledge_base` (per-contractor subcollection helper) | forever |
| Calls + transcripts | `calls` (by `contractor_id`) | 90 days / 100 calls (existing) |
| Job cards | `jobs` (by `contractor_id`) | forever |
| Post-call handoffs | `post_call_handoffs` | forever |
| Estimate records | `estimates/*` (top-level, by `contractor_id`; holds `caller_phone`, descriptions, analysis results) | forever |
| Settings prefs | `contractors/{id}/settings/preferences` (greeting name, text-reply message) | forever |
| Estimate media | `gs://kevin-estimate-media/...` | 90-day lifecycle (existing) |
| Apple tx bindings | `apple_transactions` | forever |
| RTDB live-call state | `active_calls/{call_sid}` | transient (already ephemeral) |

## 3. Owner decisions required (the spec's open inputs)

1. **Grace period** before purge. Recommendation: **30 days** after
   `deactivated_at`. Covers accidental deletions and disputes; matches the
   billing-reconciliation window where post-deletion renewals are recorded.
2. **What survives as a tombstone.** Recommendation: replace
   `contractors/{id}` with a minimal tombstone document keeping ONLY:
   `active: False`, `purged_at`, `deactivated_at`, `subscription_uuid`,
   `post_deletion_billing`, `number_release_anomaly`, `deleted_app_detected_at`.
   `active: False` is load-bearing: the reconciliation guard in
   `handle_appstore_notification` requires an EXPLICIT `active is False`
   (a missing field falls through to not-found), and the §4 sweep query
   matches `active == False` — a tombstone without it would silently
   re-create the dropped-notification hole and never re-match the sweep.
   Rationale: `subscription_uuid` is a random UUID (not PII) and is the ONLY
   key that lets the billing-after-deletion reconciliation (shipped
   2026-08-20) attribute App Store renewals for a purged account. Purging it
   would re-create the silent-billing hole. `apple_transactions` bindings
   stay for the same reason (fraud/replay defense, no PII beyond the binding).
3. **Copy alignment.** After purge ships, "deletes all data" becomes true
   (modulo tombstone). Whether to adjust copy to mention the grace period is
   an owner call — copy changes stay owner-gated either way.
4. **Data export before deletion** (GDPR/CCPA portability): explicitly OUT of
   this spec; decide separately.

## 4. Design

Two phases, reusing the existing lifecycle machinery:

- **Phase A (exists): deactivate.** Unchanged — `active=False`,
  `deactivated_at`, number release, token-cache invalidation.
- **Phase B (new): purge sweep.** Extend the existing background sweep in
  `app/main.py` (the `deleted_app_detected_at` / 14-day loop) with:
  `active == False AND deactivated_at < now - GRACE AND purged_at absent`
  → `purge_contractor(contractor_id)`.

`purge_contractor` (new, `app/db/purge.py`):
1. Delete all documents in each subcollection (batched, 500/batch):
   `contacts`, `caller_contacts`, `service_requests`, `inbound_messages`,
   `devices`, `settings`, knowledge subcollection. For `customer_memory`,
   traverse nested receipts FIRST: receipts live at
   `contractors/{id}/customer_memory/{customer_key}/command_receipts/{command_key}`
   and Firestore does not cascade-delete subcollections — deleting the
   memory doc first orphans its receipts. Delete each memory doc's
   `command_receipts` collection, then the memory doc.
2. Delete `calls`, `jobs`, `post_call_handoffs`, and `estimates` where
   `contractor_id == {id}` (batched queries; `estimates` is top-level and
   holds `caller_phone` + analysis results).
3. Delete `gs://$ESTIMATE_MEDIA_BUCKET` objects under the contractor's
   prefix (lifecycle would get them in ≤90d; explicit delete makes the
   promise honest).
4. Overwrite `contractors/{id}` with the tombstone (single `set()`, no
   merge — this is the PII kill step).
5. Log one structured line: contractor prefix, per-store deletion counts,
   duration. Never log contents.

Idempotent by construction: every step tolerates already-deleted data, so a
crashed sweep re-runs clean. Per-contractor failure isolates (try/except per
account, sweep continues).

### Safety rails

- `PURGE_ENABLED` env var, default **off**; turning it on in production is
  an owner action (flag + deploy, both owner-gated).
- `scripts/purge_dry_run.py`: prints per-store counts for what WOULD be
  purged, aggregate only, no PII — run and reviewed before first enable.
- The sweep refuses any contractor where `active != False` — purging an
  active account is structurally impossible, asserted by test.
- First production run: owner picks one known-dead account (e.g. one of the
  five "Deli Matsuo's phone" records), single-target manual script run,
  verify, then enable the sweep.

## 5. Testing requirements (all mutation-checked)

- Purge refuses active contractors (delete the guard → named test fails).
- Grace period respected (deactivated yesterday ≠ purged).
- Tombstone contains exactly the allow-listed fields and nothing else
  (assert as an allowlist, so a future PII field can't leak through).
- Idempotency: purge twice, second run is a no-op with zero errors.
- Batched deletes handle >500 docs per subcollection.
- Nested receipt traversal: a purge of a contractor with
  `customer_memory/{key}/command_receipts/*` leaves zero orphaned receipt
  docs (assert by listing the nested collection post-purge).
- Reconciliation still works post-purge: an App Store notification for a
  purged account still records `post_deletion_billing` on the tombstone.
- Sweep failure isolation: one poisoned account doesn't stop the sweep.

## 6. Explicit non-goals

- Data export / portability.
- Purging `admin_audit_events` (operator audit trail, not user data).
- Real-time purge at deletion time (grace period is deliberate).
- Any copy change (owner-gated).

## 7. Rough size

`app/db/purge.py` + sweep wiring + dry-run script + tests ≈ one PR of
comparable size to #192. No iOS work required.
