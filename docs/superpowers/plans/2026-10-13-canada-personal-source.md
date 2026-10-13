# Canada Personal source preparation

Base: `2c345b4eb370946b7ae72ca088609e158830fa07`.

The owner selected Canada as the next market to qualify. The working cost
assumption is that the incoming screening and in-app pickup flow does not need
emergency registration or an emergency monthly fee. This is provisional and
does not authorize changing provider settings. This slice spends $0 and does
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

- 284 focused Canada/US owner SMS, enrollment, forwarding and post-call identity
  tests passed; 75 runtime-safety and provisioning regressions passed.
- 58 actual Foundation/XCTest forwarding and SMS preference tests passed on
  macOS, including canonical phone identity and stale confirmation checks.
- Three backend guard mutations (disabled path, Canadian region, consent) and
  one native Canadian dial-code guard mutation were detected by the tests.
- The final app and unit-test targets compiled for generic iOS Simulator with
  signing disabled through `xcodebuild-external`. The result bundle reports
  `succeeded`, zero errors and one existing acquisition warning. No simulator
  was booted; this was compilation, not iOS test execution or device evidence.
- Fresh independent review found and verified corrections for final-read
  cancellation, stale consent acknowledgment after STOP, and canonical owner
  phone hydration. Re-review is clean. Fatal Ruff selectors and diff checks
  passed.

Hosted checks and automatic PR review must still run on the committed candidate
before merge. Their results belong to that exact GitHub head, not this local
validation record. Carrier profiles, Canadian service/suppression qualification,
deployment, handset delivery and physical forwarding/restoration remain future
owner-gated work.
