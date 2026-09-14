# Prototype interaction repair

Parent owns Git and design. Worktree and base are unchanged from BUILD-BRIEF.md.
Only edit docs/frontend-concept/index.html. Read PANEL-REVIEW.md and the original
brief. No other writes, Git mutations, external requests, installs, browser,
agents, native/backend changes, environment/credential reads or integrations.

Two independent reviewers blocked the first artifact. Repair the actual source,
not just the report. Preserve the parent's new CSS refinement block, thin phone
frame, desktop composition, journal rules, editorial copy and review labels.
Small CSS additions for the following controls/accessibility are allowed.

## Required state repairs

1. Pickup entry requires callId === activeCallId, current record identity and
   phase screening/waiting. It navigates to that call's live view immediately.
   Completion requires the same generation, same active ID, same record and
   phase joining. Ending during joining must remain ended permanently. Reset
   invalidates pending work. Duplicate clicks produce one operation.
2. Take-message has the same active-ID and answerable-phase entry guard. It
   keeps the call active; its delayed transcript append checks generation,
   identity and taking-message phase. Ending sets a terminal Left message
   outcome. All ending paths use the same guarded reducer behavior.
3. Establish explicit route precedence. Tapping Settings, a live strip or any
   notification from Kevin must actually show that destination. Tabs always
   show their roots. Back from Settings returns to its origin tab; live/detail
   back returns to Calls. The active call stays independent from selected call.
4. One reusable phase-aware global live strip appears across Kevin, Settings,
   other call detail, and Calls when its large answerable hero is absent. It
   identifies the active caller, correct phase and View. Show Pick up ONLY for
   screening/waiting. Never show Waiting/Pick up for joining/on-call/message.
5. Refresh notification text on every state change. Redaction removes caller
   name AND reason. Use accurate phase or ended text, including generic ended
   text when redacted. Opening old Marcus alert while Maya is active opens
   ended Marcus without answer controls and leaves Maya's global strip usable.
6. Appointment entry requires business mode, current record/request identity,
   pending status, no operation already pending. Disable Decline while adding;
   reducer also rejects it. Completion checks generation, request identity,
   mode, pending status and operation ownership. Mode switch invalidates pending
   business operations cleanly. Failure retains request; retry works. Success
   explicitly says Confirmed in demo, never claims a sent message/calendar event.

## Preferences, dialogs and navigation ergonomics

7. showModal honors false from onConfirm, retaining the dialog and draft.
   Escape and explicit close return focus to the triggering control, including
   when render replaced its node. Dialog has accessible name via labelledby.
8. Replace unsaved Urgent Emergency Keywords and free-text greeting-style
   invention with the actual controls in the pinned brief: Screen all calls
   (off means contacts ring through), Urgent alerts, Block robocalls. Booleans
   are editable drafts; show their actual effective values on Kevin. Keep custom
   business greeting in its existing business editor. Do not invent keyword
   scoring/settings. Save is a ~700ms local simulation with pending label and
   disabled duplicate action. On pref-save-failure the first attempt retains
   open dialog, inputs/draft, error and previous effective values. Retry saves.
   Closing/resetting/switching scenario before completion must not apply it to
   another dialog/session. Scope by generation and modal operation identity.
9. Preserve focus/caret while typing search and retain scroll position on
   same-view asynchronous updates. No timer may jump the user to the top or
   steal focus. Intentional navigation can start at the top. Keyboard support
   must remain usable. Mark filter selection with aria-pressed or valid tabs.
10. Settings/profile entry should visibly say Settings (not just AR initials).
    All product controls need >=44px targets and readable >=14px support text.
    Ensure responsive content can wrap at320px, including stage strip/buttons.

## Truthful, necessary scope and copy

11. Replace the AES-256/90-day purge and export local-purge guarantees with
    accurate fictional in-memory data/reset explanation. Remove Export Journal
    and Trade Dispatch/Jobber controls: these add scope beyond this concept's
    required call/behavior/account flows. Keep calendar only as an explicitly
    unconnected explanatory concept. No external requests or links.
12. Remove all carrier dial strings from setup instructions. This fictional
    number must never be presented as something to activate. Explain check
    forwarding on a real device in the later native version; status stays
    Setup needs checking. No ready/protected/verified claim after clicking.
13. Plain product language in dialogs: no CallKit/telurl, StoreKit2, tokens,
    transport/signaling, API credentials or architecture jargon. Explain what
    the proposed native control does and what can be explored in this demo.
    Label simulated outcomes as simulated without filling product screens
    with engineering disclaimers. Remove service dispatch from mode copy.
14. Keep existing six fictional records, business/personal distinction and the
    same fictional subscription across modes. All should include every record;
    Spam remains a useful filter. Never introduce outbound text reply.

## Concrete verification expected from source behavior

- Pick up, end before1200ms, callback cannot resurrect. Reset also cancels.
- Kevin -> global View, Settings and stale notification destinations visible.
- Joining/on-call/message reachable everywhere, never stale Waiting/Pick up.
- Take message adds one transcript turn, end yields Left message terminal.
- Appointment double-confirm and decline-during-confirm cannot override state.
- Preference failure remains open with edited checkbox draft; effective value
  unchanged; successful retry persists; cancel-before-completion applies nothing.
- Search supports consecutive typing without focus loss. Modal Escape restores
  connected trigger focus. Same-view timer updates retain reader position.
- No fetch/XHR/WebSocket/storage/permissions/tel/SMS/calendar/network behavior.
- Inline JS parses with node --check. No broad production/native tests needed.

Report touched file, brief fixes, syntax result and actual usage. Parent reviews
and exercises browser independently. Do not self-grade the artifact approved.
