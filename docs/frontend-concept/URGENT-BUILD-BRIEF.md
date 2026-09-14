# Approved urgent-call prototype update

Owner approved proceeding on September 14, 2026. Extend the existing reviewed
HTML only; this is a fictional preview of the feature being implemented, not
native refactor approval. Base HEAD 9be7200730b0a55318c767bc2e10ae3fceae8d24.

Builder: headless agy gemini-3.7-flash-high. Parent owns Git and verification.
Only edit docs/frontend-concept/index.html in
/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/frontend-concept. Read the
existing BUILD-BRIEF.md and PANEL-REVIEW.md; their isolation, privacy, visual,
accessibility and state ownership rules still apply. No other file edits,
Git mutations, external calls, dependency installs, agents, or browser control.
Never read .env files or credentials. Scratch only under configured TMPDIR.

Implement all of this:

1. Add an Urgent call scenario using the existing fictional caller fixture.
   Show a clear Urgent label and concise fictional reason, with Pick up and
   Take a message using the same call-ID-bound reducer as ordinary live calls.
2. Both desktop and mobile notification previews need a collapsed body-tap
   affordance (opens that exact call), a separate Expand notification button,
   and an expanded action row: Pick up and Take a message. No nested buttons.
   Label this a simulation; never imply all iOS surfaces always show buttons.
3. Dismiss notification is presentation only: no call command. The live entry
   remains accessible; Kevin keeps handling the caller. The separate simulated
   30-second owner wait still ends normally. Provide a review control labelled
   Simulate 30 seconds without an answer so this can be exercised immediately.
   If still waiting, transition to taking-message and append Kevin's unavailable
   line; no transition if joining, on-call, ended, or a newer call/scenario.
4. Actions share one lifecycle: duplicate or opposite taps during a pending
   request do nothing; pickup failure keeps that call retryable; take-message
   requests show Requesting until the simulated acknowledgement, then Taking a
   message. Add a Message request failure scenario with explicit retry. Never
   show Recording or pretend audio is being recorded. Caller remains connected.
5. An ended notification has no enabled call actions and opens its summary.
   Old A notification cannot answer active B. Reset/end/new call during pending
   callbacks prevents resurrection. Preserve all existing scenarios and guards.
6. Details-hidden mode keeps current stage and Urgent label where appropriate
   but no caller name or reason. Urgent alerts disabled in the saved Kevin
   preferences suppress simulated urgent notification/ringing, while the call
   stays visible in the app. Saving failure must preserve the saved preference.
7. Keep the existing Calls + Kevin design, mobile 320px and 200% text support,
   focus handling, keyboard access and at least 44px targets. No new product
   tabs, callback/SMS/blocking/reminders, fake Critical Alert/DND bypass promise,
   or global redesign. Retain the network-blocking CSP and memory-only state.

Return JSON {node, changed_files, implemented_scenarios, checks_run, known_limits}.
Parent tests the actual browser and independently reviews the diff.
