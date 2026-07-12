#!/usr/bin/env bash
set -euo pipefail

service="${1:?Cloud Run service name is required}"
project="${2:?GCP project ID is required}"
region="${3:?GCP region is required}"
environment="${4:?Deployment environment is required}"
expected_sha="${5:?Expected deploy SHA is required}"

fail() {
  printf 'cloud_run_deployment status=failed reason=%s\n' "$1" >&2
  exit 1
}

[[ "$project" == "kevin-491315" ]] || fail invalid_project
[[ "$region" == "us-central1" ]] || fail invalid_region
[[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]] || fail invalid_sha
case "$environment" in
  staging)
    [[ "$service" == "kevin-api-staging" ]] || fail invalid_service
    revision_pattern='^kevin-api-staging-[0-9]{5}-[a-z0-9]{3}$'
    ;;
  production)
    [[ "$service" == "kevin-api" ]] || fail invalid_service
    revision_pattern='^kevin-api-[0-9]{5}-[a-z0-9]{3}$'
    ;;
  *)
    fail invalid_environment
    ;;
esac

command -v curl >/dev/null || fail missing_curl
command -v gcloud >/dev/null || fail missing_gcloud
command -v jq >/dev/null || fail missing_jq

service_json="$(
  gcloud run services describe "$service" \
    --project "$project" \
    --region "$region" \
    --format=json
)" || fail service_read
latest_created="$(jq -er '.status.latestCreatedRevisionName' <<<"$service_json")" \
  || fail invalid_service_state
latest_ready="$(jq -er '.status.latestReadyRevisionName' <<<"$service_json")" \
  || fail invalid_service_state
[[ "$latest_created" == "$latest_ready" ]] || fail latest_not_ready
[[ "$latest_ready" =~ $revision_pattern ]] || fail invalid_revision

jq -e --arg revision "$latest_ready" '
  ([.status.traffic[]?
    | select(.revisionName == $revision)
    | (.percent // 0)]
    | add // 0) == 100
' <<<"$service_json" >/dev/null || fail stale_traffic
service_url="$(jq -er '.status.url' <<<"$service_json")" || fail invalid_service_state
[[ "$service_url" =~ ^https://[a-z0-9.-]+\.run\.app$ ]] || fail invalid_service_url

verified=false
for _ in $(seq 1 12); do
  if health="$(
      curl --fail --silent --show-error \
        --connect-timeout 5 --max-time 20 "$service_url/health"
    )" \
    && jq -e \
      --arg environment "$environment" \
      --arg service "$service" \
      --arg revision "$latest_ready" \
      --arg sha "$expected_sha" '
        .status == "ok"
        and .environment == $environment
        and .service == $service
        and .revision == $revision
        and .deploy_sha == $sha
      ' <<<"$health" >/dev/null; then
    verified=true
    break
  fi
  sleep 5
done

if [[ "$verified" != "true" ]]; then
  fail identity_health
fi

printf 'cloud_run_deployment status=verified environment=%s service=%s revision=%s deploy_sha=%s\n' \
  "$environment" "$service" "$latest_ready" "$expected_sha"
