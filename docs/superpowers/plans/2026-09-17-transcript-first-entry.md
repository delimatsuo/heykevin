# Transcript-first notification entry

Owner approved September 17, 2026 after the staff engineering, privacy and UX
panel agreed on the bounded workflow below. This extends the native call
screening interface; it does not reopen deferred product work.

## Acceptance contract

- A notification body tap or **Read transcript** opens the exact authenticated
  call's full conversation directly, without a compact-card navigation step or
  a call-control action.
- **Take a message** opens the same conversation and uses one existing owner
  operation, with truthful pending, confirmed and uncertain states.
- **Pick up** connects the exact call and keeps its captured screening transcript
  readable in the connected screen, labeled **Before you joined**. Mute, speaker
  and end remain visible. Connecting does not wait for a transcript fetch.
- Caller identity and any existing server-provided screening reason appear with
  an honest fallback. The app never parses notification prose or invents a reason.
- Earlier transcript lines stay in place while the owner reads them. New lines
  follow automatically only while the owner is already near the bottom.
- Live-call entry remains available from Calls, Kevin and Account Settings.
  The compact card remains an optional dashboard entry point.
- Owner correction later September 17: call cards and connected screens follow
  the same system appearance as the surrounding app. Light mode uses light
  surfaces; dark mode uses dark surfaces. This supersedes the September 14
  plan's always-dark active-call card. No separate theme setting is added.

The first two registered notification actions remain Pick up and Take a message;
Read transcript is third. iOS controls how many actions appear in each layout.
The notification body is the dependable passive entry point. Lock-screen previews
continue to respect the owner's system settings.

## Boundaries

Preserve exact call/account/session ownership, passive dismissal, one action
operation, natural speech completion and the conditional three-second pause,
and the single 30-second urgent wait. No new post-pickup transcription, persistent
transcript copy, recording, archive, callback, text reply, Dispatch, or retention
change. VoiceOver qualification remains deferred by the owner.

The new `screening_reason` is existing extraction output stored on the current
RTDB call node. Its write must preserve timestamps and all arbitration fields,
reject missing/ended/replaced/claimed calls, and match the authenticated stream
token. The existing authenticated exact-call status endpoint returns it. It is
screening context, not verified identity or a guarantee that the caller's claim
is true. Older servers/records return an empty reason. Backend deployment is
needed to supply the new field to an updated app.

## Reproduction before implementation

Base: `04d796a5c0e1af1b48fb930fb925374082504a7f`.

Three native-root fixture checks passed before a route change: cold entry,
cold entry with an announcement due, and warm entry while an announcement was
shown. These results do not establish the owner's physical notification path.
They did not justify an announcement/router rewrite.

The focused `NotificationEntryTests` reproduction failed when root discovery
adopted the exact requested call during the notification's status read. The
captured scope was empty; discovery changed it to that same call at the next
revision. The old strict equality test incorrectly sent this validated live call
to the historical/unavailable fallback. The failure was observed in the managed
`kevin-notification-race-repro-20260917T140801Z-9984` result bundle.

The correction may admit only that one empty-to-requested-call transition.
Different calls, multiple revisions, cleared/recreated calls and changed account
generations must still fail. Existing notification and root presentation code
remain the routing authority.

## Verification and release

Record local results and independent review in the implementation record. Test
default/read/message/pickup dispatch, duplicate actions, invalid payloads, ended
and unknown states, account changes including A-B-A, replacement calls, captured
transcript ownership, scroll position and the connected controls.

An installed candidate still needs owner phone checks for cold/warm/locked
notification entry, message-taking and pickup. Simulator fixtures do not prove
APNs delivery, unlock behavior, microphone/audio routing or live speech timing.
No phone or VoiceOver pass is claimed here. A reviewed source merge does not
authorize backend deployment or an App Store/TestFlight upload.
