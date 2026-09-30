# Isolate optional measurement from purchase and retention-critical work

Master accepts both GitHub Codex P2 findings on PR 279, HEAD
cff210a8525b9a219fbf5abb0daa5a637af85a7e. They are real despite the default-off
flag: future enabled analytics must not delay purchase confirmation or the
activity stamps used to retain a caller's number.

Builder: headless agy gemini-3.7-flash-high. Reason: bounded async scheduling,
ownership of background work, and held-await behavior require implementation.
One writer in this exact integration worktree. Allowed writes ONLY:

- app/services/acquisition.py
- app/services/subscription.py
- app/webhooks/twilio_incoming.py
- tests/unit/test_acquisition_measurement.py
- tests/unit/test_acquisition_wiring.py

No Git mutations, tests/builds, network/providers, credentials, .env reads, other
worktrees, dependencies, deployment, workflow edits, or Swift changes. Master
owns review, test execution, mutations and Git. Do not edit this brief.

## Payment scheduling contract

Add a small synchronous schedule_payment_measurement helper in acquisition.py.
It may enqueue optional work but MUST NOT await it or access Firestore itself.
Keep record_payment_measurement as the existing async, revalidating writer.

- Default disabled or unconfigured organization returns without creating a task.
- Invalid contractor ID, tier or non-dict transaction returns without a task.
- Keep a strong private set of pending payment measurement tasks, capped at 32.
  At capacity drop the optional observation; never wait for a slot.
- The task calls the existing record_payment_measurement. Capture contractor ID,
  tier and a new dict containing ONLY classification fields: environment,
  productId, revocationDate, purchaseDate, price, offerDiscountType. Do not retain
  account tokens, transaction IDs, other arbitrary payload fields, or the caller's
  mutable dict. Existing verification still happens before scheduling.
- Keep the slot occupied until the actual task finishes. Do not use a timeout
  that releases the slot while a thread-pool Firestore operation is still running.
  This keeps a hung optional writer bounded and never blocks the HTTP response.
- A done callback removes the task, retrieves exceptions without leaking IDs or
  payloads, and tolerates cancellation. Handle scheduling failure without leaking
  an unawaited coroutine. Logs, if any, are generic or exception-class only.
- In both accepted direct verification and accepted SUBSCRIBED/DID_RENEW paths,
  call this synchronous helper after the existing successful entitlement write.
  No await on measurement in these critical paths. Keep failure/rejection paths
  and entitlement results unchanged, including the accepted-update guard.

## Inbound/forwarding stamp order

In both _record_inbound_call_evidence and _record_forwarding_evidence, attempt
the existing contractor lookup/throttled timestamp write BEFORE invoking optional
measurement. Preserve all existing timestamp names, throttle rules and behavior.
Measurement must still get a chance when the legacy timestamp is recent. Use
sequential isolated try/except blocks with conditional throttling, not an
unconditional finally: cancellation during legacy work must propagate without
starting optional measurement. Do not move measurement above the legacy stamp
or suppress it merely because the legacy stamp is recent.

This supersedes the earlier plan's 'measurement before throttle' ordering. The
actual invariant is that throttling legacy fields must not suppress acquisition
milestones, while a stalled acquisition writer must never delay the legacy stamp.

## Required behavior tests

- Execute actual update_subscription_from_transaction with successful mocked
  entitlement boundaries and the REAL scheduler + a held fake measurement writer.
  Assert verification returns ACTIVE within a small timeout while measurement
  stays held. Do the equivalent for accepted handle_appstore_notification.
- Existing accepted/rejected/false/throw hook tests must assert scheduling only
  after successful entitlement updates; adapt mocks to the new synchronous helper.
  Preserve exception-isolation coverage and rejected ownership/product checks.
- Exercise actual scheduler default-off, valid eligibility, cap (32 held tasks,
  33rd discarded), copied allowlisted payload (mutate original before releasing),
  completed/throwing/cancelled cleanup. Release and join all held tasks in finally
  so tests leave no pending task. No real transport or Firestore; retain tripwires.
- Parameterize both legacy stamp helpers with a held measurement. Once measurement
  enters, assert legacy update already completed; release safely in finally.
  Cover recent-stamp throttle (no duplicate legacy write, measurement still runs)
  and thrown measurement (legacy stamp survives). Keep existing number-release
  regressions passing. Use bounded waits rather than sleeps/unbounded gates.
- Cancel each actual helper during a held legacy lookup and during a held legacy
  update. Cancellation must propagate promptly and optional measurement must not
  start. Parameterize explicit production helper names to prevent stale aliases.

## First repair audit

Parent focused execution found 442 passing tests and two AttributeError failures:
the new inbound cases resolved _record_inbound_evidence rather than the actual
_record_inbound_call_evidence. Fresh review independently confirmed that defect
and the finally/cancellation issue above. Correct both and remove the extra blank
line at the end of test_acquisition_measurement.py; preserve all other contracts.

Return strict JSON changed_files, changes, remaining_gaps. Do not report test
success. Parent will run focused/full checks and mutation probes, then re-review.

## Qualification receipt

The final repair passed 448 focused Python tests and the full local suite:
8,240 passed, 1 skipped. The full-suite harness disabled local Google credential
discovery and real socket connections. Fatal Ruff, Python compilation and diff
checks passed. Swift source was unchanged; the preceding combined iOS run passed
87 tests using the required Xcode wrapper.

Five deliberate mutations were detected: awaiting measurement in direct purchase
verification (one failure), awaiting it in the renewal notification (one), removing
the pending-task cap (one), placing measurement before both legacy stamps (two),
and starting measurement during legacy cancellation through finally (four).
Original source bytes were restored and then the full suite was run successfully.

Fresh independent review reported no remaining findings for the repaired delta.
Reviewed SHA-256 values:

- acquisition.py: 12b7179173b6b6de27dd65b8eb57cd7e03d8bbdb1239ca1bc159799780877bc8
- subscription.py: e16de40939a2e75055c9d02f5aa3f0cd33eea1045acb67aed68b483fe3230cf2
- twilio_incoming.py: be9aac29e7a6be4e123d58307c8ac680980221fc8931199de7e9b993a50f034c
- test_acquisition_measurement.py: 361f86401dc7cf08d7679eb1485c58b0d4ba1e4d502f6add1dfd82d7a02068c8
- test_acquisition_wiring.py: ddec41d9442946358904c29c80421cbe08ba72994f5e348dc688b362be6761e0

These checks qualify source. Exact-head hosted CI and configured GitHub Codex
re-review must complete before merge; deployment and measurement activation remain
separate release gates. The optional Cursor Security Agent failed to run on the
previous candidate and provided no security findings or successful review evidence.
