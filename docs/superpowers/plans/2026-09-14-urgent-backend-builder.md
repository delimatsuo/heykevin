# Backend builder envelope

Read the adjacent 2026-09-14-urgent-call-handoff.md as the pinned contract.
Worktree /Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/urgent-call-handoff.
Base HEAD 66f8a446e0e873ecb280c39aaab60f7cf0448a36 and tree
a96ecde675099cac7ef4b1c2c3e60c2aab00c4cb. Parent owns Git and verification.
Model headless agy gemini-3.7-flash-high, implementation of a pinned state contract.

Write allowlist: app/services/owner_call_actions.py (new),
app/services/urgent_handoff.py (new), app/api/voip.py,
app/api/contractors.py, app/db/contractors.py, app/services/push_notification.py,
app/services/voice_pipeline.py, app/services/gemini_pipeline.py,
app/services/relay_pipeline.py, app/webhooks/media_stream.py,
app/webhooks/relay_stream.py, app/webhooks/twilio_incoming.py (only direct decline
acknowledgement if needed), app/services/state_machine.py (only additive fields),
tests/unit/test_urgent_handoff.py (new), tests/unit/test_owner_call_actions.py
(new), tests/unit/test_urgency.py, tests/unit/test_conference_security.py,
tests/unit/test_phase0_push_payloads.py, tests/unit/test_screening_summary_push.py.
Do not change other files. Do not write reports. Return JSON in final response.

No Git mutations, external/provider calls, credentials, .env access, installs,
browser, other agents, deployment, flags, workflow changes or real data. Do not
execute tests; parent independently runs them. Source reads and syntax checks
are fine. Scratch only configured TMPDIR. Never rm -rf.

Implementation details:
- Put durable arbitration and status in owner_call_actions.py with pure record
  reducers and async RTDB adapters. Expose helpers pipeline handlers can call.
  Keep old helper signatures where practical but route HTTP accept/decline through
  arbitration. Reject empty/missing owner and stale/terminal live records.
- Acceptance uses one opaque conference, a transaction claim nonce and stable
  operation ID. Generate token/binding before Twilio redirect; binding failure
  must be observable (existing register_conference swallows errors, so verify
  the exact binding before redirect). Mark accepted intent before redirect so
  stream teardown cannot hang up the caller, but return token only after a
  confirmed redirect. Definite pre-redirect failure can be retried deliberately;
  an exception at/after redirect keeps uncertain status and cannot issue another
  redirect. GET may confirm a lost redirect via read-only conference participant
  evidence for the exact original call SID, otherwise stays uncertain.
- Take-message and timeout use the same owner decision. A queued request is
  message_requested; successful pipeline instruction becomes taking_message.
  Keep legacy /call_commands for known-contact direct-ring fallback, and do not
  erase a command on failed injection. A repeated command must not speak twice
  after a successful injection. A timeout cannot override a claimed pickup.
- Shared urgency dispatcher checks fresh contractor smart_interruption setting
  (default true), exact live owner/lifecycle and once-per-call escalation before
  any push; false does not affect transcription/safety or normal notification.
  One failed channel must not prevent attempting the other supported channel.
  Get capability from registered device; old clients get regular urgent push
  only. No unauthenticated urgent conference allocation at detection time.
- Persist smart_interruption as a StrictBool optional PATCH field, default true
  in new contractor document. Urgent sender uses supported time-sensitive push
  plus category/collapse/call/account fields. Ordinary screening summary copy
  says Tap to view live, never Tap to answer. Preserve existing safe push copy.
- All pipeline urgency detections start/preserve the 30s hold timer. Repeated
  hold phrases must not extend an urgent deadline. Don't await APNs before the
  caller can continue. Stop tasks on pipeline stop; ensure decline and automatic
  fallback acknowledge the durable decision only after instruction acceptance.

Meaningful tests with injected in-memory RTDB transactions and fake providers:
same request racing twice -> one redirect; opposite actions -> one winner;
transaction retries cannot produce two owners; vanished/ended/other-tenant/
empty-owner records -> no provider command; uncertain result -> no redirect
replay; failed command injection remains pending; successful injection then
repeat -> no repeat speech; urgency preference off/old/new device payload paths;
each engine urgency leaves a bounded timer and pickup suppresses fallback.
Keep source existing privacy tests. Do not weaken tests to accept regressions.
Return {node,changed_files,implementation_notes,tests_added,known_limits}.
