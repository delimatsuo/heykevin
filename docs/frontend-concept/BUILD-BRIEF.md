# Frontend concept builder brief

Read PANEL-REVIEW.md first. Owner requests an interactive HTML frontend proposal
for review before native refactor. This is concept exploration, not production
implementation. Base HEAD66f8a446e0e873ecb280c39aaab60f7cf0448a36, treea96ecde675099cac7ef4b1c2c3e60c2aab00c4cb.

## Builder boundary

- Model gemini-3.7-flash-high, headless agy. Strong tier because a complete
  interactive interface and carefully pinned state contract are required.
- Work only in /Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/frontend-concept.
- Read AGENTS.md, .claude/DECISIONS.md, docs/agent-operating.md. Parent owns all Git.
- Only create/edit docs/frontend-concept/index.html. Read-only all other files.
  Do not create implementation reports, scripts, tests or screenshots in the repo.
- Parent wrote panel/brief docs. Do not edit them. Never touch app/, ios/, existing
  prototypes, production configs, workflows, .env files, credentials or user data.
- No Git mutations, push/PR/merge, deploy, provider/API/network actions, dependency
  installs, browser control, child agents, or OS integrations. Source read-only
  commands and local Node syntax checks are okay. Scratch only configured TMPDIR.

## Artifact

One standalone index.html, inline CSS and JS and inline SVG icons; no packages,
remote fonts, images, CDN, requests or external links. It must work as a local
file and through a local static server. Memory-only state; reset on reload.
CSP: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
img-src data:; font-src 'none'; connect-src 'none'; object-src 'none';
frame-src 'none'; base-uri 'none'; form-action 'none'. No eval or Function.
No local/session storage, fetch, XHR, WebSocket, sendBeacon, service workers,
Notification, media capture, cookies, tel:, sms:, mailto: or Calendar links.
Escape all editable content before HTML insertion, or use DOM textContent.

## Visual direction — execute with care

Create a genuinely distinctive, extremely polished, mobile-first native app
proposal. Do not copy the two older blue/gray prototypes. Think a calm editorial
call journal, warm and intelligent, with restrained fine detail and excellent
spacing. This must look art-directed, not like a generic dashboard template.

Palette: warm ivory canvas #F6F3EC, paper #FFFEFA, ink #172924,
secondary #58675F, cobalt #3159C9 used sparingly, pickup forest #236147,
amber #8B530D on #FFF0D2, red #AA342C, rules #D8DED6. Product uses local
system sans; headings32px, caller22px, reason17px, support14px minimum. Product
headings medium with close tracking; tabular numerals on times. Fine dividers,
subtle paper surfaces, continuous 20–28px corners, generous space. No gradient
background, generic metric cards, emoji icons, giant AI orb, or feature grid.
Use clean consistent 1.6px stroke inline SVG icons. Create a simple four-pill
wave mark for Kevin. Green should distinguish pickup, not claim verified setup.

Desktop review shell: pale warm ground, small logo/wordmark upper left and
Concept 01 / Design study label upper right. Large restrained Georgia editorial
headline beside the phone (e.g. 'A little quieter. A lot clearer.'). One paragraph
explains the Calls + Kevin direction. A thoughtfully composed phone preview
around390px wide is the focal point. Compact scenario controls/rationale sit
beside or below it; keep them quieter than the product. Avoid a full-width admin
rail. The view must balance at1280–1440px. The device has thin elegant framing,
not a heavy black novelty mockup. A softly floating two-tab navigation layer is
okay; reserve its space rather than covering content.

On narrow windows the phone becomes a naturally scrolling edge-to-edge app,
review controls collapse into an accessible disclosure above/below it and the
'Interactive concept · fictional calls' label stays visible. Support320px width
and200% text scaling without clipping. A desktop device scroll area is allowed
if all content remains reachable. Do not make tiny fixed-height canvases. Main
buttons48–56px, all tap targets at least44px. Visible focus outlines, semantic
buttons/labels and native dialog with focus return/Escape. prefers-reduced-motion
removes waves/transitions; no forced scrolling of transcript or focus stealing.

## Product architecture and screens

Two persistent tabs: Calls (default) and Kevin. Labeled Settings/profile entry
at top. Review controls never appear as product tabs. Account is fictional Alex
Rivera, a business-mode sample account at a fictional home-services company.
Numbers only reserved fictional +1 202 555 0100–0199. Personal mode retains the
same fictional subscription and hides ALL business-only controls/appointments.

CALLS: confident title, date/context, small 'Setup needs checking' row linking
to explanatory setup. Chronological journal with All / Unread / Spam filters,
a working search affordance/input, caller/initial avatar, useful reason, time,
read indicator and plain-language outcome. Six varied concise fictional records,
including one owner-pending appointment. Do not use urgency heuristics as an
emergency score or identity shields. Opening a record marks it read. No persistent
follow-up/work queue is implied. A useful designed empty state and retained-list
error with Retry; loading with a short skeleton. Clear-search recovery works.

Default composition should show the benefit: one simulated waiting call from
Maya Chen ('The kitchen sink is leaking again.') in a single deep-ink live feature
panel above the journal. Name/reason/stage first; a tasteful waveform accent;
View call and clear Pick up action. Fixture label makes simulated state evident.
Repeat/Start call in review shell can replay this from screening to waiting.

LIVE VIEW: back to Calls, caller + reason, explicit screening/waiting/joining/
on-call/taking-message/ended stage. Transcript with distinct Kevin/caller labels,
a short summary, thumb-reachable Pick up and Take a message. Joining is pending;
duplicate clicks cannot create another join. Take a message continues transcript
until a separate simulated caller-end control. On-call offers local mute/speaker
if displayed and End call. A global compact live strip persists when navigating
to Kevin/Settings or ended detail of another call. An ended call has NO answer
control. Every action resolves its passed callID, never whichever call is active.

CALL DETAIL: caller, reason/summary first, clear outcome/date and transcript
expand/collapse. Call back opens a local explanatory dialog, no actual dial link.
Business appointment record shows proposed time and 'Nothing is booked until you
confirm'. Confirm flow pending->explicitly 'Confirmed in demo' retaining time;
no claim SMS was sent. Failure keeps request and retry. Hide all appointment
controls if personal mode. Avoid dead decorative buttons.

KEVIN: warm identity header, 'How Kevin answers' and compact grouped settings.
Behavior editor: Screen all calls with clear contacts-ring-through explanation;
urgent alerts, spam handling. Draft in dialog, Save changes asynchronous simulation,
saved value updates only on success; on failure preserve editable draft and
previous effective setting and enable retry. A small Business/Personal control
explains mode change before confirming, never changes plan. In business mode
include editable greeting and business knowledge/hours, with a sample save in
memory. Services/Calendar can open explanatory local concept dialogs; do not
invent provider connections. Setup needs checking stays unverified even after
viewing instructions. No activation/carrier code links or ready/protected label.

SETTINGS: account, sample plan, notification-preview guidance, help/account actions
as clear local explanatory dialogs. No real authentication, payment, provider
connection or deletion. Keep concise and visually consistent.

NOTIFICATION PREVIEW: in review shell, a tasteful lock-screen/banner illustration,
label it a simulation, details visible vs hidden via review-only control; not a
new app privacy setting. Banner click opens its specific call. Hidden mode omits
name and reason. Old notification A while B active opens ended A, no pickup on A,
and leaves B unanswered with accessible live entry.

## State and scenario contract

Read the complete state table in PANEL-REVIEW.md. Use one explicit state object
and a small event reducer/event dispatcher. Key calls by stable IDs and store
activeCallId separately from selected/detailCallId and notificationTargetId.
A generation counter plus timer registry invalidates pending callbacks on reset
or scenario replacement; callbacks also check call identity and expected phase.
Do not destroy history when starting/ending a call. Ending invalidates joining.
After Take a message, transcript remains available and updates with a caller
message; only separate caller-end makes it ended.

Review shell controls: scene/scenario selector, replay incoming call, end caller,
show notification, details-hidden checkbox, reset. Scenarios must include:
- Normal waiting call (default) and a fresh screening->waiting replay.
- Failed pickup; Retry then succeeds locally for that SAME call.
- Old notification: ended call A + active B and saved A alert.
- Quiet inbox/no active call, empty history, loading, retained-history error.
- Preference save failure, appointment confirmation failure.
- Long caller name/reason.
Keep these reachable on mobile in review disclosure. For failures make first
attempt fail and explicit retry succeed, with clear inline feedback. Reset during
joining never revives the call. Scenario changes reset generation; no stale timer
may change a newer fixture. All transitions are local simulations, not calls.

## Definition of done

Produce the complete polished and functioning artifact, not a plan or scaffold.
Every visible interactive control does something understandable locally. Run
local Node syntax check of inline script if useful; no backend/Xcode tests.
Return JSON with node, changed_files, implemented_scenarios, checks_run,
known_limits and no claim of independent approval. Parent will inspect every
changed file, test with the real browser and get an independent artifact review.
