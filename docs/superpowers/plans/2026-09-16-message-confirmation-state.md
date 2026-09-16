# Message confirmation after a natural transition

Owner intake, September 16, 2026: on TestFlight 1.3.1 (40), tapping Take a
message produced an "Outcome not confirmed" warning even though Kevin's spoken
message response was normal. The live view showed Taking message. This records
the reported behavior; it does not close the three-second timing or other
physical-call acceptance rows. No caller details are needed for this regression.

## Acceptance criteria

- An exact, valid HTTP 202 decline/message_requested response keeps the request
  pending without an outcome-unknown warning. It does not claim completion,
  issue another POST, or permit the opposite action.
- When ordinary live polling reports taking_message and this session retains
  an unfinished decline, reconcile with a GET for that exact operation. An
  ordinary response has no operation identity and cannot itself clear the
  retained operation or warning.
- Repeated observations share in-flight work. Completed actions do not start
  new reconciliation. Late GETs cannot apply after account, session generation,
  call, or lifecycle revision changes. Pickup reconciliation remains explicit.
- Only an exact, validated decline/taking_message acknowledgment marks the
  retained request complete and clears its warning. Unknown responses remain
  recoverable; conflicts and ended calls retain their existing honest outcomes.
- Check status is usable from the active call card and live transcript whenever
  an unfinished operation can be checked, including beside Dismiss after a
  call-level Taking message projection. It is not disabled just because the
  retained action blocks another POST. Existing presentation leases remain.

## Bounded implementation

Change only the iOS coordinator, the two existing call surfaces, their focused
regression tests, and this evidence record. Reuse the root observer's two-second
cadence; do not add timers, background retry loops, new endpoints, synthesized
operation IDs, or backend/provider changes. Existing strict response parsing,
direct-call behavior, notification actions, passive dismissal, and speech timing
remain intact. No release metadata or accessibility work is included.

Graph: independent backend contract audit plus master iOS diagnosis -> pinned
Flash-high implementation -> master test/mutation probes plus fresh independent
source review -> one coherent PR. Master owns Git. The backend audit confirmed
that an unqualified GET echoes an empty operation ID, while an exact GET either
acknowledges the retained decision or reports conflict. Backend source is
unchanged from the deployed natural-transition commit.

## Verification and release boundary

Exercise delayed confirmation, unknown versus valid pending, wrong operation,
timeout conflict, repeated observations, in-flight work, stale sessions/calls,
and explicit-only pickup recovery. Mutation probes must fail when automatic
GET reconciliation, exact identity validation, or post-await ownership guards
are removed. Inspect both Check status placements and retain existing control
layout smoke tests. Local simulator results do not prove physical-call behavior.

App Store/TestFlight upload remains a separate owner gate. A reviewed merge does
not change the installed build or deploy the backend.
