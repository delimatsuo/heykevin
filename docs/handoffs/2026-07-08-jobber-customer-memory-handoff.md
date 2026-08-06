# Jobber Customer Memory Handoff

Created: 2026-07-08 18:11 EDT
Prepared by: Codex

## Objective

Continue the Hey Kevin Jobber customer-memory work. The user wants Kevin to behave like a true AI receptionist by using existing Jobber context for returning callers: recognize the caller, know prior service/property context, avoid unnecessary name/address questions, and eventually support scheduling. The current narrow slice is read-only Jobber memory injection into Gemini Live for live calls.

## Current State

- Repo/workspace: `/Volumes/Extreme Pro/myprojects/Kevin`
- Active worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory`
- Active branch: `codex/jobber-customer-memory`
- Latest commit: `d185edd Guard Jobber memory matching`
- Related PR: `https://github.com/delimatsuo/heykevin/pull/76`
- PR state: draft, base `main`, merge state `CLEAN`
- Worktree dirty state before handoff docs: clean
- Main checkout warning: `/Volumes/Extreme Pro/myprojects/Kevin` is stale and dirty; do not work there for this feature.
- Staging deploy: `kevin-api-staging-00058-muc` serving 100% traffic from commit `d185eddb9283fbd063e90030f5ef4992724153ad`
- Production deploy: not touched

## Newest User Request

The user invoked `$handoff` after staging deploy completed. The previous question, "what do you recommend we do next?", was interrupted and replaced by the handoff request. Newest user request wins.

## Completed Work

- Created isolated worktree:
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory`
  - Branch `codex/jobber-customer-memory`
- Created draft PR #76:
  - `https://github.com/delimatsuo/heykevin/pull/76`
- Implemented read-only Jobber customer memory:
  - `app/services/jobber.py`
  - `lookup_customer_memory(auth, phone)` reads client, phones, properties, notes, jobs, visits, and requests.
  - Formatter creates compact private prompt context and redacts phone-like values.
  - Returned Jobber candidates are now phone-validated before memory injection. No match means no memory.
  - Notes are sorted newest-first so correction notes can beat stale notes.
- Wired Gemini setup to use memory:
  - `app/services/gemini_pipeline.py`
  - Starts the Jobber lookup before websocket setup.
  - Applies a `JOBBER_MEMORY_TIMEOUT_SECONDS = 0.9` latency budget.
  - Skips memory on timeout/error.
  - Greeting prompt can use the known caller first name naturally and says not to mention Jobber/private notes.
- Added tests:
  - `tests/unit/test_jobber.py`
  - `tests/unit/test_receptionist_intelligence.py`
- Seeded Jobber test data in the user's Jobber test account:
  - Client: `Jonathan Caller`
  - Phone ending: `8667`
  - Property: `Jonathan Test Residence`, `100 Market Street, Lynnfield, Massachusetts 01940`
  - Job: `Completed sink repair - Hey Kevin memory test`
  - Notes include a correction note saying structured property should win over an older synthetic San Francisco note.
  - Recent requests include toilet replacement scenarios.
- Deployed PR #76 directly to staging Cloud Run:
  - Revision: `kevin-api-staging-00058-muc`
  - URL: `https://kevin-api-staging-l63rergg7a-uc.a.run.app`
  - `deploy_sha`: `d185eddb9283fbd063e90030f5ef4992724153ad`

## In Progress

- Handoff documents have been created in `docs/handoffs/` and are uncommitted unless a later agent commits them.
- No code implementation is in progress.
- Live-call staging smoke test has not been run yet.

## Important Decisions

- 2026-07-08: Keep PR #76 narrow and read-only.
  - Rationale: avoid mixing customer memory with scheduling/write behavior before proving live-call behavior.
  - Consequence: PR #76 reads Jobber context only; it does not schedule appointments or write Jobber records.
- 2026-07-08: Do not trust first Jobber phone-search result.
  - Rationale: wrong customer memory in a live call would be a serious product/privacy bug.
  - Consequence: `lookup_customer_memory` now validates returned phone numbers and returns no memory on mismatch.
- 2026-07-08: Do not deploy production.
  - Rationale: user needs staging smoke test first.
  - Consequence: production service `kevin-api` was not touched.
- 2026-07-08: Do not use the stale `staging` branch for this deploy.
  - Rationale: `origin/staging` is heavily divergent from current main and merging PR #76 into it produced broad conflicts.
  - Consequence: PR #76 was deployed directly to the staging Cloud Run service from the feature worktree.
- 2026-07-08: Use `deli@ellaexecutivesearch.com` as the admin/deploy account for Kevin GCP.
  - Rationale: the Google Cloud IAM UI showed this account has Owner/Organization Administrator on project `kevin-491315`.
  - Consequence: local deploy succeeded after correcting quota project config.

## Files And Artifacts

- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/app/services/jobber.py`: Jobber API lookup, memory normalization, phone validation, prompt formatting.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/app/services/gemini_pipeline.py`: Gemini Live memory lookup timing and prompt injection.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/tests/unit/test_jobber.py`: Jobber memory and phone-match regression tests.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/tests/unit/test_receptionist_intelligence.py`: Gemini setup/greeting regression tests.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/AGENTS.md`: project guide and deployment rules.
- `/Volumes/Extreme Pro/myprojects/Kevin/secrets/jobber_seed_client_probe.json`: local probe evidence for seeded Jobber test data; contains operational data and should not be committed.
- `/Volumes/Extreme Pro/myprojects/Kevin/secrets/jobber_probe_report.json`: Jobber API probe report; should not be committed.
- `/Volumes/Extreme Pro/myprojects/Kevin/secrets/jobber_schema.json`: Jobber GraphQL schema snapshot; should not be committed.

## Commands Run And Results

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_jobber.py tests/unit/test_receptionist_intelligence.py -q
```

Result: `55 passed, 2 warnings`.

```bash
uv run --python 3.12 --with '.[dev]' ruff check app/services/jobber.py app/services/gemini_pipeline.py tests/unit/test_jobber.py tests/unit/test_receptionist_intelligence.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit -q
```

Result: `300 passed, 16 warnings`.

```bash
gh run view 28975454509 --json conclusion,status,url,headSha,workflowName,displayTitle
```

Result: GitHub Actions run completed with `conclusion: success`, `headSha: d185eddb9283fbd063e90030f5ef4992724153ad`, workflow `Deploy`, display title `Add Jobber customer memory context`.

```bash
gcloud run deploy kevin-api-staging \
  --source . \
  --project kevin-491315 \
  --region us-central1 \
  --allow-unauthenticated \
  --service-account kevin-api-staging-runtime@kevin-491315.iam.gserviceaccount.com \
  --build-service-account projects/kevin-491315/serviceAccounts/kevin-build-staging@kevin-491315.iam.gserviceaccount.com \
  --update-env-vars ENVIRONMENT=staging,APPSTORE_ENVIRONMENT=sandbox,CLOUD_RUN_URL=https://kevin-api-staging-l63rergg7a-uc.a.run.app,DEPLOY_SHA=d185eddb9283fbd063e90030f5ef4992724153ad \
  --tag staging
```

Result: deployed `kevin-api-staging-00058-muc`; routing 100% traffic.

```bash
curl -fsS https://kevin-api-staging-l63rergg7a-uc.a.run.app/health
```

Result:

```json
{"status":"ok","environment":"staging","service":"kevin-api-staging","revision":"kevin-api-staging-00058-muc","deploy_sha":"d185eddb9283fbd063e90030f5ef4992724153ad"}
```

```bash
git status --short --branch
```

Result before handoff docs: `## codex/jobber-customer-memory...origin/codex/jobber-customer-memory`

```bash
git status --short --branch
```

Result for main checkout:

```text
## main...origin/main [ahead 1, behind 88]
 M AGENTS.md
 M ios/Kevin.xcodeproj/project.pbxproj
 M ios/Kevin/App/AppDelegate.swift
 M ios/Kevin/App/KevinApp.swift
 M ios/Kevin/Models/AppState.swift
 M ios/Kevin/Services/APIClient.swift
 M ios/Kevin/Services/ContactSyncManager.swift
 M ios/Kevin/Services/KeychainManager.swift
 M ios/Kevin/Views/OnboardingView.swift
 M ios/Kevin/Views/SettingsView.swift
 M ios/project.yml
?? .playwright-mcp/
?? docs/superpowers/plans/2026-05-18-admin-dashboard.md
?? docs/superpowers/plans/2026-06-30-v2-phase-0-safety-audit.md
?? docs/superpowers/specs/2026-06-30-business-first-dispatch-v2-design.md
?? jobber-login.png
?? live-screen-1.png
?? live-screen-2.png
?? recents.png
?? scripts/jobber_oauth_probe.py
?? uv.lock
```

## Verification

- Passed locally: Jobber and receptionist tests, full unit test suite, ruff check.
- Passed remotely: GitHub Actions test job for PR #76.
- Passed staging health check: `200 OK`, revision `kevin-api-staging-00058-muc`, deploy SHA `d185eddb9283fbd063e90030f5ef4992724153ad`.
- Not run: live Twilio inbound call to staging.
- Not run: production deploy.
- Not verified: whether the current Twilio test number is routed to staging. The user must ensure the call path hits `https://kevin-api-staging-l63rergg7a-uc.a.run.app`, not production.

## Risks And Watchouts

- High: A normal production Twilio call may still hit production, not staging. Do not claim staging behavior from a call unless Twilio webhook/number routes to the staging URL.
- High: Do not merge or force-push `staging` casually. `origin/staging` is divergent and old. A merge attempt from PR #76 produced broad conflicts across backend, docs, and tests.
- High: Do not work in the dirty main checkout at `/Volumes/Extreme Pro/myprojects/Kevin`. Use the feature worktree for PR #76.
- Medium: Staging was deployed directly from the feature worktree, not from the `staging` branch. The deployed revision is valid for smoke testing but source-of-truth branch hygiene still needs cleanup later.
- Medium: Jobber OAuth callback URL is visible in the in-app browser with an authorization `code` parameter. Do not paste that full URL into public logs or docs.
- Medium: The user said Jobber client secret rotation will happen later. Do not rotate secrets unless explicitly asked.
- Medium: Current local gcloud account may drift. Before future deploys, run `gcloud config list --format='value(core.account,core.project,billing.quota_project)'`.

## Do Not Do

- Do not deploy production without explicit user approval.
- Do not overwrite or force-update `staging` without explicit approval and a plan for its divergence.
- Do not revert unrelated dirty files in the main checkout.
- Do not commit local `secrets/` Jobber probe artifacts.
- Do not expose Jobber OAuth codes, client secret, admin bearer tokens, or phone numbers beyond last four digits in user-facing text.

## Next Recommended Steps

1. Confirm the staging call path. Ensure the Twilio number or webhook used for the test points to `https://kevin-api-staging-l63rergg7a-uc.a.run.app`.
2. Run one live inbound call from the seeded caller ending in `8667`.
3. Read staging/admin call logs after the call and verify:
   - Kevin recognizes the known caller naturally.
   - Kevin does not mention Jobber or private notes.
   - Kevin does not ask for name/address if memory already provides them.
   - Kevin uses prior sink-repair context only as background and does not assume the new toilet request is at the same property without confirmation.
   - First response latency remains acceptable.
4. If staging call passes, decide whether to mark PR #76 ready and merge to `main`.
5. If staging call fails, inspect logs and patch PR #76 in the feature worktree.

## Open Questions

- Which Twilio number/webhook should be used for the staging live-call test?
- Should the next agent update Twilio temporarily to staging, or does a staging/test number already exist?
- After smoke testing, should PR #76 remain draft for another staff review, or be marked ready for merge?
