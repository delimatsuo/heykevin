---
name: deploy-staging
description: Use when deploying current feature branch to kevin-api-staging for phone testing. Runs pre-flight checks, merges to staging branch, pushes to trigger CI, and verifies health.
---

# Deploy to Staging — Kevin AI

Deploys current branch to `kevin-api-staging` on Cloud Run via GitHub Actions.

## Project Config

| Item | Value |
|------|-------|
| Staging service | `kevin-api-staging` |
| Staging branch | `staging` |
| GCP project | `kevin-491315` |
| Region | `us-central1` |
| Health endpoint | `/health` |
| CI trigger | Push to `staging` → GitHub Actions auto-deploys |
| GitHub repo | `https://github.com/delimatsuo/heykevin` |

## Process

### Step 1: Pre-Flight

```bash
# Verify tests pass
python3 -m pytest --tb=short -q

# Check for uncommitted changes
git status --short
git stash list
```

If tests fail: **STOP**. Fix tests before deploying.

If uncommitted changes exist: commit or stash them first.

### Step 2: Merge to Staging

```bash
FEATURE_BRANCH=$(git branch --show-current)

# Safety check — never merge staging or main into staging
if [[ "$FEATURE_BRANCH" == "staging" || "$FEATURE_BRANCH" == "main" ]]; then
  echo "ERROR: Already on $FEATURE_BRANCH — nothing to deploy"
  exit 1
fi

git checkout staging
git pull origin staging
git merge "$FEATURE_BRANCH" --no-ff -m "chore: merge $FEATURE_BRANCH to staging for testing"
git push origin staging
git checkout "$FEATURE_BRANCH"
```

### Step 3: Monitor CI

```bash
# Wait for run to start, then watch
sleep 10
RUN_ID=$(gh run list --repo delimatsuo/heykevin --branch staging --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --repo delimatsuo/heykevin
```

If CI fails: check logs with `gh run view "$RUN_ID" --repo delimatsuo/heykevin --log-failed`

### Step 4: Health Check

After CI passes, get the staging URL and verify:

```bash
STAGING_URL=$(gcloud run services describe kevin-api-staging \
  --project kevin-491315 \
  --region us-central1 \
  --format='value(status.url)' 2>/dev/null)

if [ -z "$STAGING_URL" ]; then
  echo "INFO: kevin-api-staging doesn't exist yet — CI will create it on first deploy"
else
  STATUS=$(curl -sf -o /dev/null -w "%{http_code}" "$STAGING_URL/health" || echo "000")
  echo "Staging health: HTTP $STATUS at $STAGING_URL"
fi
```

### Step 5: Report

```
============================================
STAGING DEPLOYMENT
============================================
Branch:   <feature-branch> → staging
CI Run:   <run-url>
Service:  kevin-api-staging
URL:      <staging-url>
Health:   HTTP 200 ✓

Test on your phone, then run /promote-production when ready.
============================================
```

## Manual Deploy (bypass CI)

If CI is not working and you need to deploy directly:

```bash
gcloud run deploy kevin-api-staging \
  --source . \
  --project kevin-491315 \
  --region us-central1 \
  --allow-unauthenticated
```

## When to Stop

- Tests fail locally → fix before merging
- CI fails → check logs, fix, re-push to staging
- Health check fails after 3 retries → investigate before promoting
- Merge conflict → resolve manually, do NOT force push

## Next Step

After verifying on your phone: `/promote-production`
