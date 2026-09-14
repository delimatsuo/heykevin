# Notification action review — September 14, 2026

Status: recommendation for owner review. No native implementation or expanded
notification action set is approved by this document. The HTML remains the
previously delivered concept; its missing notification actions are recorded here.

Baseline reviewed: `598ccc36549cb9316434135346f81f328e0cf7e8`.

## What the recorded plans say

The [original Telegram-first PRD](https://github.com/delimatsuo/heykevin/blob/407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc/kevin-prd.md#L69-L99)
explicitly described five inline actions:

| Original action | Original intended behavior |
|---|---|
| Pick Up | Join the current conversation. |
| Call Back | Arrange an immediate callback after ending the screened call. |
| Text Them | Send a caller-facing text. |
| Voicemail | Ask the caller for a message and transcribe it. |
| Ignore | Have Kevin politely explain the owner is unavailable and offer to take a message. |

This confirms the broader interaction concept existed. It does not mean all five
are approved for the current iOS release. The [current PRD N1](../../kevin-prd.md#n1--finish-the-screening-notification-enhancement)
specifies caller/reason updates, body-tap navigation and Pick Up. The September 4
handoff records only `PICK_UP_ACTION`; `AppDelegate.swift:140–152` registers exactly
that one custom action. No Ignore notification action is registered.

The native app already has an in-app Ignore control: `ContentView.swift:468–476`
sends `decline`, whose backend meaning is a `take_message` command. This is
consistent with continuing the conversation rather than hanging up.

## Recommended experience

Use two custom actions for the richer proposal:

| Interaction | Meaning |
|---|---|
| Pick Up | Request connection to the exact screened call; show connecting until it succeeds. |
| Take a message | Tell Kevin the owner will not join and let Kevin collect the message; keep the caller connected. |
| Tap the notification body | Open the matching conversation without answering. An ended call opens its summary. |
| Dismiss the notification | Dismiss presentation only. Do not silently issue a call command or promise to suppress later alerts. |

Take a message is a clearer label for the old Ignore/Voicemail decision. It is a
proposed second iOS action, not an already implemented notification feature.
The existing N1 release should not expand automatically into all five legacy
actions. Call Back can be considered for ended-call follow-up. Text reply,
blocking, reminders and additional live-call buttons remain outside this scope.

Show a collapsed preview focused on caller, concise reason and current stage,
plus an expanded preview exposing the two choices. A separate View live button
is unnecessary when the notification body already performs that navigation.
Do not promise action buttons are always visible on every iOS notification
surface. Apple documents that presentation varies and some surfaces show only
two actions; put the most relevant choices first.

## Findings to address before native delivery

1. **Prototype omission:** Both HTML notification previews only open the call.
   They omit even the documented Pick Up action and do not demonstrate the
   collapsed/expanded interaction. The next prototype revision should show
   both proposed buttons, pending/failure behavior and ended-call handling.
2. **Misleading native copy:** `push_notification.py:333–337` says Tap to answer,
   but ordinary banner tapping opens the call. Use Tap to view live or omit the
   instruction; reserve answering for the explicit Pick Up action.
3. **Call ownership:** Notification-selected call and active call must remain
   separate. An old A alert must open A without replacing or answering active B.
   `AppDelegate.swift:214–232` currently writes the payload SID into shared active
   state before lifecycle validation.
4. **Pending actions:** `CallManager.swift:127–140` does not visibly deduplicate
   notification pickup or recheck its initiating account/session after awaiting
   the response. The accept endpoint can create a fresh conference per request.
   Bind each action to account, call and operation; validate current lifecycle
   server-side and make retries reuse the same accepted operation.
5. **Take-message truth:** The decline endpoint acknowledges queueing, not
   consumption. Show requested/pending until Kevin accepts the transition;
   failure must preserve the previous usable state. Resolve pickup versus
   take-message races on the server.
6. **Privacy wording:** Redacted previews should still distinguish screening,
   waiting and taking-message states. Use Taking a message rather than Recording
   unless actual audio recording is part of the verified feature.

Findings 3–5 are source-review concerns, not reproduced production incidents.
Qualification should cover old alerts, duplicate taps, lost responses, caller
hangup during pickup, action conflicts and account changes while requests run.
Then check the installed iPhone build while locked/unlocked and with the app in
the foreground/background. No deployment or device state was changed here.

## Apple constraints

- [Actionable notifications](https://developer.apple.com/documentation/usernotifications/declaring-your-actionable-notification-types)
  use registered categories/actions and payload identifiers.
- [Notification presentation](https://developer.apple.com/library/archive/documentation/NetworkingInternet/Conceptual/RemoteNotificationsPG/SupportingNotificationsinYourApp.html)
  can show fewer actions depending on surface. This is archived Apple guidance;
  verify the actual supported iOS presentation on device.
- [Foreground actions](https://developer.apple.com/documentation/usernotifications/unnotificationactionoptions/foreground)
  bring the app forward and request unlocking when needed. Current Pick Up uses
  this option, so do not promise locked-screen answering without unlocking.
- [Dismissal handling](https://developer.apple.com/documentation/UserNotifications/UNNotificationDismissActionIdentifier)
  is not the same as ignoring or flicking away a banner. A UI dismissal is not
  reliable evidence that the owner requested a call-state change.

## Review record

Two independent Codex reviewers (staff engineering and product UI/UX) examined
the source and concept. Expected/returned outputs: 2/2. The parent reconstructed
the earlier PRD/history, checked official Apple guidance and reduced the findings.
Both recommend Pick Up plus Take a message for the richer proposal, body tap for
inspection and passive dismissal. Both distinguish that recommendation from the
current one-action iOS release scope.
