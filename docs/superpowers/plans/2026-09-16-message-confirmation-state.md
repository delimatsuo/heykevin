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

## Local evidence — September 16

Source candidate: `b159585b5227332e6e952ae1e140d4ca8e0e5f1e`. Independent review
passed the coordinator, both call surfaces, regression tests, and owner-intake
record. One test synchronization weakness was corrected before execution: the
stale-response assertions now wait for reconciliation to finish. No source
defect remained in review.

- **46 iOS tests passed**, zero failures/skips: 30 call-action tests and 16 live
  observer tests. Wrapper result: `kevin-confirmation-tests-20260916T211615Z-90348`.
- **Three existing native UI smoke tests passed**, zero failures/skips, covering
  active-card/detail navigation, return from Kevin/Account, and fixture action
  isolation. Result: `kevin-confirmation-ui-20260916T211728Z-91069`. The new recovery
  control's placement and enabled conditions were inspected in source; these
  smoke tests do not establish its runtime behavior in an unknown-outcome state.
- **141 backend contract tests passed** in the owner-action and message-transition
  modules. No backend source changed.
- **All five mutations were detected** by their targeted behavioral regression:
  disabling automatic exact GET (`kevin-confirmation-mut-auto-get-20260916T212340Z-95786`),
  removing expected-pending handling (`kevin-confirmation-mut-pending-20260916T212435Z-97788`),
  removing exact response identity validation (`kevin-confirmation-mut-identity-20260916T212834Z-3471`),
  removing account/session ownership (`kevin-confirmation-mut-auth-20260916T213757Z-13067`),
  and removing call/revision ownership (`kevin-confirmation-mut-scope-20260916T213855Z-14640`).
  Each ran exactly one test and failed on assertions, rather than compilation.
  The pending mutation reproduced the reported warning text. The original source
  was restored byte-for-byte after every probe and matched the candidate commit.
- The restored source then passed the same **46 iOS tests**, zero failures/skips,
  in `kevin-confirmation-restored-20260916T214020Z-15032`. Coordinator SHA-256:
  `a185ae91ce31949f268cfa09e623d00c713bf86b9ea1b20b25606fc92ff23452`.

All iOS invocations used the required Xcode wrapper and explicit project, with
the iPhone 16 / iOS 26.0 simulator. Unrelated Xcode work delayed execution; no
external process was stopped or lock bypassed. Result bundles are in the managed
XcodeStorage Results directory. These are local/source checks, not phone acceptance.

Actions preflight: public repository `delimatsuo/heykevin`, personal billing owner
`delimatsuo`; seven Python shards, Python quality and required fail-closed `Test`,
all bounded standard Ubuntu jobs with PR-scoped cancellation. Branch push and
main merge do not deploy; the PR adds one nine-job validation run. Expected
chargeable cost is **$0**, with no paid allocation used. September paid usage
observed at 21:40 UTC was **$70.723072867**, Hey Kevin net $0; no run was active
or queued for this repository. Exact final-head CI and automatic Codex review
must complete before merge and are recorded on the associated PR.
