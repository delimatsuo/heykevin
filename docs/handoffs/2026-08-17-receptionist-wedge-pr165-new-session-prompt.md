You are continuing Hey Kevin receptionist-wedge work: rebase draft PR #165 onto current main.

Current objective:
Rebase `codex/customer-memory` onto `origin/main` (live intake from PR #174 is already there), keep every memory flag default-off, update the PR with `--force-with-lease` to that feature branch only. Do not merge, deploy, or enable flags.

Workspace:
- Rebase here: `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory`
- Branch: `codex/customer-memory` at `4f99e8bbcdae77081786798990e1f6d922158c68` (matches origin; rebase has NOT started)
- Do not work in `/Volumes/Extreme Pro/MYPROJECTS/Kevin` (local `main` is 5 commits behind at `d3cd5c7`)
- Do not touch `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/live-intake-controller`
- Read first:
  - `docs/handoffs/2026-08-17-receptionist-wedge-pr165-handoff.md` (this session's durable state; on the primary checkout, copy or read from `/Volumes/Extreme Pro/MYPROJECTS/Kevin/docs/handoffs/`)
  - `.worktrees/customer-memory/docs/handoffs/2026-08-12-customer-memory-pr165-handoff.md` (activation P0/P1 still valid; SHA/CI evidence is stale)
  - `.worktrees/customer-memory/docs/customer-memory-rollout.md`
  - `docs/superpowers/plans/2026-08-17-receptionist-wedge.md` on `origin/main`

Newest user request:
`/handoff` interrupted an authorized `go` to rebase PR #165. Resume that rebase. Newest request before the handoff was: rebase #165 onto main, flags stay off.

Current state:
- Slice 1 (live intake) is merged and in production: PR #174, `9bc275c`, revision `kevin-api-00245-jjs`. Owner live-tested a real business Kevin number: name + service question, Friday 2pm offered, confirm-with-Deli. Pass. Do not reopen intake unless a call skips the job and jumps to the form.
- Public demo `kevin-public-demo-00031-tgd` is still `d3cd5c7`. Do not deploy or iterate it.
- PR #165 is OPEN DRAFT at `4f99e8b`. `merge-tree` vs `origin/main` shows `changed in both` for exactly three files: `app/config.py`, `app/db/contractors.py`, `app/services/gemini_pipeline.py`.
- Customer-memory worktree dirty state: untracked 2026-08-12 handoff md files only. Do not delete them.

Critical constraints:
- Do not revert user/other-agent changes.
- Do not work in the wrong worktree.
- Do not enable `customer_memory_capture_enabled`, `customer_memory_personalization_enabled`, `service_request_mutations_enabled`, or `SERVICE_REQUEST_RECOVERY_ENABLED`.
- Do not merge PR #165 or deploy `kevin-api` unless the owner explicitly asks after green tests.
- Do not force-push `main` or `staging`. Feature-branch update after rebase: `git push --force-with-lease origin codex/customer-memory`.
- Do not drop live intake from `gemini_pipeline.py`. Keep `_live_intake`, hold-speech opening instructions, fail-closed send, and `_credit_kevin_turns` (greeting must not mark `service_action` asked). Also keep PR #165 `from app.services.receptionist_context import build_greeting_text`.
- Do not `git reset --hard` or `git clean` in the primary checkout or this worktree.
- Do not reintroduce global contact fallback.

Facts and evidence:
- `origin/main` = `9bc275c01071e5cf255237a533a6a40130558c09`
- Production/staging health `deploy_sha` = that SHA (verified 2026-08-17 15:38 EDT)
- PR #165 CI at `4f99e8b` is void after rebase; rerun tests on the new tree
- Keep tests: `tests/unit/test_live_intake_controller.py` and the Gemini greeting/schedule/public-demo wiring tests in `test_receptionist_intelligence.py`

Next recommended action:
1. `cd /Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory && git fetch origin && git rebase origin/main`
2. Resolve the three conflicts: intake from main + greeting/context and PROTECTED flags from #165
3. Run focused pytest (intake + memory + isolation) then full `pytest -q`
4. Force-with-lease to `codex/customer-memory` only. Leave the PR draft.

Verification expected:
```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 \
  /Volumes/Extreme\ Pro/MYPROJECTS/Kevin/.venv/bin/python -m pytest tests/unit/test_live_intake_controller.py tests/unit/test_receptionist_intelligence.py tests/unit/test_customer_memory.py tests/unit/test_receptionist_customer_context.py tests/unit/test_post_call_customer_memory.py tests/unit/test_contact_tenant_voice_isolation.py -q
```
Then full `pytest -q`. Intake tests must still pass after the rebase.

Known risks:
Dropping `_live_intake` during the `gemini_pipeline.py` conflict would unship production behavior. Accidental flag-true would activate unready ANI-auth mutations (P0).

If anything conflicts, the newest user request wins. Start by running:

```bash
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory" status --short --branch
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory" log -1 --oneline
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin" fetch origin main
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin" log -1 --oneline origin/main
```
