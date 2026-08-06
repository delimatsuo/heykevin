You are continuing work on Hey Kevin's Jobber customer-memory feature.

Current objective:
Validate PR #76 in staging with a live call, then decide whether to patch further, request another review, or mark the PR ready. The feature lets Kevin read known Jobber customer context before the first Gemini Live response so returning callers are handled more like real receptionist interactions.

Workspace:
- Repo root: `/Volumes/Extreme Pro/myprojects/Kevin`
- Feature worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory`
- Branch: `codex/jobber-customer-memory`
- Important docs to read first:
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/AGENTS.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/handoffs/2026-07-08-jobber-customer-memory-handoff.md`

Newest user request:
The latest request was `$handoff`. Before that, the user asked what to do next after staging deploy. Newest request wins; continue from the handoff.

Current state:
- PR #76: `https://github.com/delimatsuo/heykevin/pull/76`
- PR branch: `codex/jobber-customer-memory`
- Latest commit: `d185edd Guard Jobber memory matching`
- PR is draft, base `main`, merge state `CLEAN`.
- Staging deploy is live from the PR branch:
  - Service: `kevin-api-staging`
  - Revision: `kevin-api-staging-00058-muc`
  - URL: `https://kevin-api-staging-l63rergg7a-uc.a.run.app`
  - Deploy SHA: `d185eddb9283fbd063e90030f5ef4992724153ad`
- Health check returned:
  - `{"status":"ok","environment":"staging","service":"kevin-api-staging","revision":"kevin-api-staging-00058-muc","deploy_sha":"d185eddb9283fbd063e90030f5ef4992724153ad"}`
- Main checkout `/Volumes/Extreme Pro/myprojects/Kevin` is stale and dirty. Do not work there for this feature.
- These handoff files are uncommitted unless a prior agent committed them after writing this prompt.

Critical constraints:
- Do not revert user/other-agent changes.
- Do not work in the dirty main checkout.
- Do not deploy production unless the user explicitly asks.
- Do not force-update or casually merge `staging`; `origin/staging` is divergent and merging PR #76 into it produced broad conflicts.
- Do not expose secrets, Jobber OAuth callback codes, full phone numbers, or admin bearer tokens.
- Jobber client secret rotation is intentionally deferred.
- Calls only prove staging behavior if the Twilio webhook/number routes to `https://kevin-api-staging-l63rergg7a-uc.a.run.app`.

Facts and evidence:
- Local verification passed:
  - `uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_jobber.py tests/unit/test_receptionist_intelligence.py -q` -> `55 passed, 2 warnings`
  - `uv run --python 3.12 --with '.[dev]' ruff check app/services/jobber.py app/services/gemini_pipeline.py tests/unit/test_jobber.py tests/unit/test_receptionist_intelligence.py` -> `All checks passed!`
  - `uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit -q` -> `300 passed, 16 warnings`
- GitHub Actions test for PR #76 passed:
  - Run URL: `https://github.com/delimatsuo/heykevin/actions/runs/28975454509`
- Staging deploy completed directly from the feature worktree:
  - `gcloud run deploy kevin-api-staging --source . --project kevin-491315 --region us-central1 ...`
  - Result: revision `kevin-api-staging-00058-muc`, 100% traffic.

Next recommended action:
1. Verify which Twilio number/webhook can hit staging. Do not assume the user's normal number hits staging.
2. Run one live inbound call from the seeded Jobber caller ending in `8667`.
3. Read staging/admin logs and verify behavior:
   - Kevin recognizes the known caller naturally.
   - Kevin does not mention Jobber/private notes.
   - Kevin does not ask for known name/address unnecessarily.
   - Kevin uses prior sink-repair context as background only.
   - First response latency is acceptable.
4. If the staging call passes, ask whether to mark PR #76 ready or request another staff review.
5. If the staging call fails, patch PR #76 in `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory`.

Verification expected:
- Start with:
  ```bash
  cd "/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory"
  git status --short --branch
  curl -fsS https://kevin-api-staging-l63rergg7a-uc.a.run.app/health
  ```
- After any code changes, run:
  ```bash
  uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_jobber.py tests/unit/test_receptionist_intelligence.py -q
  uv run --python 3.12 --with '.[dev]' ruff check app/services/jobber.py app/services/gemini_pipeline.py tests/unit/test_jobber.py tests/unit/test_receptionist_intelligence.py
  uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit -q
  ```

Known risks:
- The in-app browser still shows a local Jobber OAuth callback URL with an authorization code. Treat that as sensitive.
- Current local gcloud account may not be the deploy account. Before deploys, check:
  ```bash
  gcloud config list --format='value(core.account,core.project,billing.quota_project)'
  ```
- The successful deploy required `deli@ellaexecutivesearch.com` and quota project `kevin-491315`.

If anything conflicts, the newest user request wins. Start by running:

```bash
git status --short --branch
```
