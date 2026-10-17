#!/usr/bin/env bash
# Smoke test a deployed API: /healthz (liveness) and /readyz (Redis + schema) must both return 200.
# Usage: scripts/smoke_test.sh https://<service-or-tag-url> [attempts]
# Used by .github/workflows/deploy.yml and docs/RUNBOOK.md. Needs only curl.
set -euo pipefail

base="${1:?usage: smoke_test.sh <base-url> [attempts]}"
attempts="${2:-10}"
base="${base%/}"

check() {
  local path="$1" expect="$2" body
  body="$(curl -fsS --max-time 10 "${base}${path}")" || return 1
  [[ "$body" == *"$expect"* ]] || { echo "unexpected ${path} body: ${body:0:200}"; return 1; }
}

for i in $(seq 1 "$attempts"); do
  if check /healthz '"status":"ok"' && check /readyz '"status":"ready"'; then
    echo "smoke OK: ${base} (attempt ${i})"
    exit 0
  fi
  echo "smoke attempt ${i}/${attempts} failed; retrying in 6s"
  sleep 6
done
echo "smoke FAILED: ${base}"
exit 1
