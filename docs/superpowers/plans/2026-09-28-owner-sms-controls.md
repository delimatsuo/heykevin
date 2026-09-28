# Owner SMS controls and final-call timing

Owner request: remove the text at call start, retain the conversation summary,
and allow SMS opt-out in the app and by replying STOP.

Source base: `2ca472a936af4306c47192e1ea4e89ed28c3253c`.
Branch: `codex/sms-summary-preferences`.

## Findings and scope

No dedicated call-start SMS sender exists in the audited source. Live-call
notices use APNs. Stream teardown could start summary processing before Twilio
confirmed the call ended. The change closes that timing gap; it does not prove
which text the owner saw, because its wording was not available.

Screened-call summaries now require a durable terminal call record with matching
contractor identity before the handoff is claimed. Nonterminal work stays pending.
The bounded worker rotates through pending IDs so active calls do not starve ended
calls. After 24 hours, unresolved records can be quarantined by a transaction
that rechecks readiness and refuses claimed/completed work. Existing at-most-once
claims and uncertain-delivery handling remain intact. The media stream already
persists transcripts before enqueue; terminal callbacks may arrive before or
after that persistence. Terminal status write failure now returns HTTP 500.

Voicemail recaps continue from their signed final transcription callback. That
expired-account route does not create the screening call record.

## SMS consent and delivery

The server stores an independent app preference and provider opt-out:

| Field | Writer | Legacy default |
| --- | --- | --- |
| `owner_sms_enabled` | Authenticated account PATCH | true |
| `owner_sms_opted_out` | Signed owner consent / provider 21610 | false |
| `owner_sms_opt_out_revision` | Consent transaction | 0 |
| `owner_sms_opt_out_source`, `owner_sms_opt_out_updated_at` | Consent transaction | absent |

Present malformed boolean values fail closed. Provider fields are protected from
client PATCH. SMS preference saves require persistence acknowledgement and a
matching fresh account readback; GET/PATCH expose explicit version 1 settings.

Every automatic owner SMS uses `send_owner_sms(contractor_id, body)`: personal
and business summaries, voicemail recaps, estimate outcomes and account number
notices. It reads current preferences and valid owner/sender identities immediately
before sending. There is no global recipient or generic owner-send fallback.
Suppression returns None, separately from provider failure, so other call effects
can finish. Existing caller-facing SMS/MMS gates stay separate.

Owner texts retain the configured Messaging Service and explicit Kevin sender,
and append `Reply STOP to stop texts.` outside generated/translated content.
Both synchronous and asynchronous Twilio 21610 failures persist the provider block.
The per-message status callback uses the existing Twilio signature validator,
including its contractor/revision query. No new secret or provider configuration
is needed by this source change.

The existing signed inbound webhook resolves the tenant by To, matches the
owner's From, and rechecks both identities inside the consent transaction. It
handles provider OptOutType or exact whole-message STOP/START keywords. Unknown
OptOutType does not authorize START fallback. Owner consent does not create an
ordinary inbound-message push or duplicate confirmation text.

Each accepted consent event increments the revision, even if its state is
unchanged. MessageSid events use deterministic hashed deduplication IDs. An old
START replay cannot undo newer STOP, and a delayed 21610 cannot undo newer START.
START clears only the provider block; an app switch set to off stays off.

## App behavior

Kevin tab → How Kevin answers → SMS notifications.

The setting loads from the account, saves with captured authentication, and
refreshes when the app becomes active or the user returns to the tab. It has no
unscoped UserDefaults preference. Old/malformed server responses show unavailable
with a retry, not a guessed setting. Failed saves retain the desired selection
for retry while displaying the last confirmed value. Overlapping GET/save and
account changes are fenced. Provider-blocked state explains START and shows the
Kevin number; turning on the app switch cannot clear STOP.

## Verification performed locally

- 201 focused backend regression tests passed across SMS consent/delivery,
  post-call lifecycle/operations, estimates, number release, caller messaging,
  call status and inbound messages, with external network/Firestore access denied.
- A final 48-test owner SMS run passed after adding voicemail suppression coverage
  and restoring all mutation probes. This overlaps the regression suite.
- All 350 Kevin iOS unit tests passed on Kevin-Native-Review (iOS 26.0) through
  `xcodebuild-external`, including 21 SMS preference tests. XcodeGen regenerated
  the checked-in project.
- Five deliberately removed guards were each detected by failing tests: app
  opt-out, provider opt-out, fresh phone identity, stale provider failure revision,
  and terminal-call readiness. Original bytes were restored after each probe.
- Fatal Ruff checks and `git diff --check` passed.
- Fresh-context independent actual-diff review returned no findings. The master
  separately audited code, test isolation and results; builder claims were not
  treated as verification.

Read-only inventory nodes returned 2/2 at the same base and were reduced in code.
One headless Gemini 3.7 Flash High builder ran in two passes; the first was stopped
for defects/test isolation. After both exited, the master repaired audit findings
as the sole writer. Final independent review returned 1/1, with no findings.

## Publication and release boundary

Before publication: exact candidate HEAD/dirty state, repository/owner and triggers
must be rebound. Public `delimatsuo/heykevin` uses nine standard Ubuntu PR jobs
(seven Python shards, quality and fail-closed required `Test`), with bounded
job timeouts and PR cancellation. Expected incremental paid Actions cost: $0.
The account's September paid Actions usage readback was $79.209079006; billing
can lag. No paid allocation, workflow changes or new artifact storage is included.
Exact-HEAD CI and configured Codex review are still required before merge.

Source merge does not deploy. No new staging/production deployment, Twilio
configuration, App Store/TestFlight upload or real message/call was performed for
this slice. Public build 42 and TestFlight build 43 do not contain this setting.
The separate notification deployment run 36458376853 remains at the owner's
production approval gate. Only Deli may resolve it under the latest instructions;
do not queue a competing deployment.

Release acceptance requires the backend and a new iOS build, then an owner-run
completed call, app-off test, STOP test and START test (with app-off independence).
Source/unit/CI evidence alone cannot establish SMS delivery on a physical phone.
