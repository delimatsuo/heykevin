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
  available. Can I take a message?” Retain existing caller-language handling.
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

## Relay delivery boundary

ConversationRelay owns its audio queue. The transition uses non-preempting
text output and a queued, app-owned three-second silent WAV through the existing
`/static` mount. Caller interruption remains enabled. The clip is queued once
after a submitted availability offer and before message-taking text; timeout and
no-offer paths omit it. It does not use playback-receipt counts as a completion
signal or insert SSML pause tags. Twilio's ElevenLabs integration documents only
the phoneme SSML tag, even though the upstream speech provider supports more.

This follows Twilio's documented [text and play message contract](https://www.twilio.com/docs/voice/conversationrelay/websocket-messages).
Once text is submitted to the provider, the documented API cannot selectively
withdraw an offer that is queued but has not yet been heard. Queue ordering and
the WAV duration are locally testable; actual retrieval, audible timing and this
queued-offer edge remain physical-call acceptance items.

The legacy Voice engine already uses a fixed-English unavailable phrase. This
slice retains that baseline behavior; its language parity gap is recorded in
the PRD for separate prioritization. Gemini and relay continue using their
existing conversation-language context.

## Local evidence recorded September 16

- iOS: 22 `CallActionTests` and three targeted `FrontendUITests` passed. Visual
  checks covered English Business in light mode and Portuguese Personal in dark
  mode. These are simulator results; the new controls are not installed on the
  owner's phone.
- Voice transition: 16 focused tests passed. In-memory mutations removing the
  first-audio offer suppression and subsequent-chunk authority check each
  failed their corresponding behavioral test. Test transports and background
  provider helpers are isolated.
- Integrated backend: 483 focused tests passed across the shared action
  consumer, all three transition engines, legacy commands, speech and call
  lifecycle, summary notifications, urgent handoff and media ingress.
- Additional in-memory mutations proved sensitivity to the three-second
  delay, post-read call/account/stream ownership, missing stream authority,
  pending-state cleanup, Gemini suppression across a boundary timeout, Relay
  media payload shape and preservation of a caller reply during transition.
  Final Relay mutations also failed when continuation was permitted after a
  rejected final ownership check or when the pre-tool ownership check was
  removed. All 23 Relay transition tests passed after tightening these gates.
- Independent source review checked exact-call action ownership and the
  transition boundaries. Its caller-continuation and stale-ownership findings
  were repaired, including checks before model requests, tool execution and
  outbound continuation text. The reviewed consumer and Relay blobs are
  `ec74a26917a91da91d999cc7be98a437611b4662` and
  `5cb38a7197decd150511eee02cc7a77defc7897b`.
- PR review follow-up restored the released hold-detection markers so generic
  scheduling or inventory phrases do not newly trigger an owner hold. The
  first accepted Voice audio chunk now commits the local announcement before
  any later guard failure; the common final guard still blocks stale ACKs.
  Retry tests cover rejected and raised reads without a second TTS request or
  audio chunk. Mutations removing this commit point or restoring broad markers
  failed their regressions. Reviewed Voice/helper blobs are
  `3a49ddeb52e53d2c5bbc67748d887a16437874e1` and
  `43610d6847879891c737c421c74b7f675518af06`.
- Gemini send failures retain the pre-existing exception contract for silence,
  intake and recovery callers; the message transition handles failures as
  retryable. Five regressions cover this boundary. Swallowing send errors again
  failed four tests while the transition's retryable-failure control passed.
  The Gemini source blob is `52f383dfef7cde976183c93e15e063a3d2f8ca8a`.

Exact-HEAD CI and the final commit-bound review are recorded in the associated
pull request before merge. Backend deployment, a new installed iPhone build,
and physical-call acceptance of these changes remain outstanding.
