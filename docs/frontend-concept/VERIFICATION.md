# Frontend concept verification

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
