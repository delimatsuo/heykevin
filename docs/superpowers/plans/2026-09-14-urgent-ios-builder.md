# iPhone builder envelope

Read adjacent 2026-09-14-urgent-call-handoff.md for the pinned HTTP/push contract.
Work only /Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/urgent-call-ios at
HEAD66f8a446e0e873ecb280c39aaab60f7cf0448a36 tree
a96ecde675099cac7ef4b1c2c3e60c2aab00c4cb. Parent owns all Git and verification.
Use headless agy gemini-3.7-flash-high for the pinned stateful implementation.

Allowed edits: ios/Kevin/App/AppDelegate.swift, ios/Kevin/App/KevinApp.swift
(only unit-test isolation described below), ios/Kevin/Services/APIClient.swift,
ios/Kevin/Services/CallManager.swift, ios/Kevin/Services/CallActionCoordinator.swift
(new), ios/Kevin/Models/AppState.swift, ios/Kevin/Views/ContentView.swift,
ios/Kevin/Views/CallHistoryView.swift, ios/Kevin/Views/SettingsView.swift,
ios/KevinTests/CallActionTests.swift (new), ios/project.yml (only unit-test
environment variable; no version, signing or capability changes).
No other edits. No Git operations, reports, test/build execution, agents,
providers/network, credentials, .env reads, installs, browser or OS integrations.
Parent runs tests/build. Source reads and syntax checks are okay. No rm -rf.

Implement scoped behavior, not a frontend redesign:

1. Register PICK_UP_ACTION and TAKE_MESSAGE_ACTION in SCREENING_CALL, both
   foreground. A default body tap validates the payload's exact call on GET
   /api/call-action/{sid}; it does not answer. A dismiss action returns without
   opening a screen or calling any API. Unknown actions/categories do not
   manufacture a live call. A stale/ended A alert opens A's summary from Recents
   if available (otherwise an honest unavailable state), preserving active B.
   Do not call setActiveCall before validation. Legacy push payloads may omit
   contractor_id; the authenticated exact-call GET still enforces ownership.
2. Create a testable MainActor CallActionCoordinator for notification, live view,
   and urgent CallKit actions. Snapshot account ID, API token, session generation,
   call SID and one UUID operation ID before any await. Generation must increment
   on logout, contractor change and credential changes, including A->B->A.
   Requests use captured credentials, never dynamic auth after awaiting.
   Responses must match identities and known status. New endpoint malformed/
   wrong-call/wrong-operation responses fail closed. Old backend successful
   accept shape {status:ok,access_token,conference_name} may be used only as
   compatibility for its same captured request and live session; never from GET.
3. Deduplicate concurrent same-call accepts across entry points. A pending
   accept disables both actions; decline cannot run alongside it. Maintain an
   operation through uncertain results and reconcile with read-only GET rather
   than automatically repeating POST. Limit polling, surface connection/request
   failure honestly, and offer an explicit check/retry control where appropriate.
   Never show success merely because queueing succeeded: decline transitions
   Requesting a message -> Taking a message only at action_status=taking_message.
   Tests inject request/status closures to exercise suspended completions and
   races without AppState, Keychain, CallKit or providers.
4. Update live Pick up and renamed Take a message controls to use that coordinator,
   display pending/error, preserve transcript and caller on failure. Keep Text
   reply flag off. Add a simple Urgent label when server status says urgent.
   Don't clear a newer call because an older poll completes. Preserve the same
   call's message-taking state on duplicate setActiveCall. All async live polls
   and foreground active-call updates check captured generation/account/SID.
5. For reason urgent_call VoIP: capture call SID, payload contractor, deadline
   and account/session in a CallKit UUID-bound context. Mandatory immediate
   reporting still occurs; stale/wrong-account payloads report then promptly end
   and never send an action. Duplicate pushes cannot replace an existing call
   context. Expire the matching unanswered ring at expires_at, max30seconds;
   legacy urgency without expiry uses arrival+30. On answer call authenticated
   accept and connect with returned token/conference under the SAME incoming
   CallKit UUID. Do not use connectDirectly (it starts a second outgoing call).
   End/decline uses this UUID's captured call ID, never AppState.activeCallSid.
   A late result or Twilio callback cannot connect or clear a different call.
   Keep preissued-token known-contact and expired ring answer working without
   calling accept again. Include callSid and contractorId fields in reporting
   with backward-compatible defaults if other callers need them.
6. Register urgent_handoff_v1=true with device registration. Use time-sensitive
   payloads from server without requesting Critical Alert permission/entitlement
   or promising mute/Focus bypass. Keep ordinary notification permissions.
7. Make urgent alert preference server-confirmed: load smart_interruption from
   profile, default true. Show pending save, prevent overlapping writes, commit
   local effective setting only after successful captured-account response;
   failure keeps previous value and displays an error. Profile refresh must not
   programmatically trigger another PATCH. Preserve unrelated settings behavior.
8. Test-host isolation: add KEVIN_UNIT_TESTS=1 only under each XcodeGen scheme's
   test environmentVariables. AppDelegate skips registration when that env is1;
   KevinApp shows an inert view and skips scene tasks in that mode. Production
   launch is unchanged. No tests launch network, contact sync or StoreKit.

Tests must causally exercise fake suspended responses: duplicate entries send
one POST; opposite action while pending blocked; same account with changed
generation invalidates; old A response while B active does not connect B;
wrong returned SID/opID/account/malformed data rejected; message pending/failure
does not claim taking_message; read-only reconciliation no second POST; expired
urgent context cannot answer; known-contact direct branch preserved. Prefer
testing extracted pure routing/session/CallKit ownership helpers actually used
by production code. Parent mutation-checks guards. Return JSON
{node,changed_files,implementation_notes,tests_added,known_limits}.
