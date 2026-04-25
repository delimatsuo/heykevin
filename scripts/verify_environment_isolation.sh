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
  STAGING_BUILD_SERVICE_ACCOUNT
  PRODUCTION_BUILD_SERVICE_ACCOUNT
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

cloud_run_env_value() {
  local service="$1"
  local name="$2"

  gcloud run services describe "${service}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --format=json \
    | jq -r --arg name "${name}" '.spec.template.spec.containers[0].env[] | select(.name == $name) | .value // empty'
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
  vars="$(gh api "repos/${REPO}/environments/${env_name}/variables?per_page=100" --jq '.variables[].name' || true)"
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
echo "== Cloud Run isolation values =="
staging_firestore_project="$(cloud_run_env_value "${STAGING_SERVICE}" FIRESTORE_PROJECT_ID)"
staging_database_url="$(cloud_run_env_value "${STAGING_SERVICE}" FIREBASE_DATABASE_URL)"
staging_twilio_sid="$(cloud_run_env_value "${STAGING_SERVICE}" TWILIO_ACCOUNT_SID)"
staging_twilio_phone="$(cloud_run_env_value "${STAGING_SERVICE}" TWILIO_PHONE_NUMBER)"
staging_dial_in_number="$(cloud_run_env_value "${STAGING_SERVICE}" DIAL_IN_NUMBER)"
staging_dial_in_numbers="$(cloud_run_env_value "${STAGING_SERVICE}" DIAL_IN_NUMBERS)"
production_twilio_sid="$(cloud_run_env_value "${PRODUCTION_SERVICE}" PRODUCTION_TWILIO_ACCOUNT_SID)"
production_twilio_phone="$(cloud_run_env_value "${PRODUCTION_SERVICE}" TWILIO_PHONE_NUMBER)"

if [[ -z "${staging_firestore_project}" || "${staging_firestore_project}" == "${PROJECT_ID}" ]]; then
  echo "  ERROR: staging FIRESTORE_PROJECT_ID must be set and must not be production" >&2
  exit 1
fi

if [[ -z "${staging_database_url}" || "${staging_database_url}" == *"${PROJECT_ID}-rtdb"* ]]; then
  echo "  ERROR: staging FIREBASE_DATABASE_URL must be set and must not be production" >&2
  exit 1
fi

if [[ -z "${staging_twilio_sid}" || -z "${production_twilio_sid}" ]]; then
  echo "  ERROR: staging and production Twilio Account SIDs must both be configured" >&2
  exit 1
fi

if [[ "${staging_twilio_sid}" == "${production_twilio_sid}" ]]; then
  echo "  ERROR: staging TWILIO_ACCOUNT_SID matches production Twilio" >&2
  exit 1
fi

if [[ -n "${production_twilio_phone}" && "${staging_twilio_phone}" == "${production_twilio_phone}" ]]; then
  echo "  ERROR: staging TWILIO_PHONE_NUMBER matches production Twilio phone number" >&2
  exit 1
fi

if [[ -n "${production_twilio_phone}" && "${staging_dial_in_number}" == "${production_twilio_phone}" ]]; then
  echo "  ERROR: staging DIAL_IN_NUMBER matches production Twilio phone number" >&2
  exit 1
fi

if [[ -n "${production_twilio_phone}" && "${staging_dial_in_numbers}" == *"${production_twilio_phone}"* ]]; then
  echo "  ERROR: staging DIAL_IN_NUMBERS contains production Twilio phone number" >&2
  exit 1
fi

echo
echo "Environment isolation checks completed."
