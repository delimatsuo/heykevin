# Frontend concept verification

## Current urgent-call revision

Date: September 14, 2026. Artifact: `index.html`.
SHA-256: `942c45e608987d8d539382c851b39860b1c0734e5d67bea92636d32a6a47cc21`.
Base: `66f8a446e0e873ecb280c39aaab60f7cf0448a36`.

The approved revision adds expanded Pick up / Take a message notification
controls, passive dismissal, an urgent-call scenario, a server-preference
simulation, explicit request failure/retry, and a bounded owner wait.

The independent Codex reviewer (gpt-6-astra, high) rebound the final hash and
approved the source with no remaining findings. Repairs addressed suppressed
navigation, presentation-control focus, bootstrap/replay timer scheduling,
phase-accurate suppressed/dismissed text, scenario call IDs, failed requests that
straddle the deadline, honest ended summaries and focus during automatic updates.

Parent verification on the actual source and in-app browser:

- Pick up shows Connecting before connected controls. Message requests show
  Requesting and disable competing actions; failure remains retryable.
- Dismiss changes presentation only. Restore and expand/collapse retain focus.
  A dismissed call continues to the 30-second fallback.
- Saving Urgent alerts off suppresses the urgent preview; View in app opens the
  exact call. After hangup, suppression text correctly says the call ended.
- An old Marcus notification opens Marcus's summary while Maya remains live.
  Its actions cannot answer Maya.
- Hidden details omit caller name/reason on desktop and mobile. At 320px and
  200% product text, long content wraps; document width 305px within 320px.
  The expanded redacted notification and navigation remain reachable.
- Immediate urgent-call hangup produces an honest incomplete-message summary.
  Seven extracted-source outcome cases cover screening, waiting, joining,
  requesting, taking-message, a received message and a connected call.
- Real automatic timeout retains the live Calls back-button focus. A focused
  notification Pick up action moves to its own enabled body control when timeout
  disables the actions; later transcript updates preserve that focus.
- Eight extracted-source timer probes pass. Five deliberate mutations are
  rejected: removing generation, active-call or record-identity guards, extending
  the original deadline, or retaining a dead timer after a pending-action failure.
- Inline JavaScript compiles and `git diff --check` passes. No network APIs,
  persistent browser data, real calls or platform notification services are used.

These are local simulation checks. They do not qualify APNs ordering, lock-screen
behavior, CallKit, carrier routing, native accessibility or production delivery.
The broader native frontend remains subject to owner design approval.

| Urgent HTML builder pass | Harness/model | Input tokens | Output tokens | Total tokens | Retries |
|---|---|---:|---:|---:|---:|
| Initial | headless agy / gemini-3.7-flash-high | 272113 | 74352 | 346465 | 0 |
| Repair | headless agy / gemini-3.7-flash-high | 312322 | 48739 | 361061 | 0 |

The parent pinned the contract, repaired reproduced timer/outcome/focus issues,
and owns verification and Git. The prior concept's evidence below applies to
its earlier hash; it is retained as history rather than final-revision evidence.

## Earlier frontend concept verification

Date: September 14, 2026.

Artifact: `index.html`.
SHA-256: `30be67ad714d4cc6bd78fc7c46670b98c4468c1b22ecc72ce004686cfc9ae3ba`.
Repository baseline: `66f8a446e0e873ecb280c39aaab60f7cf0448a36`.
Worktree: `.worktrees/frontend-concept`, branch `codex/frontend-concept`.

## Independent reviews

The three-role planning panel returned 3/3 initial reviews and 3/3 debate
responses. Its consensus is in `PANEL-REVIEW.md`.

Two reviewers independently inspected the actual implementation. Their initial
reports were reduced into eight distinct issues. Repairs addressed pending-call
ownership, route precedence, active-call access, failed preference drafts,
appointment conflicts, accurate phase labels, unsaved controls and unsupported
privacy claims. A second review found two remaining operation-replacement races;
both were reproduced and fixed.

| Final artifact reviewer | Harness/model | Final disposition |
|---|---|---|
| Staff engineer | Codex / gpt-6-astra / high | Approved, no findings |
| Security and privacy engineer | Codex / gpt-6-astra / high | Approved, no findings |

Both reviewers rebound the final hash above. The staff reviewer ran 16 focused
probes against extracted actual JavaScript with controlled timers. Removing the
pickup completion guard made the ended-during-joining assertion fail. Removing
the appointment operation-token guard made the cancelled-operation replacement
assertion fail. These probes establish local state behavior, not native calls.

## Browser verification

The parent exercised the HTML in the in-app browser with fictional fixtures:

- Screening replay reaches waiting; pickup shows joining and then connected.
  Mute/speaker give visible feedback; ending removes answer controls.
- Caller end during joining remains ended after the deferred callback.
- Take-message keeps the call active and appends a caller transcript turn.
- Kevin and Settings retain a correctly labeled route to the current call.
- Failed preference save preserves its draft and prior effective value; retry
  saves. Cancelling a pending save and opening Setup leaves Setup open and the
  cancelled preference unchanged. Escape restores focus to the trigger.
- Business profile edits persist in the page. Personal mode hides business
  controls; the fictional Business subscription remains unchanged.
- Appointment pending state disables Decline. Failure retains the request;
  retry produces explicitly simulated confirmation with the proposed time.
- An old Marcus alert opens ended Marcus while Maya stays active. Desktop and
  mobile redaction omit caller name and reason; mobile caller-end targets Maya.
- Search retains focus while typing; All contains six calls, Spam one, and
  opening David reduces the unread count from three to two.
- Empty history, loading skeletons, quiet journal and retained-history error
  render. Retry clears the error while preserving six records.
- Desktop layout, 390px and 320px viewport settings, and 200% product text were
  inspected. Long names/reasons wrap without horizontal page overflow. The
  global status also wraps at larger text; navigation remains reachable.
- Default product buttons have targets of at least 44px and no supporting text
  below 14px. Rendered default product/notification text samples had contrast
  of at least 4.73:1. The error Retry contrast was repaired and rechecked.
- Reduced-motion CSS disables repeated animation and smooth scrolling. OS-level
  motion preferences and native assistive technologies were not qualified.
- No browser warning/error entries were observed after the interaction checks.

Inline JavaScript passes `node --check`. Static inspection found no external
asset URLs, live network API calls, browser persistence, permission prompts or
actionable telephone/message links. The CSP blocks network connections, form
submission, external scripts/fonts, frames and objects.

## Implementation record

The parent pinned the panel brief and owns the artifact audit and Git. Two
sequential builder passes used headless `agy`, model `gemini-3.7-flash-high`.
The parent then repaired independently reproduced issues and visual defects.

| Builder pass | Input tokens | Output tokens | Total tokens | Retries |
|---|---:|---:|---:|---:|
| Initial concept | 190935 | 60107 | 251042 | 0 |
| Interaction repair | 289978 | 93834 | 383812 | 0 |

Only concept files are changed. No Swift, backend, provider, workflow, release
configuration, production data or real communication was changed. No hosted CI
was triggered for this local HTML review artifact. The native refactor still
requires the owner's subsequent approval.
