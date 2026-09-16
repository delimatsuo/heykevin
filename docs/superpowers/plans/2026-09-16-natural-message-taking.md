# Natural Take a message transition

Owner request: September 16, 2026, following a real test call in the build 39
acceptance session. The owner could read streamed conversation phrases in the
notification, but found the action buttons unclear and the immediate
unavailability response abrupt.

## Accepted behavior

- Keep **Pick up** and **Take a message**, their order, exact-call ownership,
  pending acknowledgement and existing account checks. Pick up is the primary
  phone action; Take a message is a neutral message action. Apply the app-owned
  design to the active card and full transcript. Notification customization is
  limited to existing localized titles and system action icons.
- Take a message must let Kevin's currently playing speech finish. If Kevin has
  not begun an availability offer, omit that offer and naturally say the owner
  is unavailable and offer to take a message.
- If Kevin has already offered to check availability, wait three seconds after
  the later of accepting this owner instruction and finishing current speech.
  Then say a natural equivalent of “Unfortunately, [configured owner] is not
  available. Can I take a message?” Preserve the caller's language.
- Apply the pause once to an explicit decline. Retry and acknowledgement
  recovery must not repeat speech or restart the pause. The existing unanswered
  30-second owner timeout receives no extra three-second delay or second wait.
- Preserve transcription, caller interruption and subsequent conversation.
  A stopped call, changed account, replaced stream or changed durable claim
  cannot deliver or acknowledge a stale instruction after the new waits.

## Implementation and evidence boundary

The bounded change covers the existing Voice, Gemini and ConversationRelay
engines and their shared durable owner-action consumer. Completion of model
generation is distinct from completion of queued speech. An incomplete wait
must remain retryable rather than authorize overlapping speech. Temporary
transition state must be cleaned up without losing an accepted message-taking
decision.

Verification must exercise speech boundaries, a hold that ended before the
owner's tap, exactly one three-second pause, timeout behavior, duplicate and
failed acknowledgement, delivery retry, caller interruption, teardown and
ownership changes during waits. Behavioral mutation checks must detect removal
of timing and ownership protection. Local mocks and simulator results do not
establish live provider or physical-phone acceptance.

No provider/model migration, archive, callback/text reply, follow-up product,
Dispatch, notification extension, VoiceOver requirement or accessibility work
is included. Existing build 39 is already public. A backend deployment and any
new iOS upload remain separately owner-gated; merging source does not deploy it.
