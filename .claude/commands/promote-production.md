---
name: promote-production
description: Use when staging has been tested and is ready to go to production. Verifies staging health, creates rollback tag, merges staging to main, and monitors CI deploy to kevin-api.
---

# Promote to Production — Kevin AI

Promotes `staging` → `main`, triggering CI deploy to `kevin-api` (production).

## Project Config

| Item | Value |
|------|-------|
| Production service | `kevin-api` |
| Production URL | `https://kevin-api-752910912062.us-central1.run.app` |
| Production branch | `main` |
| GCP project | `kevin-491315` |
| Region | `us-central1` |
| Health endpoint | `/health` |
| CI trigger | Push to `main` → GitHub Actions auto-deploys |

## Process

### Step 1: Verify Staging is Healthy

```bash
STAGING_URL=$(gcloud run services describe kevin-api-staging \
  --project kevin-491315 \
  --region us-central1 \
  --format='value(status.url)' 2>/dev/null)

STATUS=$(curl -sf -o /dev/null -w "%{http_code}" "$STAGING_URL/health" || echo "000")
echo "Staging health: HTTP $STATUS"
```

If staging is not HTTP 200: **STOP**. Fix it before promoting.

### Step 2: Create Rollback Tag

Tag the current production HEAD before overwriting it — this is your escape hatch.

```bash
ROLLBACK_TAG="prod-rollback-$(date +%Y-%m-%d)-$(git rev-parse --short origin/main)"
git tag "$ROLLBACK_TAG" origin/main
git push origin "$ROLLBACK_TAG"
echo "Rollback tag: $ROLLBACK_TAG"
```

Keep this tag — it lets you roll back via GitHub Actions `/rollback` workflow.

### Step 3: Merge Staging → Main

```bash
git checkout main
git pull origin main
git merge staging --no-ff -m "chore: promote staging to production $(date +%Y-%m-%d)"
git push origin main
git checkout -  # return to previous branch
```

### Step 4: Monitor CI Deploy

```bash
sleep 10
RUN_ID=$(gh run list --repo delimatsuo/heykevin --branch main --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --repo delimatsuo/heykevin
```

If CI fails: **DO NOT retry blindly.** Check logs:
```bash
gh run view "$RUN_ID" --repo delimatsuo/heykevin --log-failed | tail -50
```

### Step 5: Production Health Check

```bash
sleep 15
STATUS=$(curl -sf -o /dev/null -w "%{http_code}" \
  "https://kevin-api-752910912062.us-central1.run.app/health" || echo "000")
echo "Production health: HTTP $STATUS"
```

### Step 6: Report

```
============================================
PRODUCTION DEPLOYMENT
============================================
Branch:       staging → main
Service:      kevin-api
URL:          https://kevin-api-752910912062.us-central1.run.app
Health:       HTTP 200 ✓
Rollback tag: <tag-name>

If anything breaks: use rollback tag in GitHub Actions
→ https://github.com/delimatsuo/heykevin/actions/workflows/rollback.yml
============================================
```

## Rolling Back

If production is broken after promote:

**Option A — GitHub Actions rollback (preferred):**
1. Go to https://github.com/delimatsuo/heykevin/actions/workflows/rollback.yml
2. Run workflow: environment=`production`, method=`traffic-split`, revision=`<rollback-tag>`

**Option B — Manual traffic split:**
```bash
# Find the previous revision
gcloud run revisions list --service kevin-api --project kevin-491315 --region us-central1 --limit 5

# Route 100% traffic to previous revision
gcloud run services update-traffic kevin-api \
  --project kevin-491315 \
  --region us-central1 \
  --to-revisions=<previous-revision>=100
```

**Option C — Redeploy rollback tag:**
```bash
git checkout <rollback-tag>
gcloud run deploy kevin-api \
  --source . \
  --project kevin-491315 \
  --region us-central1 \
  --allow-unauthenticated
git checkout main
```

## When to Stop

- Staging health check fails → fix on staging, re-test before promoting
- Merge conflict → resolve on staging branch first, re-test, then promote
- CI fails on main → investigate, fix on a branch, deploy-staging again, then re-promote
- Production health fails → rollback immediately, investigate offline

## NEVER

- Force-push `main`
- Skip the rollback tag
- Promote directly from a feature branch (always go through staging first)
- Delete rollback tags (`prod-rollback-*`)
