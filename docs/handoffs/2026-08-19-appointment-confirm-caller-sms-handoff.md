# Appointment Confirm + Caller SMS Handoff

Created: 2026-08-19 12:05 EDT
Prepared by: Cursor Grok 4.6

## Objective

Owner decides whether a requested appointment is booked (tap Confirm in the
app). Kevin must not write Google Calendar without that click. After a
successful calendar write, SMS the **caller** an informational confirmation
(time + how to change). Do not put a tap-to-book link in the owner request SMS.

## Current State

- Repo: `https://github.com/delimatsuo/heykevin`
- Primary checkout (stale, do not implement here):
  `/Volumes/Extreme Pro/MYPROJECTS/Kevin` on `main` at `d3cd5c7`,
  **23 commits behind** `origin/main`. Untracked older handoff files only.
- **Work here:**
  `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/appointment-confirmed-sms`
- Branch: `feat/appointment-confirmed-caller-sms` (tracks `origin/main`)
- Latest commit on that worktree: `b44b7c3`
  `Merge pull request #180 from delimatsuo/fix/appointment-sms-local-time`
- Dirty state: **8 modified, 1 untracked** (this feature, **not committed**)
- `origin/main`: `b44b7c3` (includes #165 memory flags-off, #177/#179 A2P SID,
  #178 Confirm API + iOS Confirm source, #180 local-time SMS)

### Related worktrees (do not mix)

| Path | Branch | Role |
| --- | --- | --- |
| `.worktrees/appointment-confirmed-sms` | `feat/appointment-confirmed-caller-sms` | **This work.** Uncommitted Confirm+caller-SMS. |
| `.worktrees/appointment-sms-time` | `fix/appointment-sms-local-time` | Merged as #180. Done. |
| `.worktrees/owner-confirm` | `feat/owner-confirm-appointment` | Merged as #178. Done. |
| `.worktrees/sms-msid` / `sms-a2p` | A2P SID | Merged as #177/#179. Done. |
| `.worktrees/customer-memory` | `codex/customer-memory` | Merged as #165. Flags still default-off. |
| Primary `Kevin/` | `main` `d3cd5c7` | Stale. Do not edit. |

## Newest User Request

1. Remain with **user decides if it books**. Do not create a calendar event
   without the owner's click.
2. **Once scheduled**, send SMS with confirmation and details (to the caller).
3. Implement that. Session implemented it in the worktree.
4. Then `/handoff`. On 2026-08-19 13:04 EDT Deli said **commit** and that
   **SMS compliance is approved**.

Earlier in the same conversation: Deli's screenshot had **no Confirm button**.
That was a **test call to his own phone**. Do not tell him to call Jonathan or
book that slot.

## Completed Work

### Product decisions (panel, 2026-08-19)

- App Store **1.2.6** (released 2026-08-13) has **no Confirm UI**. Confirm
  merged in PR **#178** on 2026-08-18 (`91f197a` / `c38af78`).
- Owner SMS stays notify-only: `APPOINTMENT REQUEST … (not confirmed)` +
  `tel:`. No book URL. No Reply YES. Competitors SMS the **customer after
  booking**, not the owner to write Google Calendar.
- Calendar write = authenticated in-app Confirm only.
- After Confirm succeeds, SMS the **caller** (not a second book link).

### Implementation (uncommitted on `feat/appointment-confirmed-caller-sms`)

- `app/services/appointment_confirm.py`: after `book_appointment` returns
  `event_id`, optionally SMS caller; `caller_notified` in API payload;
  `caller_notified_at` on `appointment_request`; retry SMS on already-confirmed
  if never sent; skip when caller == `owner_phone`; skip unless
  `sms_compliance_status == "approved"`; 422 if slot fails `slot_is_plausible`
  (rejects year 2020 vs now).
- `app/services/gated_actions.py`:
  `ActionKey.APPOINTMENT_CONFIRMED_CALLER_SMS`
  (`requires_flag=False`, `requires_sms_compliance=True`,
  `requires_owner_confirmation=True`).
- `app/services/appointment_time.py`: `format_wall_clock`, `slot_is_plausible`.
- `app/services/post_call.py`: owner SMS time formatting uses `format_wall_clock`.
- `ios/Kevin/Views/CallHistoryView.swift`: Details card shows requested time
  above Confirm.
- `ios/project.yml`: `MARKETING_VERSION` **1.2.7**, `CURRENT_PROJECT_VERSION` **32**.
- Tests: `tests/unit/test_owner_confirm_appointment.py` expanded;
  `tests/unit/test_appointment_time.py` added.

## In Progress

- Feature implemented; commit/PR is the next shipping step.
- Production deploy of **#180** (local wall-clock) **not live**.
- iOS Confirm **not in App Store** (still 1.2.6 / build 31 era).
- Slice 4 hang-up caller SMS: still not built (unrelated).

## Important Decisions

- Auto-book (`GOOGLE_CREATE_EVENT`) stays **off**.
- Do not enable `customer_memory_capture_enabled`,
  `customer_memory_personalization_enabled`, or
  `service_request_mutations_enabled`.
- SMS confirm **link that writes calendar** is permanently out of scope
  (possession = write). Optional later: deep link that **opens** Confirm UI.
- Caller confirmation SMS is **not** a new feature flag; it uses SMS
  compliance + owner tap. Self-tests to `owner_phone` do not get the caller
  text.
- Test call `CA7cc46173f6d522683f95fc295515cdd2` (Jonathan / Electus, Friday
  12pm spoken): prior session recorded Firestore
  `start_time` `2020-08-21T12:00:00Z`, `pending_owner_confirmation`, no
  `event_id`. **Do not confirm that SID.** New confirm path 422s absurd years.

## Files And Artifacts

Worktree dirty files:

- `app/services/appointment_confirm.py`
- `app/services/appointment_time.py`
- `app/services/gated_actions.py`
- `app/services/post_call.py`
- `app/services/side_effect_inventory.py`
- `ios/Kevin/Views/CallHistoryView.swift`
- `ios/project.yml`
- `tests/unit/test_owner_confirm_appointment.py`
- `tests/unit/test_appointment_time.py` (untracked)

## Commands Run And Results

```bash
curl -sS https://kevin-api-752910912062.us-central1.run.app/health
```

Result (2026-08-19 12:05 EDT): revision `kevin-api-00248-xnx`,
`deploy_sha=a996c0042315a43184da6b754004774c33499954` (**PR #179**, includes
#178 Confirm API, **does not include #180** `b44b7c3`).

```bash
curl -s https://itunes.apple.com/lookup?bundleId=com.kevin.callscreen
```

Result: version **1.2.6**, `currentVersionReleaseDate` **2026-08-13**.

```bash
cd "/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/appointment-confirmed-sms" && \
/Volumes/Extreme\ Pro/MYPROJECTS/Kevin/.venv/bin/python -m pytest \
  tests/unit/test_owner_confirm_appointment.py \
  tests/unit/test_appointment_time.py \
  tests/unit/test_appointment_requests.py \
  tests/unit/test_gated_actions.py \
  tests/unit/test_phase0_side_effect_inventory.py -q
```

Result: **48 passed** (re-run after inventory evidence string).

Full `tests/unit`: **2518 passed**, **2 failed**
`tests/unit/test_release_process.py` — `bash: python: command not found` in
fake PATH. Unrelated to this feature.

## Verification

- Passed: targeted pytest 48; Confirm+SMS unit matrix (send after book, skip
  self, skip without compliance, no SMS on calendar 502, no double SMS,
  retry if never sent, 422 on 2020).
- Failed: two release-process PATH tests (environment).
- Not run: iOS archive / TestFlight; production deploy; live Confirm on a
  phone; live caller SMS; Firestore re-read of `CA7cc…`.

## Risks And Watchouts

- **P0** Coaching Confirm on App Store 1.2.6: the button is not in that binary.
- **P0** Confirming `CA7cc…` or any 2020/`Z` slot before #180 is on prod and
  year guard is deployed.
- Caller SMS needs `sms_compliance_status=approved`. Deli confirmed 2026-08-19
  that SMS compliance is approved; self-tests to `owner_phone` still skip.
- **P1** Production SMS times still wrong until `deploy_sha` includes `b44b7c3`.
- Primary checkout is **behind**; editing it will miss #178/#180.

## Do Not Do

- Do not implement in `/Volumes/Extreme Pro/MYPROJECTS/Kevin` (stale `main`).
- Do not `git add -A`, force-push `main`, or `--no-verify`.
- Do not enable memory or service-request mutation flags.
- Do not add SMS book links or Reply-YES calendar writes.
- Do not self-approve GitHub `production` env.
- Do not tell Deli to book the Jonathan self-test or tap Confirm on 1.2.6.
- Do not confirm `CA7cc46173f6d522683f95fc295515cdd2`.

## Next Recommended Steps

1. Push `feat/appointment-confirmed-caller-sms` and open the PR (this session).
2. Staging deploy of `origin/main` + this PR after merge; smoke; then
   production so `deploy_sha` includes **#180** and this confirm-SMS.
3. Archive iOS **1.2.7 (32)** TestFlight. Confirm is not in App Store 1.2.6.
4. Use a **new** test call (not `CA7cc…`) after deploy: owner Confirm →
   Calendar event at spoken local time → caller SMS (or skip if same as
   `owner_phone`).

## Open Questions

- None for SMS compliance: Deli confirmed 2026-08-19 that SMS compliance is
  **approved**. Caller confirmation SMS will send after Confirm (except when
  caller == `owner_phone`).
- Production deploy of this PR + #180, then TestFlight 1.2.7 (32), still
  pending (hard gate: do not self-approve GitHub `production`).
