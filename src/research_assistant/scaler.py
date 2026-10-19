"""Queue-depth scaler for the Cloud Run worker pool (run every minute by Cloud Scheduler as a Cloud Run job).

Cloud Run worker pools scale MANUALLY (a fixed instance count) unless an external autoscaler sets the count.
This job is that autoscaler, kept deliberately small:

1. read queue statistics from Redis (queue depth, unfinished runs, age of the oldest queued run),
2. log one `queue_stats ...` line (the source of the queue-depth / oldest-job-age log-based metrics and alerts),
3. compute the desired instance count  ceil(unfinished runs / runs per instance)  clamped to [min, max],
   scaling in by at most one instance per invocation (scale-in drains runs via SIGTERM, see RUNBOOK),
4. PATCH the worker pool's `scaling.manualInstanceCount` through the Cloud Run Admin API v2
   (projects.locations.workerPools.patch with updateMask), only when it differs.

While the kill switch is engaged it only reports; it never scales up a paused system.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from research_assistant.schemas import utcnow
from research_assistant.store import RunStore

log = logging.getLogger(__name__)

RUN_API = "https://run.googleapis.com/v2"
METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"  # noqa: S105 - a URL, not a secret
MAX_SCAN = 500  # cap on unfinished runs inspected per invocation


@dataclass(frozen=True)
class QueueStats:
    queue_depth: int  # run ids waiting in ra:queue
    active_runs: int  # unfinished runs (queued + running)
    queued_runs: int  # unfinished runs whose status is queued
    oldest_queued_age_s: float  # 0 when nothing is queued

    def log_line(self) -> str:
        return (
            f"queue_stats queue_depth={self.queue_depth} active_runs={self.active_runs} "
            f"queued_runs={self.queued_runs} oldest_queued_age_s={self.oldest_queued_age_s:.0f}"
        )


def queue_stats(store: RunStore) -> QueueStats:
    now = utcnow()
    active = store.active_runs()
    queued = 0
    oldest = 0.0
    for rid in active[:MAX_SCAN]:
        st = store.load(rid)
        if st is None or st.status != "queued":
            continue
        queued += 1
        oldest = max(oldest, (now - st.updated_at).total_seconds())
    return QueueStats(
        queue_depth=store.queue_depth(), active_runs=len(active), queued_runs=queued, oldest_queued_age_s=oldest
    )


def desired_instances(stats: QueueStats, *, runs_per_instance: int, current: int, min_n: int, max_n: int) -> int:
    if min_n < 0 or max_n < min_n or runs_per_instance < 1:
        raise ValueError("need 0 <= min <= max and runs_per_instance >= 1")
    work = max(stats.active_runs, stats.queue_depth)
    target = min(max(math.ceil(work / runs_per_instance), min_n), max_n)
    if target < current:
        target = max(target, current - 1)  # scale in gently: one instance per invocation
    return target


def metadata_token(http: httpx.Client) -> str:
    """Access token of the job's service account from the Cloud Run metadata server."""
    r = http.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=5.0)
    r.raise_for_status()
    return str(r.json()["access_token"])


class WorkerPoolScaler:
    """Reads and sets `scaling.manualInstanceCount` of one worker pool via the Cloud Run Admin API v2."""

    def __init__(self, pool_name: str, http: httpx.Client, token: Callable[[], str]) -> None:
        if not pool_name.startswith("projects/") or "/workerPools/" not in pool_name:
            raise ValueError("pool_name must be projects/P/locations/L/workerPools/NAME")
        self.pool_name = pool_name
        self.http = http
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def current(self) -> int:
        r = self.http.get(f"{RUN_API}/{self.pool_name}", headers=self._headers(), timeout=10.0)
        r.raise_for_status()
        scaling: dict[str, Any] = r.json().get("scaling") or {}
        return int(scaling.get("manualInstanceCount", 0))  # proto3 JSON omits zero values

    def set(self, n: int) -> None:
        r = self.http.patch(
            f"{RUN_API}/{self.pool_name}",
            params={"updateMask": "scaling.manualInstanceCount"},
            json={"scaling": {"manualInstanceCount": n}},
            headers=self._headers(),
            timeout=30.0,
        )
        r.raise_for_status()


def run_once(
    store: RunStore,
    scaler: WorkerPoolScaler | None,
    *,
    runs_per_instance: int,
    min_n: int,
    max_n: int,
    kill_switch: bool,
    dry_run: bool = False,
) -> dict[str, Any]:
    stats = queue_stats(store)
    log.info(stats.log_line())
    out: dict[str, Any] = asdict(stats) | {"action": "none"}
    if scaler is None:
        return out
    current = scaler.current()
    want = desired_instances(stats, runs_per_instance=runs_per_instance, current=current, min_n=min_n, max_n=max_n)
    if kill_switch and want > current:
        want = current  # paused system: never add capacity
    out |= {"current_instances": current, "desired_instances": want}
    if want != current:
        out["action"] = "dry_run" if dry_run else "scaled"
        if not dry_run:
            scaler.set(want)
        log.info("worker_pool_scaled from=%d to=%d dry_run=%s", current, want, dry_run)
    return out
