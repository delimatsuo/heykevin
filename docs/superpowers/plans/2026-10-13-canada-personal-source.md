# Canada Personal source preparation

Base: `2c345b4eb370946b7ae72ca088609e158830fa07`.

The owner selected Canada as the next market to qualify. Twilio confirmed on
October 13 that emergency registration and its monthly fee are not mandatory
for the described incoming-only screening and in-app conference pickup flow.
That answer does not authorize changing provider settings or establish live
account configuration or billing. This slice has a $0 incremental budget and does
not activate Canada, deploy either environment, buy numbers, register senders,
change credentials or IAM, or place calls or messages.

## Acceptance criteria

1. **Canada SMS is separate and default off.** A disabled path constructs no
   provider or database client. It cannot reuse the US campaign or messaging
   service. Existing US enrollment guards and payload behavior remain intact.
2. **Identity and consent are authoritative.** A Canadian sender and owner
   recipient must resolve to Canada, belong to one fresh active assignment,
   and have explicit consent bound to that sender, recipient, disclosure,
   collection time, source, and opt-out revision. An authenticated explicit
   consent action cannot clear carrier STOP or lifecycle holds. Generic
   profile writes cannot manufacture consent.
3. **Enrollment has one bounded mutation.** Exact account, project, service,
   assignment, consent, routing and membership checks precede at most one
   membership create. Conflicts refuse transfer. Independent readback and
   frozen routing checks establish success. Cancellation and ambiguous
   outcomes cannot become successful enrollment or trigger a retry.
4. **Owner summaries keep their exact sender.** Canadian summaries require
   the approved Canadian service and current consent/membership. App-off,
   STOP, callbacks and identity-bound provider opt-out handling remain in
   force. Enrollment checks during sending never create membership.
5. **Canadian forwarding guidance is nonactionable until qualified.** Neither
   native fallback nor server instructions may supply generic Canadian dial
   codes. Carrier and plan eligibility plus a complete restorable baseline
   are prerequisites for later qualification. Deactivation or clearing all
   forwarding is not described as restoration. US behavior is preserved.
6. **Evidence is local and synthetic.** Focused unit tests, adversarial guard
   probes, native compilation and independent review precede publication.
   Passing these checks does not establish carrier behavior, handset SMS
   delivery, live pickup, billing, deployment or emergency requirements.

## Graph and ownership

Two independent implementation nodes use isolated worktrees: backend SMS and
native forwarding/consent. The master alone integrates their file-disjoint
outputs, checks both returned artifacts against the expected set, and owns
Git. A fresh-context reviewer receives the integrated diff and these criteria.

Implementation uses Sol within the existing Codex session because this slice
has no paid external-model allocation. Unknown token usage is recorded as
unavailable. Hosted validation is allowed only after exact-head admission
establishes $0 total incremental Actions and AI cost; provider activation is
outside this slice.

## Local validation collected 2026-10-13

- 399 focused Canada/US owner SMS, enrollment, forwarding, post-call identity,
  runtime-safety and provisioning tests passed on the corrected local candidate.
- 58 actual Foundation/XCTest forwarding and SMS preference tests passed on
  macOS, including canonical phone identity and stale confirmation checks.
- Six focused guard mutations were detected by the tests, covering backend
  consent/enrollment guards and the native Canadian dial-code guard.
- A loopback HTTP probe of the actual consent method, session configuration
  and response models confirmed a delayed response after 16.2 seconds. Restoring
  the old request or resource timeout made the same probe fail. HTTP errors and
  cancellation each produced one attempt and an unconfirmed result. Consent
  now uses a dedicated 60-second request and 90-second resource budget; ordinary
  API budgets remain unchanged. Datastore latency is not universally bounded,
  so a client timeout still requires refresh rather than an assumption of rollback.
- The final app and unit-test targets compiled for generic iOS Simulator with
  signing disabled through `xcodebuild-external`. The result bundle reports
  `succeeded`, zero errors and five existing warnings. No simulator
  was booted; this was compilation, not iOS test execution or device evidence.
- Fresh independent review found and verified corrections for final-read
  cancellation, stale consent acknowledgment after STOP, and canonical owner
  phone hydration. Hosted review subsequently identified missing enrollment
  recovery, overbroad Canadian consent gating and premature native transport
  timeouts. Consent now stays pending
  until verified membership and conditional promotion, with an explicit retry
  path; legacy and non-Canadian profiles retain ordinary preference controls.
  Independent re-review of those corrections is clean. Fatal Ruff selectors
  and diff checks passed.

All nine required hosted checks passed on initial head `2301308`; its hosted
review produced the two findings described above. The corrected candidate must
receive its own exact-head checks and review before merge. Read-only inspection
of the separate Cursor security automation found that its initial run was
blocked by a team usage limit. The current automation lists only `PR Opened`
as its trigger, and the current team Spending dashboard shows on-demand usage
off. It is unavailable review evidence, not a successful security review; no
rerun or account setting change is part of this slice. Standard public Ubuntu
Actions jobs and existing included Codex review allowance admit the bounded
PR update at $0 incremental cost. Carrier profiles, Canadian service/suppression qualification,
deployment, handset delivery and physical forwarding/restoration remain future
owner-gated work.
