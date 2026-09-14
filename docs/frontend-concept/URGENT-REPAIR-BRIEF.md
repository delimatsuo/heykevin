# Bounded repair after independent urgent-preview review

Work only in /Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/frontend-concept.
Only edit docs/frontend-concept/index.html. Current SHA256
5dde69d05d3e6b418a83d9d4e1a409a996ddacf151b74c746ce91af7415889a9.
Headless agy gemini-3.7-flash-high. No agents, no Git mutations, no external
access, no credentials/.env, no tests, no other files. Parent verifies.

Preserve the new urgent features and all existing guards. Fix these exact issues:

1. The suppressed notification View in app button is dead: bindAppScreenEvents
   binds before updateNotificationBanner replaces that DOM; bindNotificationEvents
   does not handle view-live. Bind its navigation in the notification binder
   after DOM replacement, with the passed notification target ID; ended targets
   open their summary without changing the active call.
2. Expand/collapse/dismiss/restore replaces focused controls with innerHTML.
   Remember which desktop/mobile preview contained focus and its action, restore
   focus to that preview's equivalent control or appropriate successor only
   when that interaction initiated the update. Asynchronous updates must not
   steal focus from elsewhere. Expanding should preserve the expand/collapse
   button so the next Tab reaches actions. Dismiss should focus Restore.
3. Install one guarded 30-second owner-wait timeout whenever a record enters
   waiting, including initial bootstrap, normal waiting selection, urgent
   selection, screening replay/New call completion. Prefer one helper that
   captures generation, call ID, record identity and phase, avoids duplicate
   timers and never extends the same wait. Clear/reset invalidates. Pending
   actions and ended/on-call phases cannot be changed by its callback. Manual
   Simulate 30 seconds stays usable. Use the smallest coherent implementation.
4. Suppressed/dismissed card text must reflect the target phase: waiting,
   requesting, taking message, connected, ended, no call. Never claim screening
   continues or the call is live after it ended. Dismiss stays presentation only.
5. Replace the fabricated urgent summary 'Advised cutting street main supply'
   with 'Kevin is trying Alex; no response confirmed yet.' Replace the urgent
   transcript line 'I am alerting Alex immediately with an urgent priority alert.
   Please stand by.' with natural copy 'I will try Alex now. Please stay somewhere
   safe while I check.' No invented repair instructions or response promises.

Return JSON {changed_files, fixes, known_limits}. Do not reformat unrelated code.
