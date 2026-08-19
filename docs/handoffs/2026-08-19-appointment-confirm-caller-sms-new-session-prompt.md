You are continuing work on Hey Kevin: owner-tap appointment Confirm, then informational SMS to the caller after Google Calendar write. No calendar event without the owner's click. No SMS book link.

Workspace:
- Worktree (implement here only): `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/appointment-confirmed-sms`
- Branch: `feat/appointment-confirmed-caller-sms` (based on `origin/main` `b44b7c3`)
- Do **not** edit `/Volumes/Extreme Pro/MYPROJECTS/Kevin` primary `main` (`d3cd5c7`, 23 commits behind).
- Read first:
  - `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/appointment-confirmed-sms/docs/handoffs/2026-08-19-appointment-confirm-caller-sms-handoff.md`

Newest user request:
Commit. SMS compliance is approved. Implementation was in the worktree; commit/PR is the next shipping step.

Current state:
- Confirm path SMS + year/plausible-slot 422 + iOS slot card + version 1.2.7 / build 32.
- `APPOINTMENT_CONFIRMED_CALLER_SMS`: compliance + owner tap, no feature flag. Skip if caller == `owner_phone`.
- Deli confirmed SMS compliance is **approved** (2026-08-19). Caller SMS will send after Confirm except on self-tests to `owner_phone`.
- App Store Hey Kevin is still **1.2.6** (2026-08-13). Confirm UI is **not** in that binary.
- Production health `deploy_sha=a996c00` (revision `kevin-api-00248-xnx`): Confirm API yes, **#180 local-time SMS not live**.
- Do not confirm call `CA7cc46173f6d522683f95fc295515cdd2` (stored year 2020 / Z).

Critical constraints:
- Auto-book / `GOOGLE_CREATE_EVENT` off.
- Do not enable `customer_memory_*` or `service_request_mutations_enabled`.
- Do not add unauthenticated SMS confirm URLs or Reply-YES that write Calendar.
- Never self-approve GitHub `production`. No `git add -A`, no force-push `main`.
- Worktrees only. Do not mix `.worktrees/owner-confirm` or `appointment-sms-time` (already merged).

Facts and evidence:
- Targeted pytest 48 passed:
  `cd "/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/appointment-confirmed-sms" && /Volumes/Extreme\ Pro/MYPROJECTS/Kevin/.venv/bin/python -m pytest tests/unit/test_owner_confirm_appointment.py tests/unit/test_appointment_time.py tests/unit/test_appointment_requests.py tests/unit/test_gated_actions.py tests/unit/test_phase0_side_effect_inventory.py -q`
- Full unit: 2518 passed; 2 `test_release_process.py` fails are `python: command not found` in fake PATH, unrelated.

Next recommended action:
1. `git status --short --branch` in the worktree.
2. If Deli wants it shipped: commit only the feature files (plus this handoff pair if including docs), push, `gh pr create`.
3. Do not cut TestFlight until Confirm iOS is in the binary (1.2.7/32) and prefer prod `deploy_sha` including #180 + this PR.

Verification expected:
- Same targeted pytest command above, all pass.
- After PR merge + deploy: prod `/health` `deploy_sha` is not still `a996c00`.
- After TestFlight: Recents → Details shows requested time + Confirm (not App Store 1.2.6).

Known risks:
- Telling the owner to tap Confirm on the installed App Store app.
- Self-tests to `owner_phone` skip caller SMS even though SMS compliance is approved.

If anything conflicts, the newest user request wins. Start by running:

```bash
cd "/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/appointment-confirmed-sms"
git status --short --branch
```
