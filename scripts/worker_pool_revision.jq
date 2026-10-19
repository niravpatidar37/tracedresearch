# Rollback target from a Cloud Run Admin API v2 WorkerPool resource (see worker_pool_revision.sh).
([.instanceSplitStatuses[]?] | max_by(.percent) | .revision) // .latestReadyRevision // ""
| split("/") | last // ""
