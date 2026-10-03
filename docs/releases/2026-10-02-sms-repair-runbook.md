# Existing-user SMS repair runbook

Status on October 2: source diagnosis and read-only audit tooling prepared;
live reconciliation and repair await authenticated Twilio and Google Cloud
access. No membership, consent, registration, number or message was changed.

The October 1 account inspection found 29 Kevin voice numbers, 19 members of
the verified Hey Kevin A2P messaging service and ten outside that service. These
are dated provider inventory counts, not ten proven affected active users.
The correct production account ends **0494**; the unrelated account ends 45a9.
Rebind the full SID against the serving service before any action.

Source diagnosis: `provision_twilio_number` purchases a number and configures
voice/SMS callbacks but does not enroll it in the Messaging Service. Existing
number assignments return early. This can explain sender-pool drift; it does
not establish every affected number's history. The SMS sender uses the tenant's
explicit assigned number and configured service. Preserve that tenant identity.

## Read and reconcile

1. Confirm current Cloud Run project `kevin-491315`, service `kevin-api`, serving
   revision/SHA, Twilio account binding and configured messaging-service SID.
   Read only those allowlisted values; never dump the full environment or keys.
2. In that exact account, inspect the service, verified campaign, approved use
   case and fee/number limits. Membership is not campaign eligibility or delivery.
3. Collect owned number inventory and service membership. Join inventory to the
   pool by provider number SID, then to projected contractor assignments by valid
   E.164. Use `scripts/sms_readiness_audit.py`'s injected read-only collector;
   do not read full profiles, calls, estimates, message bodies or identity files.
4. Inspect only aggregate output. Keep active state, subscription fields,
   deletion/app-deletion, recent inbound/forwarding activity and SMS preferences
   separate. Missing data means unknown. Expired accounts may still use voicemail
   texts. A stored expiry alone does not prove nonpayment.
5. Any wrong binding, partial read, duplicate assignment or stale snapshot blocks
   candidate selection. The offline CLI verifies supplied structure, not the
   authenticity or completeness of an independently provided export.

The CLI takes a JSON snapshot, explicit expected project/account/service and
UTC as-of time. It does not authenticate, apply repairs or send messages. Raw
snapshots must remain in ephemeral protected memory/scratch; commit aggregate
evidence only. The collector's exact projection is pinned in
`../superpowers/plans/2026-10-02-sms-readiness-contract.md`.

## Bounded correction after reconciliation

The owner agreed to correcting eligible existing registrations. Before using
that authority, bind a finite private candidate list to the verified approved
campaign and confirm the correction adds no unapproved fees, declarations or
service scope. If any of those changes, present the exact difference for owner
approval. Never treat a generic review-candidate count as an apply list.

For each eligible uniquely assigned owned number, re-read its account, current
assignment, service membership and campaign compatibility immediately before
the change; add only missing membership through the existing verified service.
Read back registration state and wait for its actual readiness. Record a private
before/after reference and aggregate result. Do not move numbers between services,
buy/release numbers, bulk set compliance approval, change owner opt-in/opt-out,
or replay historical messages as part of this repair.

The existing status callback handles provider opt-out code 21610 but is not a
delivery ledger. `owner_sms` returning true proves acceptance only. For the
owner-controlled test, bind one intended test recipient and dedicated assigned
sender, observe provider final delivery status and independently confirm handset
receipt. The owner makes the call. Do not infer success from a queued message or
a `sms_sent` log. STOP/opt-out must continue to suppress delivery.

After the pilot correction passes, reconcile all authorized candidates, record
registered/pending/failed counts and explain any exclusions. A future automatic
enrollment hook needs the verified campaign/account/fee contract and separate
failure-recovery design; do not silently add chargeable enrollment to provisioning.
