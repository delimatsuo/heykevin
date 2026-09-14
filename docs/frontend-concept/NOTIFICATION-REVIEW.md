# Notification actions and urgency — September 14, 2026

**Current decision:** The owner approved Pick up, Take a message and the urgent-call
handoff. The HTML demonstrates that scope. The broader native frontend refactor
still requires design approval. This replaces the earlier recommendation-only
status and one-action release description.

The [current PRD N1](../../kevin-prd.md#n1--finish-the-screening-notification-enhancement)
and [handoff contract](../superpowers/plans/2026-09-14-urgent-call-handoff.md)
are the current requirements.

## Agreed interactions

| Interaction | Meaning |
|---|---|
| Pick up | Request connection to the exact live call; show pending until the server confirms the action and the connection succeeds. |
| Take a message | Ask Kevin to continue with the caller. Stay pending until the voice engine accepts the instruction. Acceptance does not mean a complete message has been received. |
| Tap the body | Open that conversation without answering; an ended call opens its summary or an honest unavailable state. |
| Dismiss | Change presentation only. Do not send a call command or promise to suppress future alerts. |

Urgency starts or preserves one 30-second owner wait. Pickup, message-taking and
timeout share one server decision. If the owner does not respond, Kevin explains
that the owner is unavailable and takes a message. An uncertain pickup retains
its operation and supports status checking without replaying the transfer.

Urgent alerts is persisted on the server. Turning it off suppresses extra urgent
pushes and rings while screening continues. Urgent copy is generic. Use supported
time-sensitive notifications and ordinary sound without promising a Critical
Alert, Silent Mode or Do Not Disturb bypass. Urgent CallKit ringing requires
clients advertising new handoff support and reuses the incoming call when
answered. Legacy direct-ring behavior remains part of acceptance.

## Interactive review

Expand the notification to see both actions. Scenarios cover urgency, failed
pickup, failed message requests, passive dismissal, 30 seconds without an answer,
stale alerts and preference-save failure. Disabling Urgent alerts in Kevin keeps
View in app available. Hidden details omit caller name and reason.

Ended summaries distinguish a completed fictional message from a hangup.
Callbacks check generation, call identity and phase. Automatic updates retain
focus or choose an enabled control in the same region. See [VERIFICATION.md](VERIFICATION.md).
This is a fictional local simulation, not APNs or CallKit qualification.

## History and later options

The Telegram-first PRD listed Pick Up, Call Back, Text Them, Voicemail and Ignore.
Take a message now expresses the old Ignore/Voicemail intent clearly. Callback
and text reply remain possible ended-call follow-ups in the PRD; they are not
approved live-call additions.

The review found missing action UI, misleading tap copy, stale-call/account
ownership risks, duplicate transfer risks and premature acknowledgement. The
approved implementation addresses those boundaries. Its release packet must
record tests, independent review, the exact candidate and remaining device and
deployment checks before claiming production completion.

## Apple constraints

- [Actionable notifications](https://developer.apple.com/documentation/usernotifications/declaring-your-actionable-notification-types)
  use registered categories; the preview cannot guarantee every iOS surface.
- [Foreground actions](https://developer.apple.com/documentation/usernotifications/unnotificationactionoptions/foreground)
  bring the app forward and may require unlocking.
- [Time-sensitive notifications](https://developer.apple.com/documentation/usernotifications/unnotificationinterruptionlevel/timesensitive)
  remain subject to the user's notification choices.
- [Critical Alerts entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.usernotifications.critical-alerts)
  is a separate capability outside this implementation.
- [Dismissal handling](https://developer.apple.com/documentation/UserNotifications/UNNotificationDismissActionIdentifier)
  is not evidence of an owner call-state decision.
