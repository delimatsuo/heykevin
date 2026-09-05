You're picking up an in-flight session in Hey Kevin (delimatsuo/heykevin). Read these in order before doing anything:

1. docs/handoffs/2026-09-04-screening-push-feedback-b37-handoff.md
2. docs/current-roadmap.md
3. AGENTS.md and .claude/DECISIONS.md

Then check current state:
- gh pr list --repo delimatsuo/heykevin
- git log --oneline -1 origin/main
- curl -fsS https://kevin-api-752910912062.us-central1.run.app/health | jq
- curl -fsS https://kevin-api-staging-l63rergg7a-uc.a.run.app/health | jq
- gh run view 33938295394 --repo delimatsuo/heykevin

Owner is Deli Matsuo. Workspace: /Volumes/Extreme Pro/MYPROJECTS/Kevin

Current state:
- Release build 37 (1.2.11) is uploaded, valid, and live for internal testing in TestFlight.
- Staging revision `kevin-api-staging-00165-kif` (c093ed6) is live.
- Production deploy workflow run 33938295394 is waiting at the GitHub Actions manual approval gate for Deli to approve.
