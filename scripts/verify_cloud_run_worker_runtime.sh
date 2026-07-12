#!/usr/bin/env bash
set -euo pipefail

service="${1:?Cloud Run service name is required}"
project="${2:?GCP project ID is required}"
region="${3:?GCP region is required}"

command -v gcloud >/dev/null
command -v jq >/dev/null

runtime_json="$(
  gcloud run services describe "$service" \
    --project "$project" \
    --region "$region" \
    --format='json(metadata.annotations,spec.template.metadata.annotations)'
)"

cpu_throttling="$(
  jq -r \
    '.spec.template.metadata.annotations["run.googleapis.com/cpu-throttling"] // "true"' \
    <<<"$runtime_json"
)"
minimum_instances="$(
  jq -r \
    '.metadata.annotations["run.googleapis.com/minScale"] // .spec.template.metadata.annotations["autoscaling.knative.dev/minScale"] // "0"' \
    <<<"$runtime_json"
)"

if [[ "$cpu_throttling" != "false" ]]; then
  echo "Cloud Run background CPU is required for durable worker loops: $service" >&2
  exit 1
fi

if [[ ! "$minimum_instances" =~ ^[0-9]+$ ]] || (( minimum_instances < 1 )); then
  echo "At least one Cloud Run minimum instance is required for durable worker loops: $service" >&2
  exit 1
fi

echo "Cloud Run worker runtime verified: $service"
