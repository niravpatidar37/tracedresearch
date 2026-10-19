# SLOs and cost estimate

Status: **deploy-ready; not applied (no cloud account connected).** The targets below are proposals. The
"baseline" column comes from the local benchmark in the README (16 real runs; raw data in
`results/benchmark_pooled/summary.json`), not from a production deployment.

## SLIs and SLOs (30-day rolling window)

| # | SLI | How it is measured | SLO target | Measured baseline (local) |
|---|---|---|---|---|
| 1 | **API availability**: share of `/research*` requests that do not return 5xx (deliberate 503s from the kill switch are excluded) | Cloud Monitoring `run.googleapis.com/request_count` by `response_code_class`, service `research-prod-api` | **99.5%** (about 3.6 h of error budget / month) | not yet measured |
| 2 | **API latency** for `POST /research` and `GET /research/{id}` (SSE excluded) | `run.googleapis.com/request_latencies` | **p50 < 100 ms, p95 < 300 ms, p99 < 1 s** | not yet measured |
| 3 | **Run completion**: accepted runs that reach `completed` | log-based metric on `run_finished ... status=` (JSON logs from the worker) | **>= 90%** | 15/16 = 94% |
| 4 | **Run wall time** (POST to report) | log-based distribution on `run_finished elapsed_s=` plus queue wait | **p50 <= 90 s, p95 <= 180 s, p99 <= 270 s** (270 s is the hard budget ceiling) | p50 64.8 s, p95 116.5 s (pooled benchmark in the README; contended free-tier Groq key) |
| 5 | **Citation validity**: report citations that resolve to a provenance record | `citation_check.rate` in the run result | **100%** (enforced by the validator gate) | 72/72 |
| 6 | **Grounded answer rate**: completed runs whose report is not the deterministic fallback | log-based metric `research-ENV_runs_finished{status="completed", fallback}` from `run_finished ... fallback=... fallback_reason=...` | **>= 80%** | 9/15 = 60% (15 completed, 6 fallbacks), **currently misses the target** (writer JSON failures and rate limits; see README "What did not work") |
| 7 | **Durability**: runs resumed after an instance loss without re-running a committed node | trace `node_key` duplicates (`scripts/show_trace.py`) | **100%** of resumed runs | 2/2 crash tests, 0 duplicate node keys |
| 8 | **Cost per run** | `run_finished usd=` | **p95 <= $0.005** | mean $0.00089, max $0.00152 |

### SLI 6 misses its target: how the system degrades, and how it is watched

The baseline (9/15) is below the 80% target, and this PR does not claim to fix the cause (writer JSON failures and
Groq rate limits). What it does:

- **Honest degradation.** When the writer can't produce a valid report, the run still completes, but with the
  deterministic fallback report: title `Partial findings (degraded report)`, a summary that says the writer was not
  used and why, and only the extracted findings, each cited to its source (citation validity stays 100%). The API
  returns `report.degraded: true` and a machine-readable `report.fallback_reason` (`writer_output_invalid`,
  `writer_model_unavailable`, `no_findings`, `<kind>_budget`, ...). Nothing un-grounded is added.
- **Measured.** Every finished run logs `run_finished ... fallback=<bool> fallback_reason=<code>`; the log-based
  counter `research-ENV_runs_finished` carries `status`, `fallback` and `fallback_reason` labels.
- **Alerted.** `degraded-report rate above the grounded-answer SLO`: fallback / completed > 20% over 1 h
  (`alert_fallback_rate`). See [RUNBOOK Alerts](RUNBOOK.md#alerts).

Claim-level faithfulness (is each claim supported by its cited snippet) is **not yet measured**. It needs an NLI or
judge check (Project 4), so it has no SLO yet.

### Alerting (monitoring as code, not applied)

All alerts are Terraform resources in
[`monitoring.tf`](../infra/terraform/modules/research_assistant/monitoring.tf); the table with conditions and first
actions is in [RUNBOOK Alerts](RUNBOOK.md#alerts). Summary:

- **SLO 1:** a `google_monitoring_slo` (request-based, bad = 5xx except 503) with multi-window burn-rate alerts:
  page at > 14.4x over 1 h AND 5 min, ticket at > 6x over 6 h AND 30 min. Plus an uptime check on `/healthz` and a
  5xx-ratio alert (> 2% for 5 min).
- **SLO 2:** p95 of non-SSE requests (log-based distribution from the request logs, `/events` excluded) > 0.3 s for
  10 min.
- **SLO 6:** degraded-report rate > 20% over 1 h (above).
- **Saturation:** API CPU p99 > 85%, API at max instances, worker CPU / memory p99 > 90%.
- **Queue:** depth > 20 or oldest queued run > 300 s for 10 min, or no `queue_stats` from the scaler for 10 min.
- **Memorystore:** memory usage above 80% (`noeviction` makes writes fail when Redis is full).
- **Scaler:** more than 3 failed scaler executions in 15 min.

Not alerted yet: SLO 3 (completion rate) and spend-cap trips are visible in `research-ENV_runs_finished{status}` and
the `run_finished budget=` field but have no policy.

### Accepted limits

- **Kill switch propagation: up to ~2 s.** The Redis flag is cached per instance for `KillSwitch.CACHE_S = 2`
  seconds. Accepted (rationale in [RUNBOOK Kill switch](RUNBOOK.md#kill-switch-stop-all-llm-and-tool-calls)); the
  smoke test measures it.
- **Scaler reaction time: about 1 minute** (Cloud Scheduler's finest schedule) plus instance start-up.

## Monthly cost estimate (ESTIMATE, us-central1, list prices, USD)

> This is an estimate, not a bill. Prices were read from the GCP pricing pages on 2026-10-17:
> [Cloud Run pricing](https://cloud.google.com/run/pricing),
> [Memorystore for Redis pricing](https://cloud.google.com/memorystore/docs/redis/pricing). Verify with the
> [pricing calculator](https://cloud.google.com/products/calculator) before relying on it.
> A month is taken as 730 h = 2,628,000 s.

Unit prices used:
- Cloud Run service, request-based billing: $0.000024 per vCPU-second and $0.0000025 per GiB-second while a request
  is active. Idle min-instances cost $0.0000025 per vCPU-second and $0.0000025 per GiB-second. Requests cost $0.40 per million.
- Cloud Run worker pool: the pricing page's own example is "1 vCPU / 512 MiB, one instance for a full month:
  **$16.83** without free tier ($11.61 with it)". The worker uses 1 GiB, so this estimate uses about **$18 per worker
  instance-month**. The per-second worker-pool rates on the page come in several columns (list price and commitment
  tiers), so this estimate uses the page's worked example rather than reading a column.
- Memorystore Basic, M1 (1-4 GiB): **$0.049 per GiB-hour**. The Standard (HA) tier rate was not captured for this
  estimate. It is higher than Basic, so the prod Redis line below is a **lower bound**.

| Line item | Staging | Prod | Driver |
|---|---|---|---|
| API service | ~$0-3 (scale to zero; 1,000 runs x ~120 s of SSE streaming x (1 vCPU + 0.5 GiB) = $3.03 before free tier) | ~$10 idle floor (min 1 instance: 2.628 M s x $0.00000375) + ~$3 per 1,000 runs | min instances, SSE stream time, request count |
| Worker pool | ~$0-18 (scaler min 0, max 2: billed only while instances run) | ~$18-108 (min 1, max 6; ~$18 per instance-month while running) | instance-hours chosen by the queue-depth scaler |
| Scaler (Cloud Scheduler + job) | small (not itemised): one scheduler job + ~43,800 executions of a few seconds per month | same | one execution per minute; verify on the Cloud Scheduler and Cloud Run jobs pricing pages |
| Memorystore | $35.77 (Basic 1 GiB x 730 h x $0.049) | >= $35.77 (Standard HA 1 GiB; rate not captured) | tier x GiB x hours |
| LLM (Groq, paid-rate pricing) | ~$0.89 per 1,000 runs (measured mean $0.00089/run) | same per run | runs/month; hard monthly cap: $5 staging / $20 project and $25 global prod |
| Migration job | < $0.01 | < $0.01 | one short execution per deploy |
| Artifact Registry, Secret Manager, Logging, egress | small (not itemised; verify on their pricing pages) | small | image count (20 kept), secret accesses, log volume, about 4 MB of page fetches per run |
| **Total (excl. small items)** | **~$36-57 / month** (worker 0-1 instance on average) | **>= ~$64 / month** with one warm worker, up to ~$154 with six busy all month, **+ ~$4 per 1,000 runs** | |

Cost drivers, in order:
1. Memorystore is the largest fixed cost. A BASIC 1 GiB instance holds months of run state at the measured sizes.
2. Worker instance-hours. The queue-depth scaler keeps the pool between `worker_min_instances` and
   `worker_max_instances`; prod keeps one warm worker, staging idles at zero.
3. The API min-instance floor (prod only) is there to avoid cold starts on `POST`.
4. LLM spend is the only cost that grows with each run. It is capped in code per calendar month.

Ways to cut cost: staging already idles at zero workers (`worker_min_instances = 0`) and uses
`api_min_instances = 0`. Pausing the scaler (`gcloud scheduler jobs pause research-staging-scaler --location REGION`)
freezes the pool at its current size.
Committed-use discounts apply to always-on worker and Redis capacity.
