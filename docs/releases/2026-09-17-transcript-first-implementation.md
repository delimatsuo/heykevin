# Transcript-first implementation evidence — September 17, 2026

## Scope and source

The owner approved the expert-panel workflow, then explicitly rejected dark
cards mixed into the light interface. The accepted contract is in
[the implementation plan](../superpowers/plans/2026-09-17-transcript-first-entry.md).

Base main: `04d796a5c0e1af1b48fb930fb925374082504a7f`.
Reviewed production implementation: `c9a4828d03a200b8dc01a9201f2ab3a1d8bf9375`.
Later commits strengthen the connected UI assertion and record evidence only.
Worktree: `.worktrees/transcript-first-entry`; branch `codex/transcript-first-entry`.

Notification body/read actions enter the exact authenticated call's full
conversation. A narrowly reproduced discovery race is corrected: an empty
call scope may advance once to the same requested SID during validation.
Different SIDs, multiple revisions, session changes and replaced calls still
fail validation. Existing message/pickup operations remain the action authority.

The connected screen captures the pre-pickup conversation in memory and labels
it **Before you joined**, with persistent call controls. It does not start
post-pickup transcription or create a durable transcript copy. Earlier lines
retain their reading position when new text arrives. Call surfaces follow the
system's light or dark appearance throughout Calls, live detail, Kevin/Account
return cards and the connected screen.

The backend exposes the existing extracted screening reason through authenticated
exact-call status. The metadata transaction checks current owner, live/fresh
state, stream token, SID and action ownership, preserves timestamps, and cannot
resurrect rejected records. Optional metadata storage has a one-second budget.
A timed-out database thread can finish later; the transactional checks remain
in force. Missing/older metadata produces the honest app fallback.

## Local verification

All Xcode commands used the required external wrapper with one explicit project
and the dedicated Kevin-Native-Review simulator (iPhone 16, iOS 26.0), without
signing or provider calls. Result IDs below are under the managed XcodeStorage
Results directory; ephemeral screenshots are not release assets.

- Reproduction: three baseline root notification fixtures passed, while the
  focused same-call discovery race failed before correction in
  `kevin-notification-race-repro-20260917T140801Z-9984`.
- Backend: 162 focused tests passed, plus fatal Ruff selectors and whitespace
  checks. Command: `KEVIN_DISABLE_DOTENV=1 <clone>/.venv/bin/python -m pytest -q
  tests/unit/test_owner_call_actions.py tests/unit/test_screening_summary_push.py
  tests/unit/test_screening_summary_lifecycle.py`. Coverage includes transaction
  retries, replacement/ended/malformed calls, optional storage timeout and
  mutations removing stream-token and owner-action checks.
- Native unit suite: 292 passed, zero failures/skips, in
  `kevin-transcript-unit-20260917T142312Z-14269`, before the later presentation
  styling and test-fixture corrections. Final focused reruns are recorded below.
- Initial native UI run: 14/16 passed in
  `kevin-transcript-ui-20260917T142844Z-21690`. Both failures came from the new
  root test identifier being propagated over child identifiers. Exported
  screenshots showed the connected screen and controls were present. Removing
  that root identifier restored the child assertions.
- Final light appearance: seven focused UI tests passed, zero failures/skips,
  in `kevin-transcript-light-20260917T143629Z-49522`: connected conversation and
  empty state, live-card entry, return from Kevin/Account, bottom following,
  earlier-line scroll preservation, and warm notification over announcement.
- Final dark appearance: four focused UI tests passed, zero failures/skips,
  in `kevin-transcript-dark-20260917T144015Z-61544`: both connected states, live
  card/detail roundtrip and return from Kevin/Account. Together with the earlier
  full run and corrected connected tests, all 16 distinct UI scenarios have
  passing evidence; the full history suite was not needlessly repeated for colors.
- Visually inspected Calls, full live transcript and connected-call screens in
  both light and dark appearance. Light surfaces and dark surfaces are consistent;
  readable content and persistent controls were visible. Fictional fixtures only.

- Guard mutation run `kevin-transcript-guard-mutants-20260917T144215Z-67016`
  detected deletion of the navigation-revision, snapshot-session and reason-lease
  checks. Removing only the outer notification session check did not fail: the
  coordinator independently rejected the stale session before any request.
  Removing both redundant checks was detected by
  `kevin-transcript-auth-mutant-20260917T144411Z-72873` (one expected failure).
- Removing production snapshot capture initially survived the UI assertion in
  `kevin-transcript-capture-mutant-20260917T144442Z-74277`: the query could find
  hidden transcript text behind the connected screen. The assertion now scopes
  to the connected scroll view and requires the text to be hittable. The
  strengthened assertion detected removal of production capture in
  `kevin-transcript-capture-proof-20260917T144553Z-82271` (one expected failure).
  Every mutation was restored byte-for-byte in a finally block.

- After restoring all mutants, 23 focused notification/presentation/ownership
  unit tests passed with zero failures/skips in
  `kevin-transcript-final-unit-20260917T144704Z-84852`. The strengthened connected
  UI test passed in `kevin-transcript-final-connected-20260917T144715Z-86862`.
  These final runs used `37d36cdadb08d1a342f1d563319212ce0d199c55`; production
  source is unchanged from the independently reviewed `c9a4828` commit.

## Independent review and remaining qualification

Independent backend review passed at `afdcb4d247b14b942adc474c3cbeb950c17c130a`
(cherry-picked as `e298c3c` with identical implementation). Fresh-context native
and integration review passed at `be0f014f753bf40c67cc5870099345f68905f0f4`;
the appearance/test-fixture follow-up passed at the reviewed implementation
above. No concrete source findings remained. Reviewers did not run providers.

The connected fixture invokes the production prepareConnection snapshot hook
without CallKit/Twilio. Production cleanup's scoped snapshot removal remains
source-reviewed without a direct runtime mutation test. Account/call reset,
old-lease cleanup and snapshot visibility are covered at the AppState boundary.

Physical-device cold/warm/locked notification entry, APNs delivery, CallKit
connection, mute/speaker/end behavior, and live speech timing remain open.
VoiceOver qualification remains deferred by the owner. No physical pass is
inferred from simulator or source results.

## CI envelope

Read-only check at 2026-09-17 14:37 UTC: public `delimatsuo/heykevin`, personal
billing owner `delimatsuo`, main unchanged, no open PRs or active/queued runs.
Feature-branch push triggers no workflow; PR to main runs seven Python shards,
Python quality and stable required `Test` on standard Ubuntu runners. No macOS
job or deploy runs on this PR; main merge does not deploy. PR cancellation-aware
concurrency, 15-minute suite/quality timeouts and five-minute aggregator timeout
are unchanged. Latest successful sample totals about ten rounded runner-minutes
(approximately $0.06 gross, $0 chargeable for this public repository).

Portfolio paid Actions usage was $72.120317436; Hey Kevin paid usage was $0.
A simple linear month-end projection was $127.27, not a forecast of marginal
cost. No new paid allocation is used. The owner's $80/$90/$100 portfolio gates
remain in force; this source PR adds no chargeable Actions spend.

## Routing evidence

Three pinned builder nodes returned three successful envelopes: backend,
native presentation, and the owner's later appearance correction. Master
reviewed the actual diffs and executed the verification; builder prose was not
accepted as test evidence. Usage below comes from outer agy transport records.

Master model: gpt-6-astra
Builder model/tier: gemini-3.7-flash-high
Routing reason: pinned, bounded implementation; master owns judgment, integration and verification
agy transport=headless: true
Input tokens: 1118520
Output tokens: 169125
Total tokens: 1287645
Retries: 0 builder dispatch retries
Audit defects found: 8 categories — captured-session handling, invalid test seams/fixtures, scroll anchoring, stale reason-lease writes, unbounded optional metadata delay, pickup caller identity, propagated test identifiers, and a hidden-background transcript assertion
Audit disposition: remediated and independently reviewed; runtime limitations stated above

## Release boundary

This is source implementation evidence. No version/build number was bumped;
no backend deployment, flag change, upload, submission or public release was
performed for this slice. Backend deployment is needed to supply the reason;
an updated iOS candidate is needed for the new entry/presentation behavior.
Those provider/release steps require a separate explicit owner instruction.
