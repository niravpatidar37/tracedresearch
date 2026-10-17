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
| 6 | **Grounded answer rate**: completed runs whose report is not the deterministic fallback | `run_finished fallback=` | **>= 80%** | 9/15 = 60% (15 completed, 6 fallbacks), **currently misses the target** (writer JSON failures and rate limits; see README "What did not work") |
| 7 | **Durability**: runs resumed after an instance loss without re-running a committed node | trace `node_key` duplicates (`scripts/show_trace.py`) | **100%** of resumed runs | 2/2 crash tests, 0 duplicate node keys |
| 8 | **Cost per run** | `run_finished usd=` | **p95 <= $0.005** | mean $0.00089, max $0.00152 |

Claim-level faithfulness (is each claim supported by its cited snippet) is **not yet measured**. It needs an NLI or
judge check (Project 4), so it has no SLO yet.

### Alerting (proposed)

- **SLO 1 burn rate:** page at 14.4x over 1 h (2% of the monthly budget gone), ticket at 3x over 6 h.
- **SLO 3:** completion rate below 80% over 1 h -> ticket. Check Groq 429s, the kill switch and the worker count.
- **Queue backlog:** `LLEN ra:queue` > 20 for 10 min -> ticket. The worker pool is fixed-size; raise
  `worker_instances`.
- **Memorystore:** memory usage above 80% -> ticket. `noeviction` makes writes fail when Redis is full.
- **Spend:** a `budget=project_usd` or `global_usd` trip -> notify. The cap did its job; decide whether to raise it.

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
| Worker pool | ~$18 (1 instance) | ~$36 (2 instances) | instance count x hours (always on) |
| Memorystore | $35.77 (Basic 1 GiB x 730 h x $0.049) | >= $35.77 (Standard HA 1 GiB; rate not captured) | tier x GiB x hours |
| LLM (Groq, paid-rate pricing) | ~$0.89 per 1,000 runs (measured mean $0.00089/run) | same per run | runs/month; hard monthly cap: $5 staging / $20 project and $25 global prod |
| Migration job | < $0.01 | < $0.01 | one short execution per deploy |
| Artifact Registry, Secret Manager, Logging, egress | small (not itemised; verify on their pricing pages) | small | image count (20 kept), secret accesses, log volume, about 4 MB of page fetches per run |
| **Total (excl. small items)** | **~$55-58 / month** | **>= ~$82 / month + ~$4 per 1,000 runs** | |

Cost drivers, in order:
1. Memorystore is the largest fixed cost. A BASIC 1 GiB instance holds months of run state at the measured sizes.
2. The worker pool is always on. Fewer instances save money and reduce parallelism. There is no queue-depth autoscaling yet.
3. The API min-instance floor (prod only) is there to avoid cold starts on `POST`.
4. LLM spend is the only cost that grows with each run. It is capped in code per calendar month.

Ways to cut cost: staging can scale the worker pool to 0 between test sessions
(`gcloud run worker-pools update research-staging-worker --region REGION --instances 0`) and use `api_min_instances = 0`.
Committed-use discounts apply to always-on worker and Redis capacity.
