# Urgent call handoff and notification actions

Owner approved implementation September 14, 2026. This completes necessary PRD
N1 notification behavior. Native frontend redesign, additional live actions,
SMS, caller callbacks, flags, deployments and App Store delivery are outside it.
Base: 66f8a446e0e873ecb280c39aaab60f7cf0448a36, tree
a96ecde675099cac7ef4b1c2c3e60c2aab00c4cb. Parent owns all Git.

## Acceptance criteria

- "Pick up connects the authenticated owner to the exact live screened call."
- "Take a message keeps the caller connected and shows pending until Kevin accepts the instruction."
- "Dismiss only changes presentation."
- "Urgency starts one bounded 30-second owner wait; pickup, message taking and timeout cannot issue conflicting commands."
- "Turning urgent alerts off persists on the server and suppresses urgent pushes and rings without stopping screening."
- "A duplicate or delayed action, ended call, different call or changed account cannot initiate an unintended connection."
- "Urgent alerts use supported notification behavior and make no Critical Alert or Do Not Disturb bypass promise."
- "Older app builds receive a usable urgent notification; urgent CallKit ringing is limited to clients advertising the new handoff capability."

Graph: parent pins contract -> backend builder + iOS builder + HTML builder in
separate worktrees -> parent reduces files/tests in code -> fresh independent
review -> parent verifies and integrates. Builders use headless
agy gemini-3.7-flash-high for stateful implementation. Staff/review uses Codex
gpt-6-astra high; tests and actual artifacts are the evidence anchors.

## Shared HTTP and push contract

POST /api/call-action keeps call_sid, action (accept/decline) and adds optional
operation_id, at most 80 characters. Legacy missing IDs use a stable per-action
legacy ID. New clients retain one UUID per intentional action across uncertain
outcomes; automatic POST retries are forbidden. All identity is derived from
authenticated contractor_id, never from an untrusted payload alone.

Action responses always identify call_sid, contractor_id, operation_id, action,
action_status and explicit active=true. HTTP 200 status=ok means accepted or taking_message. HTTP 202
status=pending means accepting, message_requested or uncertain. Only accepted
returns conference_name/access_token. Wrong owner is 403; absent or ended call
is 404/409; opposite or other-operation ownership conflict is 409. Errors never
look like a successful action. Legacy non-screening direct-call decline must
continue to return the caller to Kevin.

GET /api/call-action/{call_sid}?contractor_id=...&operation_id=... is read-only
reconciliation and exact-call navigation. It returns the same identity fields
plus active (Boolean), urgent (Boolean), caller_name, caller_phone and transcript
for that contractor's live call. No live call returns active=false and
action_status=ended without returning another call. No action returns ready.
Only the matching accepted operation can return connection credentials. An
unmatched existing operation returns action_conflict, never its token.
Pending GET reconciliation uses HTTP 202/status=pending; accepted or
taking_message uses HTTP 200/status=ok. Navigation without an operation never
returns credentials. Strict new-client parsing requires all action identities,
active=true and the matching operation/action before any connection.

A definitely failed acceptance preparation can return HTTP 503 with status=error,
action_status=preparation_failed, retryable=true, active=true and the exact
call_sid/contractor_id/operation_id/action=accept, with no credentials. Emit this
only after a transaction confirms release of this request's claim before any
provider redirect. Only that structured response permits a later deliberate
fresh operation. Generic or lost errors stay unresolved; conflicts never become
silent enabled retry controls. JSON lifecycle/retryability values must be actual
Booleans, not numbers, strings or missing values.

Pending acceptance reconciliation may query the already registered conference
and its exact connected caller. It cannot redirect again. Recheck owner, call,
claim and lifecycle after that read before finalization or credentials. An absent
participant or failed lookup is not proof the earlier transfer failed.

Device registration adds urgent_handoff_v1:Boolean, default false for legacy.
Urgent VoIP is sent only for registered support. Its payload includes reason=
urgent_call, call_sid, contractor_id, expires_at (Unix seconds, 30-second bound).
It carries no pre-created conference or token: the new client obtains both only
when the user answers. The regular urgent push uses category SCREENING_CALL,
interruption-level time-sensitive, ordinary sound, the same call collapse ID,
and safe generic alert text. No raw caller reason in an urgent lock-screen body.
Known-contact/expired direct VoIP pushes retain their existing token/conference
flow. Both custom actions are registered as foreground actions: PICK_UP_ACTION
and TAKE_MESSAGE_ACTION. Body taps navigate; dismiss does not send an action.

## Durable decision rules

One RTDB transaction at the existing /active_calls/{call_sid} arbitrates the
owner action and fallback. Require current nonterminal fresh data and matching
nonempty owner; never authorize a live mutation from historical Firestore alone.
No process-only lock. Retried transaction callbacks must be pure; determine the
winner from the final committed nonce, not callback-local flags. One operation
owns accept versus decline/timeout. Duplicate accepts reuse one opaque conference
and issue at most one provider redirect. Concurrent duplicates see pending.
Do not automatically replay a redirect after a lost/ambiguous provider response;
retain uncertain state and reconcile read-only when possible. No timeout or
decline can reverse an accepted or unresolved acceptance.

Persist take-message intent before processing; UI acknowledgement means the
pipeline accepted its instruction, not that a message was recorded. Do not delete
a command before successful handling. Generation/account/call checks guard each
client continuation. A late response must not overwrite or connect a newer call.
Urgency must start or preserve a bounded wait, never cancel it without replacement.
Preparation crossing the deadline cannot abandon the timeout if its claim is
later released. Direct rings create authenticated live state before alerting;
their fallback uses the same arbitration and never replays an uncertain redirect.
Message acknowledgement belongs to the receiving voice engine, not the redirect.
All three voice engines share these semantics, with failed notification delivery
unable to stop normal call handling. Missing server data blocks side effects but
must not crash the running voice pipeline.

## Verification and delivery

Test duplicate callbacks/requests, timeout vs pickup, decline vs pickup, wrong or
missing owner, ended/stale call, lost responses, changed account, a late action
while another call is active, preference save failure and all voice engines.
Mutation-check new guards. Build/test iOS locally with signing disabled via the
required Xcode wrapper. HTML remains fictional and network-free. Device/APNs and
production verification remain explicit delivery checks after local completion.
