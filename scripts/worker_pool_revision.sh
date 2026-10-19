#!/usr/bin/env bash
# Print the short name of the worker-pool revision that currently holds the largest share of the instances.
# usage: scripts/worker_pool_revision.sh <project> <region> <worker-pool>
#
# Reads the Cloud Run Admin API v2 WorkerPool resource directly (documented fields, REST reference
# projects.locations.workerPools): `instanceSplitStatuses[].{revision,percent}` is the observed split (the entry
# with the highest percent wins); the fallback is `latestReadyRevision`. Either may be a full resource name, so
# only the last path segment is printed. The jq program is tested on sample payloads in tests/test_hardening.py.
# Prints nothing for a worker pool that has never served (first deploy): there is no rollback target then.
set -euo pipefail
project="${1:?project}" region="${2:?region}" pool="${3:?worker pool}"
token="$(gcloud auth print-access-token)"
curl -fsS -H "Authorization: Bearer ${token}" \
  "https://run.googleapis.com/v2/projects/${project}/locations/${region}/workerPools/${pool}" \
  | jq -r -f "$(dirname "$0")/worker_pool_revision.jq"
