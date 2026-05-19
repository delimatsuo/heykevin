#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-}"
EXPECTED_ENVIRONMENT="${2:-}"
REQUIRE_ADMIN_API="${REQUIRE_ADMIN_API:-false}"

if [[ -z "${BASE_URL}" ]]; then
  echo "Usage: scripts/smoke_release.sh BASE_URL [expected_environment]" >&2
  echo "Set ADMIN_API_TOKEN to exercise authenticated admin API smoke checks." >&2
  exit 2
fi

BASE_URL="${BASE_URL%/}"

tmp_body="$(mktemp)"
cleanup() {
  rm -f "${tmp_body}"
}
trap cleanup EXIT

echo "== Health =="
curl -fsS "${BASE_URL}/health" -o "${tmp_body}"
jq -e '.status == "ok"' "${tmp_body}" >/dev/null
if [[ -n "${EXPECTED_ENVIRONMENT}" ]]; then
  jq -e --arg env "${EXPECTED_ENVIRONMENT}" '.environment == $env' "${tmp_body}" >/dev/null
fi
jq -r '{environment, service, revision, deploy_sha}' "${tmp_body}"

echo
echo "== Static admin =="
curl -fsS "${BASE_URL}/admin" -o "${tmp_body}"
grep -q "Kevin AI Admin" "${tmp_body}"
curl -fsS "${BASE_URL}/static/admin.css" -o /dev/null
curl -fsS "${BASE_URL}/static/admin.js" -o /dev/null
echo "admin static assets ok"

echo
echo "== Authenticated admin API =="
if [[ -z "${ADMIN_API_TOKEN:-}" ]]; then
  if [[ "${REQUIRE_ADMIN_API}" == "true" ]]; then
    echo "ADMIN_API_TOKEN is required when REQUIRE_ADMIN_API=true" >&2
    exit 1
  fi
  echo "skipped: ADMIN_API_TOKEN not set"
else
  curl -fsS \
    -H "Authorization: Bearer ${ADMIN_API_TOKEN}" \
    "${BASE_URL}/api/admin/overview" \
    -o "${tmp_body}"
  jq -e 'has("total_contractors") and has("paid_subscribers")' "${tmp_body}" >/dev/null
  echo "admin overview ok"
fi
