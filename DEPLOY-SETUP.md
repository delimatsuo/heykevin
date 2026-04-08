# Deployment Setup Log

**Generated**: 2026-04-08
**Generator**: /deploy-setup v1
**Platform**: Cloud Run (GCP)
**Config schema**: v1

## What Was Configured

- [x] Deploy config: `.claude/deploy-config.yaml`
- [x] GitHub Actions: `.github/workflows/deploy.yml` (starter — review TODOs)
- [x] GitHub Actions: `.github/workflows/rollback.yml` (starter — review TODOs)
- [x] Staging branch: `staging` created from `main`
- [x] Git remote: `origin` → `https://github.com/delimatsuo/heykevin`

## Detected Configuration

| Item | Value | Source |
|------|-------|--------|
| Stack | Python 3.12, FastAPI, iOS Swift/SwiftUI | pyproject.toml, ios/ |
| Platform | Cloud Run | Dockerfile + gcloud |
| GCP Project | kevin-491315 | gcloud run services |
| Staging Service | kevin-api-staging (to be created) | deploy-config.yaml |
| Production Service | kevin-api | gcloud run services |
| Production URL | https://kevin-api-752910912062.us-central1.run.app | gcloud run services |
| Git Provider | GitHub | github.com/delimatsuo/heykevin |
| Branch Strategy | feature → staging → main | git branches |
| CI/CD | GitHub Actions | .github/workflows/ |
| GCP Auth | Workload Identity Federation | deploy-config.yaml |

## Manual Steps Required

### Priority 1 — GitHub Setup
- [ ] Push code to GitHub: `git push -u origin main && git push -u origin staging`
- [ ] Create GitHub repository at https://github.com/delimatsuo/heykevin if not already done

### Priority 2 — Workload Identity Federation (required for CI/CD)
- [ ] Enable APIs: `gcloud services enable iamcredentials.googleapis.com --project kevin-491315`
- [ ] Create a Workload Identity Pool:
  ```
  gcloud iam workload-identity-pools create "github-pool" \
    --project=kevin-491315 --location="global" \
    --display-name="GitHub Actions Pool"
  ```
- [ ] Create a provider in the pool:
  ```
  gcloud iam workload-identity-pools providers create-oidc "github-provider" \
    --project=kevin-491315 --location="global" \
    --workload-identity-pool="github-pool" \
    --display-name="GitHub provider" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
    --issuer-uri="https://token.actions.githubusercontent.com"
  ```
- [ ] Create a service account for GitHub Actions:
  ```
  gcloud iam service-accounts create github-actions \
    --project=kevin-491315 \
    --display-name="GitHub Actions deployer"
  ```
- [ ] Grant the service account Cloud Run deployer + Storage roles:
  ```
  gcloud projects add-iam-policy-binding kevin-491315 \
    --member="serviceAccount:github-actions@kevin-491315.iam.gserviceaccount.com" \
    --role="roles/run.admin"
  gcloud projects add-iam-policy-binding kevin-491315 \
    --member="serviceAccount:github-actions@kevin-491315.iam.gserviceaccount.com" \
    --role="roles/storage.admin"
  gcloud projects add-iam-policy-binding kevin-491315 \
    --member="serviceAccount:github-actions@kevin-491315.iam.gserviceaccount.com" \
    --role="roles/iam.serviceAccountUser"
  ```
- [ ] Allow GitHub Actions to impersonate the service account:
  ```
  gcloud iam service-accounts add-iam-policy-binding \
    github-actions@kevin-491315.iam.gserviceaccount.com \
    --project=kevin-491315 \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/delimatsuo/heykevin"
  ```
  (Replace PROJECT_NUMBER with your actual GCP project number)

### Priority 3 — GitHub Repository Variables
Add these as **Variables** (not secrets) in GitHub repo Settings → Secrets and variables → Actions:
- [ ] `WIF_PROVIDER` = `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider`
- [ ] `WIF_SERVICE_ACCOUNT` = `github-actions@kevin-491315.iam.gserviceaccount.com`

Also add the backend **Secrets** that the app needs at runtime (currently in Cloud Run env vars):
- [ ] These are already set on the Cloud Run service — the workflow deploys with `--source .` which picks up the existing env var config. No action needed unless you add new env vars.

### Priority 4 — Update deploy-config.yaml
- [ ] After completing WIF setup, fill in the `wif_provider` and `wif_service_account` fields in `.claude/deploy-config.yaml`
- [ ] Update `environments.staging.url` once the staging service is first deployed

### Priority 5 — Branch Protection
- [ ] Enable branch protection on `main`: require PR review + passing CI before merge
- [ ] Enable branch protection on `staging`: require passing CI

### Priority 6 — Staging Cloud Run App Store Config
- [ ] The staging service will need App Store sandbox credentials in its env vars
- [ ] Set `APPSTORE_ENVIRONMENT=sandbox` on the staging service after first deploy

## Deployment Flow (once configured)

```
Feature branch → merge to staging → CI deploys to kevin-api-staging → test on phone
                  → merge to main → CI deploys to kevin-api (production)
```

For now (before CI/CD is wired up), deploy manually:
```bash
# Deploy to staging
git checkout staging && git merge main
gcloud run deploy kevin-api-staging --source . --project kevin-491315 --region us-central1 --allow-unauthenticated

# Deploy to production (after testing staging)
git checkout main
gcloud run deploy kevin-api --source . --project kevin-491315 --region us-central1 --allow-unauthenticated
```
