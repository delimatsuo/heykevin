# Public Call Demo Live UX Handoff

Created: 2026-08-17 11:36 EDT
Prepared by: Cursor Grok 4.6 (refresh of the 11:29 EDT Codex handoff after Deli asked what the agent is building toward)

## What we are building toward

A stranger should be able to call **+1 (857) 810-6804**, hear a short AI receptionist, and believe Kevin the way Jobber’s AI receptionist works: greet, take the job, offer real-sounding times. That number is the try-Kevin line for a **fictional Boston plumber**. It is not a production `kevin-api` release, not a new product surface, and not a roadmap.

This stretch of work is finished in code and live on Cloud Run. The only remaining step is a live call after weekday parsing shipped. Do not invent the next product node.

## Objective

Make the already-live Boston public demo number feel like a Jobber-style AI receptionist: short greeting, no spoken legal dump, and a named day such as Friday resolving to that day rather than tomorrow 9am.

## Current State

- Repo/workspace: `/Volumes/Extreme Pro/MYPROJECTS/Kevin`
- Branch: `main` tracking `origin/main`
- Worktree: primary checkout at `/Volumes/Extreme Pro/MYPROJECTS/Kevin` on `d3cd5c7`
- Latest commit: `d3cd5c7369933a4a2563fd7c505b741437b1625d` `fix: honor weekday names on public demo availability (#173)`
- Dirty state: two untracked handoff files only (`docs/handoffs/2026-08-17-public-demo-live-ux-handoff.md` and `docs/handoffs/2026-08-17-public-demo-live-ux-new-session-prompt.md`). No code changes in this 11:32 session.
- Related worktree: `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory` on `codex/customer-memory` tracking `origin/codex/customer-memory`. Leave it alone; it belongs to draft PR #165. That worktree also has its own untracked 2026-08-12 handoff files.
- Live demo: `kevin-public-demo` in GCP `hk-public-demo-bos-260811` / `us-east4`
- Boston number: `+1 857-810-6804`
- Entry point must stay `uvicorn app.public_demo_main:app --host 0.0.0.0 --port 8080 --workers 1`

Fresh live checks at 11:33 EDT this session:

```text
GET https://kevin-public-demo-947421709125.us-east4.run.app/health
{"status":"ok","environment":"demo","service":"kevin-public-demo","revision":"kevin-public-demo-00031-tgd","deploy_sha":"d3cd5c7369933a4a2563fd7c505b741437b1625d","public_demo_enabled":true}
```

```text
gcloud run services describe kevin-public-demo --project hk-public-demo-bos-260811 --region us-east4
latestCreatedRevisionName: kevin-public-demo-00031-tgd
latestReadyRevisionName: kevin-public-demo-00031-tgd
traffic:
  - revisionName: kevin-public-demo-00028-mef   tag: audio-frame-candidate   (0%)
  - revisionName: kevin-public-demo-00030-muc   tag: calm-returning-candidate (0%)
  - percent: 100  revisionName: kevin-public-demo-00031-tgd
```

The published number uses percent traffic. Tagged candidate URLs still hit old SHAs.

## Newest User Request

`/handoff` at 11:32 AM EDT, after Deli asked what the agent was building toward.

Answer to that question: a public number a stranger can call and immediately believe Kevin. Do not invent a separate product roadmap.

Standing owner rule from 10:32 AM EDT: merge and deploy demo-only after a staff-engineer check; stop asking permission for that loop. Still do not force-push, drop data, or deploy production `kevin-api`.

## Completed Work

Shipped and live on `00031-tgd` / SHA `d3cd5c7`:

- Merged PR #170 (Gemini 3.x hyphenated `thinking_level` IDs). Not demo-specific.
- Merged PR #171 (`2bcd67a`): optional `preferred_date` on demo `check_availability`. ISO `YYYY-MM-DD` only at first. Default fixture unchanged: tomorrow 9am/1pm, next day 10am.
- Deployed `main` `8646eaa` to `kevin-public-demo`. Cloud Run created `kevin-public-demo-00029-g69` but left 100% traffic on tagged revision `kevin-public-demo-00030-muc` (`11b12fe`, Jobber-style greeting). Traffic was then shifted to `00029-g69`.
- That shift also shipped PR #168 commit `0501a44`, which prepended `PUBLIC_DEMO_DISCLOSURE` to Kevin’s first turn. Deli heard the full legal dump.
- Merged PR #172 (`c888167` / merge `96cc524`): restored the short spoken greeting from `11b12fe`. Tests now fail if the legal paragraph is spoken. `docs/public-call-demo.md` says the dump is landing-page copy, not spoken.
- Deployed `96cc524` as `kevin-public-demo-00030-gp2` and shifted 100% traffic onto it.
- Deli asked for a toilet-replacement appointment on Friday (Monday 2026-08-17) and heard 9am. That 9am was the default tomorrow fixture (Tue Aug 18), because `"Friday"` failed `date.fromisoformat` and fell back to tomorrow.
- Merged PR #173 (`afb20c2` / merge `d3cd5c7`): weekday names resolve to the next occurrence on or after tomorrow in America/New_York; prompt injects TODAY/tomorrow; Kevin is told to say weekday+date, not a clock time alone; `check_availability` logs `resolved_date` with no caller identity.
- Deployed `d3cd5c7` as `kevin-public-demo-00031-tgd` and shifted 100% traffic onto it. Fresh health and traffic at 11:33 EDT still match.

Closed this stretch, do not reopen: PR #76, #111, #130. Draft PR #165 left open.

This 11:32 session did not change code, merge, or deploy. It re-verified live health, traffic, and unit tests, then refreshed these handoff files.

## In Progress

- Owner has not confirmed a post-#173 live call. The Friday 9am report was against the pre-weekday-parser revision (`00030-gp2` / `96cc524`).
- After #173, Friday on Monday Aug 17 still includes a Friday 9am slot plus Friday 1pm and Saturday 10am (existing 3-slot fixture starting on the preferred day). If Deli still hears a bare “9am” with no weekday, that is a remaining spoken-offer bug, not the old tomorrow-fallback.
- Graph-engineering canvas exists at `/Users/delimatsuo/.cursor/projects/Volumes-Extreme-Pro-MYPROJECTS-Kevin/canvases/heykevin-workflow-graph.canvas.tsx`. It is session UI, not the source of truth for live infra.

## Important Decisions

- Public demo goal is Jobber-like UX on `+1 857-810-6804`, not a production backend release.
- Spoken greeting is the short receptionist intro from `11b12fe` / `_build_greeting_text` in `app/services/public_demo_pipeline.py`. Do not prepend `PUBLIC_DEMO_DISCLOSURE`. `0501a44` already did that against owner direction.
- Weekday names are parsed in code (`_parse_preferred_date` in `app/services/public_demo.py`). Do not rely on Gemini emitting ISO dates.
- Keep the 3-slot synthetic shape (preferred day 9am/1pm, next day 10am) unless the owner asks to collapse named-day requests to that day only.
- `gcloud run deploy` against this service does not move live traffic when older revisions are tagged. After every deploy, list revisions by `DEPLOY_SHA` and `update-traffic` the new revision to 100%.
- Deploy identity for this isolated project is `delimatsuo@gmail.com` (`CLOUDSDK_CORE_ACCOUNT=delimatsuo@gmail.com`). `deli@ellaexecutivesearch.com` is the default gcloud account and cannot refresh tokens non-interactively.

## Files And Artifacts

- `app/services/public_demo.py`: disclosure constant (written/prompt only), weekday parser, slot fixture, prompt TODAY line, tool execution, `resolved_date` log.
- `app/services/public_demo_pipeline.py`: spoken greeting (`_build_greeting_text`), Gemini tool schema for `preferred_date`.
- `app/public_demo_main.py`: `/health` includes `deploy_sha` and `public_demo_enabled`.
- `docs/public-call-demo.md`: live status, written disclosure, “do not speak the dump”, activation vs refresh.
- `tests/unit/test_public_demo_policy.py`: default fixture pin, ISO preferred_date, weekday Friday from Mon Aug 17 → `2026-08-21T09:00:00-04:00`.
- `tests/unit/test_public_demo_webhook.py`: greeting must not contain `PUBLIC_DEMO_DISCLOSURE`.
- `docs/handoffs/2026-08-17-public-demo-live-ux-new-session-prompt.md`: paste-ready continuation prompt.
- `CLAUDE.md` / `AGENTS.md`: production `kevin-api` is GCP `kevin-491315`; this demo is a different project.

## Commands Run And Results

```bash
git status --short --branch
```

Result at 11:32 EDT: `## main...origin/main` plus the two untracked handoff files. HEAD `d3cd5c7369933a4a2563fd7c505b741437b1625d`.

```bash
curl -sS https://kevin-public-demo-947421709125.us-east4.run.app/health
```

Result at 11:33 EDT: revision `kevin-public-demo-00031-tgd`, `deploy_sha=d3cd5c7369933a4a2563fd7c505b741437b1625d`, `public_demo_enabled=true`.

```bash
CLOUDSDK_CORE_ACCOUNT=delimatsuo@gmail.com gcloud run services describe kevin-public-demo \
  --project hk-public-demo-bos-260811 --region us-east4 \
  --format='yaml(status.traffic,status.latestReadyRevisionName,status.latestCreatedRevisionName)'
```

Result at 11:33 EDT: 100% traffic on `kevin-public-demo-00031-tgd`. Tagged revisions `00028-mef` and `00030-muc` are 0%.

```bash
.venv/bin/python -m pytest tests/unit/test_public_demo_policy.py tests/unit/test_public_demo_webhook.py -q
```

Result at 11:33 EDT: **80 passed**, 4 warnings (unrelated deprecations), 0.90s.

```bash
gh pr list --repo delimatsuo/heykevin --state open
```

Result: only draft #165 (`codex/customer-memory`).

## Verification

- Passed this session: unit tests for greeting-without-dump and weekday Friday → Aug 21; live `/health` SHA `d3cd5c7` on revision `00031-tgd`; `gcloud` traffic 100% on that revision; `main` matches `origin/main`.
- Failed: none in this session.
- Not run: a new owner live call after `00031-tgd`. The Friday complaint was on the previous revision. `/health` is liveness, not proof that spoken Friday works.

## Risks And Watchouts

- **Traffic pin (high):** `gcloud run deploy` will print that an old tagged revision “has been deployed and is serving 100 percent.” That is a lie when `audio-frame-candidate` / `calm-returning-candidate` tags exist. Always `gcloud run revisions list` and `update-traffic NEW=100`. Tagged URLs still hit old SHAs; the published number uses percent traffic.
- **Spoken dump regression (high):** any agent that reads older copies of `docs/public-call-demo.md` (as of `0501a44`) may try to put `PUBLIC_DEMO_DISCLOSURE` back in `_build_greeting_text`. Current tests and the current doc forbid it.
- **Friday still has 9am (medium):** the fixture’s first window on a named day is 9am. After #173 that 9am should be Friday, with 1pm, not Tuesday.
- **No raw Gemini tool-arg logs:** `_handle_tool_calls` does not log raw Gemini args. `resolved_date` is logged on `check_availability` only.
- **Auth:** demo deploys must use `CLOUDSDK_CORE_ACCOUNT=delimatsuo@gmail.com`. Do not use the Ella Executives Search account for this project.
- **Production confusion (high):** Hey Kevin production is `kevin-api` in `kevin-491315`. This demo is `kevin-public-demo` in `hk-public-demo-bos-260811`. Do not mix them.

## Mistakes already made — do not repeat

- Prepended `PUBLIC_DEMO_DISCLOSURE` to the spoken greeting in `0501a44`. Owner rejected it. The dump stays written/prompt-only.
- Trusted `gcloud run deploy`’s “serving 100 percent” line. Tagged revisions can keep live traffic. Always `update-traffic` after listing by `DEPLOY_SHA`.
- ISO-only `preferred_date` (`#171`) left `"Friday"` as a parse miss, so the fixture fell back to tomorrow 9am. Callers say weekday names; parse them in code.
- Asking permission for the demo merge/deploy loop after the owner already authorized it. Staff-engineer check, then merge and deploy. Still do not force-push or touch `kevin-api`.
- Inventing a next product after the demo UX stretch. Deli asked what we are building toward; the answer is the live number feeling like Kevin, not a new surface.

## Tracked follow-ups

Canonical tracker: this file. There is no `.planning/` directory in this repo.

- Live post-#173 call: confirm short greeting and Friday → Aug 21 9am/1pm spoken with the weekday.
- Optional later: collapse named-day requests to that day only (drop Saturday 10am from a Friday request). Not asked; do not hold for it.
- Draft PR #165 customer memory: owned by a different worktree. Do not pick it up from this demo session.

## What to do RIGHT NOW

1. If Deli has not called yet, wait for that live check or place a controlled call to `+1 (857) 810-6804`.
   - Expected greeting: short receptionist intro from `_build_greeting_text`. Must not contain the legal dump.
   - Then say something like “toilet replacement Friday.”
   - Expected spoken offer: Friday Aug 21 9am and 1pm (and the fixture’s Saturday 10am unless he later asks to drop it).
2. If that call is good, **stop**. Wait for Deli’s next product pointer. Do not invent one.
3. If Friday is still wrong, pull Cloud Run logs for `public_demo event=tool name=check_availability resolved_date=` around the call, then decide whether Gemini omitted `preferred_date` or the spoken offer ignored the tool result. Do not collapse the 3-slot fixture unless he asks.
4. On any further demo code deploy: pin `DEPLOY_SHA` to `git rev-parse HEAD`, then `update-traffic` the new revision to 100%.

Rollback if the new revision is bad:

```bash
CLOUDSDK_CORE_ACCOUNT=delimatsuo@gmail.com gcloud run services update-traffic kevin-public-demo \
  --project hk-public-demo-bos-260811 --region us-east4 \
  --to-revisions kevin-public-demo-00030-gp2=100
```

`00030-gp2` is short greeting without weekday parsing (`96cc524`). `00030-muc` is the Aug 12 Jobber greeting without preferred_date and without the dump (`11b12fe`).

## Do Not Do

- Do not deploy `kevin-api` or anything in GCP `kevin-491315`.
- Do not point `+1 857-810-6804` at `/webhooks/twilio/incoming`.
- Do not start `app.main:app` on `kevin-public-demo`.
- Do not `--clear-env-vars` or `--set-env-vars` (wipes live secrets). `--update-env-vars DEPLOY_SHA=...` only.
- Do not set `PUBLIC_DEMO_ENABLED=false` unless rolling the number offline.
- Do not redeploy `kevin-public-demo-breaker` for these UX fixes.
- Do not touch draft PR #165 or `.worktrees/customer-memory`.
- Do not reopen bakeoff, visual diagnosis A3+, Jobber live writes, Relay customer rollout, or A2P.
- Do not treat tagged candidate URLs as the live number.
- Do not invent the next product after a good live call.

## Owner communication preferences

- Lead with a recommendation and the reasoning. Do not dump option menus and wait (`feedback_decisions_need_recommendations.md`).
- His time is for product, scope, spend, and authorization. Make defensible technical micro-decisions.
- Ask before commit, deploy of production `kevin-api`, or irreversible actions. Demo merge/deploy after staff-engineer check is already authorized.
- `/health` and `smoke_release.sh` are liveness, not proof Kevin works (`feedback_deploy_verification_standard.md`). For this demo, the proof is a live call.
- App is live in the App Store. Do not talk about Hey Kevin as pre-launch. This Boston number is still a fictional demo, not the production tenant API.

## Open Questions

- Did the post-#173 live number actually say Friday, or only “9am”? Unknown until Deli calls again or a controlled call is placed.
- Should a named day return only that day’s windows (drop Saturday 10am from a Friday request)? Not asked; staff said do not hold the ship for that.
- What product node comes after the demo feels like Jobber? Unspecified; do not invent one.
