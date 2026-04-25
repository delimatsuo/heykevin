# Deployment And Isolation Setup

**Generated**: 2026-04-08
**Updated**: 2026-04-25
**Platform**: Cloud Run (GCP)
**Config schema**: v1

## What Is Configured

- [x] Deploy config: `.claude/deploy-config.yaml`
- [x] GitHub Actions: `.github/workflows/deploy.yml`
- [x] GitHub Actions: `.github/workflows/rollback.yml`
- [x] Staging branch: `staging` created from `main`
- [x] Git remote: `origin` -> `https://github.com/delimatsuo/heykevin`
- [x] Backend runtime guard rejects staging/development when pointed at production Firestore or RTDB
- [x] iOS has separate `Kevin Staging` and `Kevin` schemes

## Detected Configuration

| Item | Value | Source |
|------|-------|--------|
| Stack | Python 3.12, FastAPI, iOS Swift/SwiftUI | pyproject.toml, ios/ |
| Platform | Cloud Run | Dockerfile + gcloud |
| GCP Project | kevin-491315 | deploy config |
| Staging Service | kevin-api-staging | deploy config |
| Staging URL | https://kevin-api-staging-l63rergg7a-uc.a.run.app | deploy config |
| Production Service | kevin-api | deploy config |
| Production URL | https://kevin-api-752910912062.us-central1.run.app | deploy config |
| Git Provider | GitHub | github.com/delimatsuo/heykevin |
| Branch Strategy | feature -> PR to staging -> test -> PR to main -> manual production deploy | workflow |
| CI/CD | GitHub Actions | .github/workflows/ |
| GCP Auth | Workload Identity Federation | deploy config |

## Release Flow

```
feature branch
  -> pull request into staging
  -> push/merge to staging deploys kevin-api-staging
  -> test with the Kevin Staging iOS scheme
  -> pull request into main
  -> manually run GitHub Actions > Deploy > target=production from main
```

Production no longer deploys automatically from a push to `main`. This keeps normal development isolated from live users while preserving a clear manual release button when it is time to update production.

## Required External Settings

### Priority 1 - Workload Identity Federation

- [ ] Enable APIs: `gcloud services enable iamcredentials.googleapis.com --project kevin-491315`
- [ ] Create a Workload Identity Pool:

  ```bash
  gcloud iam workload-identity-pools create "github-pool" \
    --project=kevin-491315 --location="global" \
    --display-name="GitHub Actions Pool"
  ```

- [ ] Create a provider in the pool:

  ```bash
  gcloud iam workload-identity-pools providers create-oidc "github-provider" \
    --project=kevin-491315 --location="global" \
    --workload-identity-pool="github-pool" \
    --display-name="GitHub provider" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
    --issuer-uri="https://token.actions.githubusercontent.com"
  ```

- [ ] Create a service account for GitHub Actions:

  ```bash
  gcloud iam service-accounts create github-actions \
    --project=kevin-491315 \
    --display-name="GitHub Actions deployer"
  ```

- [ ] Grant the service account Cloud Run deploy permissions:

  ```bash
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

  ```bash
  gcloud iam service-accounts add-iam-policy-binding \
    github-actions@kevin-491315.iam.gserviceaccount.com \
    --project=kevin-491315 \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/delimatsuo/heykevin"
  ```

### Priority 2 - GitHub Environments

Create GitHub Environments named `staging` and `production`.

Add these variables to both environments:

- [ ] `WIF_PROVIDER` = `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider`
- [ ] `WIF_STAGING_SERVICE_ACCOUNT` = staging-only deployer service account
- [ ] `WIF_PRODUCTION_SERVICE_ACCOUNT` = production-only deployer service account
- [ ] `STAGING_RUNTIME_SERVICE_ACCOUNT` = staging Cloud Run runtime service account
- [ ] `PRODUCTION_RUNTIME_SERVICE_ACCOUNT` = production Cloud Run runtime service account
- [ ] `PRODUCTION_TWILIO_ACCOUNT_SID` = production Twilio Account SID, used by runtime safety checks

Add these variables to the `staging` environment:

- [ ] `FIRESTORE_PROJECT_ID` = a non-production Firebase/GCP project ID, not `kevin-491315`
- [ ] `FIREBASE_DATABASE_URL` = the staging RTDB URL, not `https://kevin-491315-rtdb.firebaseio.com`
- [ ] `APNS_SANDBOX` = `false` for the current shared production APNs entitlement. Use `true` only after splitting staging to a development entitlement/bundle.

Runtime secrets such as Twilio, Deepgram, ElevenLabs, Gemini, APNs, and App Store keys remain Cloud Run service env vars or Secret Manager-backed values. Staging must use separate Twilio credentials or a Twilio subaccount so test number provisioning, webhooks, SMS, and call cleanup cannot touch live customer numbers. The backend now rejects staging/development if `TWILIO_ACCOUNT_SID` equals `PRODUCTION_TWILIO_ACCOUNT_SID`.

### Priority 3 - GitHub Protections

- [ ] Protect `main`: require pull request review and passing `Deploy / Test`
- [ ] Protect `staging`: require passing `Deploy / Test`
- [ ] Add required reviewers to the GitHub `production` environment before deployments can proceed

### Priority 4 - GCP Verification

- [ ] Confirm `kevin-api` has `ENVIRONMENT=production`, `APPSTORE_ENVIRONMENT=production`, `APNS_SANDBOX=false`
- [ ] Confirm `kevin-api-staging` has `ENVIRONMENT=staging`, `APPSTORE_ENVIRONMENT=sandbox`
- [ ] Confirm `kevin-api-staging` points at staging Firestore/RTDB and staging Twilio resources
- [ ] Confirm the GitHub Actions deploy service account can deploy Cloud Run, but day-to-day user accounts do not need broad production deploy rights

After GitHub and GCP credentials are available locally, run:

```bash
scripts/verify_environment_isolation.sh
```

## iOS Schemes

| Scheme | Run | Archive | Backend |
|--------|-----|---------|---------|
| `Kevin Staging` | Staging | Staging | `kevin-api-staging` |
| `Kevin` | Debug | Release | Debug uses staging; archive uses production |

`BackendURL` is required at runtime. Debug/Staging builds crash immediately if they are configured with the production backend URL, so a missing or bad build setting cannot silently send development traffic to production.

## Manual Deploy Fallback

Use GitHub Actions for normal releases. If CI/CD is unavailable, include the runtime environment variables explicitly:

```bash
# Deploy to staging
git checkout staging && git merge main
gcloud run deploy kevin-api-staging \
  --source . \
  --project kevin-491315 \
  --region us-central1 \
  --allow-unauthenticated \
  --service-account STAGING_RUNTIME_SERVICE_ACCOUNT \
  --update-env-vars ENVIRONMENT=staging,APPSTORE_ENVIRONMENT=sandbox,CLOUD_RUN_URL=https://kevin-api-staging-l63rergg7a-uc.a.run.app,FIRESTORE_PROJECT_ID=YOUR_STAGING_PROJECT,FIREBASE_DATABASE_URL=YOUR_STAGING_RTDB_URL,APNS_SANDBOX=false,PRODUCTION_TWILIO_ACCOUNT_SID=YOUR_PROD_TWILIO_ACCOUNT_SID

# Deploy to production after staging has been tested
git checkout main
gcloud run deploy kevin-api \
  --source . \
  --project kevin-491315 \
  --region us-central1 \
  --allow-unauthenticated \
  --service-account PRODUCTION_RUNTIME_SERVICE_ACCOUNT \
  --update-env-vars ENVIRONMENT=production,APPSTORE_ENVIRONMENT=production,CLOUD_RUN_URL=https://kevin-api-752910912062.us-central1.run.app,FIRESTORE_PROJECT_ID=kevin-491315,FIREBASE_DATABASE_URL=https://kevin-491315-rtdb.firebaseio.com,APNS_SANDBOX=false,PRODUCTION_TWILIO_ACCOUNT_SID=YOUR_PROD_TWILIO_ACCOUNT_SID
```
