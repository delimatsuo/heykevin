You are continuing work on Hey Kevin’s Boston public call demo.

Current objective:
Make +1 857-810-6804 feel like Jobber’s AI receptionist: short greeting, no spoken legal dump, named days such as Friday resolving to that day. This is the try-Kevin line for a fictional Boston plumber, not a production kevin-api release.

Workspace:
- Repo/worktree: /Volumes/Extreme Pro/MYPROJECTS/Kevin
- Branch: main at d3cd5c7369933a4a2563fd7c505b741437b1625d
- Important docs to read first:
  - docs/handoffs/2026-08-17-public-demo-live-ux-handoff.md
  - docs/public-call-demo.md
  - CLAUDE.md / AGENTS.md for deploy boundaries
  - ~/.claude/projects/-Volumes-Extreme-Pro-myprojects-Kevin/memory/MEMORY.md

Newest user request:
/handoff after Deli asked what the agent is building toward. Goal: a public number a stranger can call and immediately believe Kevin. Do not invent the next product.

Current state:
- Clean main matching origin/main except two untracked handoff files in docs/handoffs/.
- Live Cloud Run kevin-public-demo (hk-public-demo-bos-260811 / us-east4) revision kevin-public-demo-00031-tgd, /health deploy_sha=d3cd5c7…, public_demo_enabled=true, 100% traffic (gcloud confirmed 11:33 EDT).
- PRs #171 preferred_date (ISO), #172 short greeting, #173 weekday parser are merged and on that revision.
- Deli’s Friday→9am call happened before #173. Owner has not confirmed a new live call.
- Only open PR: draft #165 customer memory. Leave it and .worktrees/customer-memory alone.

Critical constraints:
- Do not deploy kevin-api / GCP kevin-491315.
- Do not start app.main:app on the demo service; command must be uvicorn app.public_demo_main:app --host 0.0.0.0 --port 8080 --workers 1.
- Do not prepend PUBLIC_DEMO_DISCLOSURE to _build_greeting_text (0501a44 already did that; owner rejected it).
- Do not --clear-env-vars / --set-env-vars; only --update-env-vars DEPLOY_SHA=….
- Do not redeploy the breaker or disable PUBLIC_DEMO_ENABLED unless rolling the number offline.
- After gcloud run deploy, tagged revisions do not auto-serve. List revisions by DEPLOY_SHA and update-traffic NEW=100. Use CLOUDSDK_CORE_ACCOUNT=delimatsuo@gmail.com.
- Standing rule: merge and deploy demo-only after a staff-engineer check; do not ask permission for that loop. Do not force-push.

Facts and evidence:
- Friday on Mon 2026-08-17 used to fall back to Tue 9am because "Friday" failed ISO parse (tests/unit/test_public_demo_policy.py now asserts 2026-08-21T09:00:00-04:00).
- curl -sS https://kevin-public-demo-947421709125.us-east4.run.app/health at 11:33 EDT: revision 00031-tgd, sha d3cd5c7.
- pytest tests/unit/test_public_demo_policy.py tests/unit/test_public_demo_webhook.py: 80 passed.
- Rollback: update-traffic kevin-public-demo-00030-gp2=100 (short greeting, no weekday parser).

Next recommended action:
1. If Deli has not called yet, wait for that live check or place a controlled call: short greeting, then “toilet replacement Friday” should be spoken as Friday Aug 21 9am and 1pm.
2. If that call is good, stop and wait for Deli’s next pointer. Do not invent a product.
3. If it is still wrong, inspect public_demo event=tool name=check_availability resolved_date= logs, then fix the actual miss (omitted tool arg vs spoken offer ignoring slots). Do not collapse the 3-slot fixture unless he asks.

Verification expected:
```bash
git status --short --branch
curl -sS https://kevin-public-demo-947421709125.us-east4.run.app/health
.venv/bin/python -m pytest tests/unit/test_public_demo_policy.py tests/unit/test_public_demo_webhook.py -q
```

Known risks:
- gcloud run deploy will claim an old tagged revision is serving 100%. Believe DEPLOY_SHA + update-traffic, not that line.
- Friday still has a 9am window by fixture design; after #173 it must be Friday 9am, not tomorrow 9am.
- /health is liveness, not proof the spoken Friday offer works.

If anything conflicts, the newest user request wins. Start by running:

```bash
git status --short --branch
```
