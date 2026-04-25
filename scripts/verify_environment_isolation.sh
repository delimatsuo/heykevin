#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-delimatsuo/heykevin}"
PROJECT_ID="${PROJECT_ID:-kevin-491315}"
REGION="${REGION:-us-central1}"
STAGING_SERVICE="${STAGING_SERVICE:-kevin-api-staging}"
PRODUCTION_SERVICE="${PRODUCTION_SERVICE:-kevin-api}"

required_gh_vars=(
  WIF_PROVIDER
  WIF_STAGING_SERVICE_ACCOUNT
  WIF_PRODUCTION_SERVICE_ACCOUNT
  STAGING_RUNTIME_SERVICE_ACCOUNT
  PRODUCTION_RUNTIME_SERVICE_ACCOUNT
  PRODUCTION_TWILIO_ACCOUNT_SID
)

required_staging_vars=(
  FIRESTORE_PROJECT_ID
  FIREBASE_DATABASE_URL
  APNS_SANDBOX
)

required_cloud_run_env() {
  local service="$1"
  shift
  local expected=("$@")

  echo "Checking Cloud Run env for ${service}"
  local env_names
  env_names="$(gcloud run services describe "${service}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --format=json | jq -r '.spec.template.spec.containers[0].env[].name' || true)"

  if [[ -z "${env_names}" ]]; then
    echo "  ERROR: Could not read ${service}. Check gcloud auth/IAM." >&2
    return 1
  fi

  local missing=0
  for name in "${expected[@]}"; do
    if ! grep -qx "${name}" <<<"${env_names}"; then
      echo "  MISSING: ${name}" >&2
      missing=1
    fi
  done
  return "${missing}"
}

echo "== GitHub auth =="
gh auth status

echo
echo "== Branch sync =="
git fetch origin main staging
git rev-list --left-right --count origin/staging...origin/main

echo
echo "== GitHub environments =="
gh api "repos/${REPO}/environments" --jq '.environments[].name' | sort

echo
echo "== GitHub environment variables =="
for env_name in staging production; do
  echo "Checking ${env_name}"
  vars="$(gh api "repos/${REPO}/environments/${env_name}/variables" --jq '.variables[].name' || true)"
  if [[ -z "${vars}" ]]; then
    echo "  ERROR: Could not read ${env_name} variables. Check gh auth/repo permissions." >&2
    exit 1
  fi
  for name in "${required_gh_vars[@]}"; do
    grep -qx "${name}" <<<"${vars}" || {
      echo "  MISSING: ${name}" >&2
      exit 1
    }
  done
  if [[ "${env_name}" == "staging" ]]; then
    for name in "${required_staging_vars[@]}"; do
      grep -qx "${name}" <<<"${vars}" || {
        echo "  MISSING: ${name}" >&2
        exit 1
      }
    done
  fi
done

echo
echo "== Cloud Run services =="
required_cloud_run_env "${STAGING_SERVICE}" \
  ENVIRONMENT \
  APPSTORE_ENVIRONMENT \
  CLOUD_RUN_URL \
  FIRESTORE_PROJECT_ID \
  FIREBASE_DATABASE_URL \
  APNS_SANDBOX \
  PRODUCTION_TWILIO_ACCOUNT_SID

required_cloud_run_env "${PRODUCTION_SERVICE}" \
  ENVIRONMENT \
  APPSTORE_ENVIRONMENT \
  CLOUD_RUN_URL \
  FIRESTORE_PROJECT_ID \
  FIREBASE_DATABASE_URL \
  APNS_SANDBOX \
  PRODUCTION_TWILIO_ACCOUNT_SID

echo
echo "Environment isolation checks completed."
