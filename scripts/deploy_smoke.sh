#!/usr/bin/env bash
# Run scripts/smoke.py against a deployed URL with the dedicated smoke API key from Secret Manager.
# usage: scripts/deploy_smoke.sh <smoke-key-secret-id> <base-url> <json-out> [extra smoke.py args...]
# The key is masked in the GitHub Actions log and only passed to smoke.py through the environment.
set -euo pipefail
secret="${1:?secret id}" url="${2:?base url}" out="${3:?json out}"
shift 3
key="$(gcloud secrets versions access latest --secret "$secret")"
[[ -n "$key" ]] || { echo "smoke key secret ${secret} is empty"; exit 1; }
echo "::add-mask::${key}"
SMOKE_API_KEY="$key" python3 scripts/smoke.py --base-url "$url" --json-out "$out" "$@"
