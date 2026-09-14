# Hey Kevin — Frontend concept review

Date: September 14, 2026. Baseline: `66f8a446e0e873ecb280c39aaab60f7cf0448a36`.

The owner requested an expert panel and an interactive HTML proposal, with freedom
to rethink the interface. This is a design study for review. It does not approve a
native refactor, activate a deferred capability, or change the current production
release. Earlier HTML previews are references, not the chosen direction.

## Panel decision: ready for owner review

Three independent roles reviewed the same raw Swift/source and proposal, then
completed one debate round. All six consensus items received support and there
are no unresolved P0/P1 plan objections. Visual and interaction verification is
was required before delivering the HTML and is now recorded in
[VERIFICATION.md](VERIFICATION.md). The staff and privacy reviewers separately
approved the finished artifact after the repair and verification loop. Native
implementation remains subject to the owner's design approval.

| Reviewer | Harness and model | Initial decision | After debate |
|---|---|---|---|
| Staff engineer | Codex, gpt-6-astra, high | Approve with conditions | Approve with verification conditions |
| Security and privacy engineer | Codex, gpt-6-astra, high | Approve with conditions | Approve |
| Product UI/UX engineer | Codex, gpt-6-astra, high | Approve with conditions | Approve |

Graph: raw discovery → three independent reviews → code reduction → one debate
round → pinned proposal → HTML builder → independent artifact/browser review.
Expected/returned panel outputs: 3/3 initially, 3/3 after debate. The builder is
separate from the reviewers; the parent owns Git and checks actual artifacts.

## Consensus recommendations

1. **P1 — Calls first, live call always reachable.** Replace the mostly empty
   Live landing tab with Calls. Elevate the current caller above the chronological
   journal. Keep a return-to-call surface available from Kevin and Settings.
2. **P1 — Caller, reason, stage, action.** Put the useful reason before the
   transcript. Distinguish joining from connected, and taking a message from
   ended. Every action and notification stays attached to its selected call.
3. **P1 — Honest state.** Local forwarding setup is unverified. Caller-provided
   names have no verified-identity shield. Simulated appointment confirmation
   must say simulated; no claim of a sent text or real provider success.
4. **P2 — Kevin owns answering behavior.** Move screening preferences, business
   knowledge and setup out of the call journal. Account, plan and support live
   behind a labeled Settings control. Personal mode hides business features;
   changing mode does not change a subscription.
5. **P2 — A warm journal, not a metrics dashboard.** Use ivory, ink, subtle rules,
   readable system typography, restrained cobalt and a forest pickup action.
   A single dark live-call panel provides emphasis. Use an editorial serif only
   outside the product, in the review presentation.
6. **P2 — Review the failures too.** Include notification redaction, stale alerts,
   pickup failure, loading/empty/error history and a failed preference save.
   Review controls stay outside the product navigation.

## Alternatives and debate

| Question | Consensus and reason |
|---|---|
| Calls + Kevin versus Today + Calls + Kevin | All three chose two tabs. Today lacks a distinct current user job and would tend to invent a persistent work queue. Reconsider only with evidence that cross-call follow-up is the dominant job. |
| Keep a permanent Live tab? | Use a global call-specific surface. Keep the current call accessible without dedicating an idle destination to it. |
| Ignore versus Take a message | Take a message explains the consequence; the call and transcript continue until the caller ends. |
| Privacy preference in the app? | Notification redaction is a review scenario illustrating iOS preview settings, not a new production setting. |
| Modern glass throughout? | Use restrained floating navigation, solid readable content and reduced-motion support. Visual novelty does not justify unclear call state. |
| A broader business work-management product? | Excluded. Existing calls, read status and appointment requests are sufficient for the concept. |

## Required changes before building

All were incorporated in the build brief:

- Explicit call IDs, phase transitions and generation-guarded deferred work.
- Setup remains unverified even after opening instructions.
- A visible fictional-concept label on desktop and mobile.
- Memory-only fixtures, no real data, no remote assets or network requests,
  no persistent storage, no permission APIs, and no actionable telephone,
  message, calendar, authentication, billing or deletion integrations.
- Restrictive CSP blocking connections, forms, objects and frames.
- Appointment and preference failure states retain the relevant information.
- Keyboard/focus behavior, 44px targets, measured contrast, long text, reduced
  motion, 320/390px widths and 200% text scaling in acceptance.

## State contract

| Event | Permitted state | Result and invariant |
|---|---|---|
| Start demo call | No active call | New stable call ID; screening, then waiting after a guarded delay. |
| Pick up selected call | That ID is active and screening/waiting | Joining; duplicate requests disabled. |
| Joining succeeds | Same ID, same generation, still joining | Simulated on-call; never infer success merely from the click. |
| Joining fails | Same ID, same generation, still joining | Return to waiting with an inline retryable error. |
| Take a message | Same active ID, screening/waiting | Taking-message; transcript continues and the call remains active. |
| Caller ends / end connected demo | Same active ID | Ended detail; clear only that active ID; invalidate pending completion. |
| Open old A alert while B is active | Alert target is A | Open ended A without pickup; preserve B and its global return control. |
| Reset or switch fixture scenario | Any | Increment generation and clear pending timers before replacing state. |
| Save preference fails | A draft is pending | Retain editable draft and previous effective value; no success indication. |
| Change to Personal | Explicit local confirmation | Hide business settings/actions; preserve the same fictional subscription. |
| Confirm appointment | Business mode and pending request | Adding → explicitly simulated success, or retained request with retry. |

## Unresolved objections

- Staff: none at plan level; verify real artifact behavior and color contrast.
- Privacy: none after isolation and state conditions were adopted.
- UI/UX: none after navigation, copy and responsive requirements were adopted.

## Completed verification scope

- Calls → detail → back; search and All/Unread/Spam filters; read state.
- Screening → waiting → joining → on-call → ended; take-message continuation.
- Failed pickup, caller end during joining, reset during joining, rapid duplicate
  pickup and stale A notification while B remains active.
- Notification details hidden/visible, business/personal, draft/save failure,
  appointment request/failure/simulated confirmation, setup and account panels.
- Empty history, retained-history failure/retry, loading and long-content fixtures.
- Browser layouts, keyboard traversal, modal Escape/focus return, 200% text,
  reduced motion, ordinary-text contrast ≥4.5:1 and no external requests.
- Concept-only file diff. Browser evidence does not qualify APNs, CallKit,
  carriers, native accessibility or the production backend.

## Source evidence and design references

- `ios/Kevin/Views/ContentView.swift`: Live/Recents/Settings navigation, idle Live
  screen and disabled outbound text reply (`kTextReplyEnabled = false`).
- `ios/Kevin/Views/CallHistoryView.swift`: chronological/read state, caller detail,
  owner-confirmed appointment request and outcome handling.
- `ios/Kevin/Views/SettingsView.swift`: mixed account/behavior/setup configuration;
  forwarding activation records local intent, not carrier verification.
- `ios/Kevin/Theme/Theme.swift`: current semantic colors and 48/56px actions.
- [Current PRD](../../kevin-prd.md): answer-now/follow-up job and release contracts.
- Apple's [Liquid Glass overview](https://developer.apple.com/documentation/TechnologyOverviews/liquid-glass)
  and [adoption guidance](https://developer.apple.com/documentation/TechnologyOverviews/adopting-liquid-glass)
  support clear content/navigation hierarchy and restrained material use.
- Apple's [tab-bar guidance](https://developer.apple.com/design/human-interface-guidelines/tab-bars)
  and [notification guidance](https://developer.apple.com/design/human-interface-guidelines/managing-notifications)
  inform predictable navigation and respecting system notification choices.

## Recommended next action

Review the interactive HTML for navigation, visual identity and the core call
journey. Native implementation requires the owner's subsequent approval and a
separate plan bound to the actual iOS/backend contracts.
