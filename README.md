# Multi-Agent Research Assistant

A LangGraph supervisor with planner, researcher and writer specialists that turns a question into a cited web-research report. Over **16 real runs, 15 finished under 5 minutes (wall time p50 65 s, p95 117 s) at $0.0009 per run. All 72 report citations resolved to real provenance records, and a hard-killed server resumed the run from its last committed step without replaying any node.**

Durable state in Redis, budgets enforced in graph code, SSRF-guarded fetching, validated citations, and a per-step trace for every run.

## Results (real runs, raw data committed)

Eight real research questions ([`results/questions.json`](results/questions.json)) go through the HTTP API (`POST /research`, then the SSE stream, then `GET /research/{id}` and `/trace`). The stack is Groq `gpt-oss-120b`/`gpt-oss-20b`, DuckDuckGo search (`ddgs`), live page fetching, and Redis 7.

| Metric | v2: failover (8 runs) | v3: fresh LLM cache (8 runs) | **Pooled (16)** |
|---|---|---|---|
| Runs completed | 8/8 | 7/8 (1 planner failed: both models rate-limited) | **15/16** |
| Wall time p50 / p95 (POST to report) | 46.1 / 103.0 s | 94.0 / 122.8 s | **64.8 / 116.5 s** |
| Runs under 5 min | 8/8 | 7/7 | **15/15 completed** |
| Tokens per run, mean (p95) | 9,192 (12,285) | 8,406 (13,375) | **8,825 (12,988)** |
| USD per run, mean (max) | $0.00081 ($0.00133) | $0.00098 ($0.00152) | **$0.00089 ($0.00152)** |
| Searches per run | 5.5 | 5.0 | **5.3** |
| Page fetches per run | 19.1 | 20.0 | 19.5 |
| Findings (claim + url + verbatim snippet + timestamp) per run | 3.75 | 4.0 | 3.9 |
| Citation validity, writer's first draft | 35/35 | 24/24 | **59/59 (100%)** |
| Citation validity, final report (validator gate) | 38/38 | 34/34 | **72/72 (100%)** |
| Sub-questions the reviewer accepted | 14/25 | 13/21 | 27/46 |
| Reviewer send-backs (max 1 per sub-question) | 11 | 10 | 21 |
| Deterministic fallback reports (writer not run or failed) | 3 | 3 | 6 |
| 429 failovers from 120b to 20b | 9 | 17 | 26 |

Raw data:
- Per-run data: [`results/benchmark_v2_failover/`](results/benchmark_v2_failover), [`results/benchmark_v3_json_retry/`](results/benchmark_v3_json_retry), and the pooled summary in [`results/benchmark_pooled/summary.json`](results/benchmark_pooled/summary.json). Each `runNN_<id>/` folder has `report.md`, `run.json` and `trace.json`.
- v2 reused 10 of 63 LLM calls from the on-disk cache, which costs $0 and makes those calls fast. v3 ran with an empty cache, so every call in it is real.
- An earlier v1 (4 runs, [`results/benchmark_v1_no_failover/`](results/benchmark_v1_no_failover)) had no model failover. It reached p50 174 s and p95 248 s, and 2 of 4 runs hit the wall-clock ceiling because `gpt-oss-120b` was rate-limited. That result is why the failover exists.

### Crash and resume (real process kill)

[`scripts/live_eval.py crash`](scripts/live_eval.py) does the following:
1. Starts the API and submits a question.
2. Waits until steps are committed, then hard-kills the server (`TerminateProcess`, so no graceful shutdown) and verifies port 8102 is free.
3. Starts a fresh server. Its resume loop picks up the orphaned run once the dead worker's 10 s lease expires.

| Run | Steps committed before kill | Steps after restart | Duplicate node keys | Planner executions | Worker PIDs in trace | Outcome |
|---|---|---|---|---|---|---|
| Groq [`results/crash_resume_groq/`](results/crash_resume_groq) | 6 | 8 | **0** | 1 | 2 (before / after) | completed. Resumed 12 s after the kill, at step 6 |
| Ollama llama3.1:8b [`results/crash_resume_ollama/`](results/crash_resume_ollama) | 4 (killed between researcher and reviewer) | 10 | **0** | 1 | 2 | completed, 10 findings, 11/11 citations valid |

The evidence logs are `crash_resume.log`, `server1.log` and `server2.log`. The last one contains `run … resumed at step 6 (pid …); completed nodes: [...]`.

Caveat on the Groq run: by then the shared free-tier key was close to its limits. All of the resumed researcher steps hit `rate limited for 542-669 s past deadline`, so that run finished with 0 findings and a fallback report. Durability still holds. The Ollama run shows a resumed run that does real research. Its 8B writer produced invalid JSON twice, which also led to a fallback report.

### Tests and security checks
- **127 pytest tests**, run in CI with no secrets. Every LLM call in them uses recorded/fake responses. Each specialist is tested alone, every budget is tested to trip, and there are tests for the SSRF guard, the citation validator, injection handling, crash/resume, and API auth, rate limiting and ownership.
- **Mutation check**: [`results/mutation_check.txt`](results/mutation_check.txt). Each security or budget control is disabled one at a time and the suite must fail for every mutant. The first round left 2 survivors (an IP check shadowed by the deny list, and the undefined-footnote check). Both were fixed with new tests. Current result: **21/21 mutants killed**.
- CI runs ruff, mypy (strict), pytest, bandit, pip-audit, gitleaks, a Docker build, and a Trivy image scan.

## Architecture

```mermaid
flowchart LR
  C[Client] -->|POST /research| API["FastAPI 127.0.0.1:8102<br/>X-API-Key auth + per-key rate limit"]
  API -->|202 run_id| C
  API --> WP["worker pool + orphan-resume loop<br/>(Redis lease per run)"]
  WP --> SUP
  subgraph G["LangGraph StateGraph (state = RunState, Pydantic)"]
    SUP{"supervisor<br/>pure routing + budgets<br/>(no LLM)"} -->|no plan| PL["planner<br/>gpt-oss-120b"]
    PL --> SUP
    SUP -->|pending sub-question| RS["researcher<br/>code: search, dedupe, domain cap, fetch<br/>LLM gpt-oss-20b: extract findings (no tools)"]
    RS --> RV["reviewer<br/>gpt-oss-20b or deterministic on failure status"]
    RV --> SUP
    SUP -->|done / budget hit| WR["writer<br/>gpt-oss-120b, no tools"]
  end
  RS -->|query| SE[("ddgs (default) / Tavily (if key)")]
  RS -->|"SSRF guard: DNS pin, public IPs only,<br/>redirect re-check, size/type caps"| WEB[(web pages)]
  WR --> CV["citation validator +<br/>sanitized markdown renderer"]
  G -->|"after EVERY node (atomic MULTI):<br/>state, node snapshot, trace step, event"| RD[("Redis 7 :16379")]
  API -->|"GET status/report, SSE events, trace"| RD
  G -.->|every paid call| LED[("shared spend ledger<br/>$3 project / $25 global cap")]
```

## Design decisions (and what they cost)

1. **A small Redis state store instead of the official LangGraph Redis checkpointer.**
   - I checked first: `langgraph-checkpoint-redis` 0.5.2 fails on plain `redis:7` with `unknown command 'FT.INFO'` because it needs RediSearch/RedisJSON ([`results/official_redis_checkpointer_probe.txt`](results/official_redis_checkpointer_probe.txt)). Implementing `BaseCheckpointSaver` myself would have meant reproducing langgraph-checkpoint 4.x internals such as delta-channel history.
   - Instead, every node is wrapped. The wrapper writes the domain `RunState`, a node snapshot (for idempotent re-entry), the trace step and a progress event in one Redis `MULTI`.
   - Supervisor routing is a pure function of that state, so resuming means re-invoking the graph with the stored state.
   - *Gave up:* LangGraph time-travel and sub-node granularity. A node that is in flight when the process dies is re-run. The LLM disk cache keeps that cheap.
2. **Tools run in code, and the models only read text.**
   - Search and fetch are deterministic code under budgets. The extraction model (20b) sees page text but has no tools, returns a strict schema, and each snippet must be found verbatim on the fetched page or the finding is dropped. The writer has no tools either.
   - *Gave up:* agentic flexibility. The researcher cannot decide to follow a link or open a PDF.
   - *In return:* an injected page cannot trigger an action.
3. **Model split: 120b for planning and writing, 20b for per-page extraction and review.**
   - Extraction and review are high-volume, bounded tasks, and the 20b costs half as much ($0.075/$0.30 vs $0.15/$0.60 per million tokens at Groq's paid rates).
   - Decomposition and long-form synthesis with citation discipline are where the bigger model should matter. I did not run an A/B on quality, so this is a judgement, not a measurement.
   - When the 120b returns a 429 asking to wait more than 20 s, planner and writer fail over to the 20b for that node. That happened 26 times in 16 runs. Without failover, v1 had p95 248 s over 4 runs and 2 wall-clock trips. With failover the pooled p95 is 117 s over 16 runs. This is not a controlled A/B, because key contention varied between rounds.
4. **The wall-clock ceiling counts active compute time stored in state, not time since creation.**
   - A redeploy that takes 10 minutes should not kill a run that has used 40 s.
   - Of the budget, 45 s is reserved for the writer. After that point research stops and the writer runs on whatever findings exist.

### Budgets (enforced in graph code; each has a test that trips it)
| Budget | Default | Enforced in |
|---|---|---|
| max sub-questions | 4 | `planner.build_plan` truncates |
| max searches per sub-question | 2 | `researcher.research` |
| max total tokens (writer reserve 9k) | 40,000 | `AgentContext._guard` before every LLM call (worst case = prompt estimate + max_tokens) |
| wall-clock ceiling (active) | 270 s | `supervisor.route` + per-node deadline passed down to LLM retries |
| reviewer send-backs | exactly 1 per sub-question | `supervisor.route` |
| per-run USD | $0.10 | `AgentContext._guard` |
| shared ledger | $3 project / $25 global | `SpendLedger.check` before every paid call, file-locked JSONL |

## What did not work

- **Official Redis checkpointer on plain Redis 7.** It needs Redis Stack modules (see probe above).
- **Strict JSON-schema mode is not consistent across the two models.**
  - `gpt-oss-120b` returned HTTP 200 with JSON that left out a required key (`title`).
  - `gpt-oss-20b` was rejected server-side with `json_validate_failed`.
  - Fixes: Pydantic defaults on the non-critical fields, plus one retry in plain JSON mode. In the pooled runs the writer still failed validation in 3 runs and was rate-limited out in 1, and 2 runs had no findings at all. Those 6 runs got the deterministic fallback report, with valid citations whenever there were findings.
- **The shared free-tier Groq key was the bottleneck, not the graph.**
  - The limit is 8k tokens per minute per model plus daily caps, shared with other builds running at the same time.
  - `retry-after` values went up to 669 s. One v3 run failed at the planner because both models were limited.
  - Wall times above are therefore an upper bound under contention.
- **Bot walls and empty pages.**
  - 85 of 293 fetches were `blocked`, mostly HTTP 403 from publishers (Lancet, NEJM, JAMA, ScienceDirect).
  - 23 were `empty`: PubMed/PMC served no extractable text to a non-browser client.
  - The biomedical question (semaglutide) got 0 findings in v2 and 2 in v3.
  - 17 of 79 DuckDuckGo searches returned "No results found" for reasonable queries, which looks like soft rate limiting.
- **A valid citation is not the same as a supported claim.**
  - In the EU AI Act run (v1), the writer said high-risk obligations apply from 2 August 2026 and cited a page whose snippet says those rules "apply from 2 December 2027". The validator passes it, because it only checks that each citation resolves to a real Finding record.
  - Claim-level faithfulness is **not yet measured**. It needs an NLI/judge check, which is a good fit for Project 4.
- **Reviewer standards.** Only 27 of 46 sub-questions were judged fully answered. The rest are listed under "Limitations" in each report rather than dropped.
- **Llama 3.1 8B (Ollama) as writer** produced invalid JSON twice in a row. The 8B is fine for planning and extraction, but not for the nested report schema.

## Security

**Threat model.**
- The main threat is *indirect prompt injection through fetched web pages*. Project 6 will attack this service that way (see [`docs/REDTEAM.md`](docs/REDTEAM.md)).
- Other threats:
  - SSRF through attacker-influenced search results and redirects
  - cost exhaustion
  - cross-tenant access to runs
  - XSS or phishing links in the rendered report

**Controls in code.**
- Fetcher ([`fetch/ssrf.py`](src/research_assistant/fetch/ssrf.py), [`fetch/fetcher.py`](src/research_assistant/fetch/fetcher.py)):
  - only http/https on ports 80/443, and no userinfo
  - DNS is resolved and *every* answer must be a public IP. Private, loopback, link-local, CGNAT, metadata (169.254.169.254, fd00:ec2::254), NAT64/6to4/IPv4-mapped and documentation ranges are blocked.
  - the connection goes to the validated IP (DNS pinning, with SNI and Host preserved)
  - redirects are followed manually (max 3) and re-validated on every hop
  - 2 MB size cap (both declared length and streamed bytes), 10/15 s timeouts, content-type allowlist, `trust_env=False`
  - every failure becomes a status (`blocked`, `timeout`, `paywalled`, `rate_limited`, `unsupported`, `too_large`, `empty`), never an exception
- Untrusted text handling:
  - page text sits inside `<<<PAGE …>>>` data blocks, and any delimiter strings found in the text are neutralised first
  - control and bidi characters are stripped
  - no model that reads page text can call a tool
  - each extracted snippet must appear verbatim on the page
- Report output ([`report.py`](src/research_assistant/report.py)):
  - all model text is escaped. HTML becomes entities, markdown link, image and heading syntax is escaped, and URLs are removed.
  - the only links in a report are footnotes rendered by code from Finding records
  - every `[^n]` marker is checked against a rendered source backed by real findings before the report is returned. A paragraph without a valid citation is dropped.
- API ([`api.py`](src/research_assistant/api.py)):
  - API keys of at least 16 characters, compared in constant time. With no keys configured the API fails closed with 503.
  - fixed-window rate limit per key in Redis (POST and GET counted separately)
  - runs are visible only to the creating key (404 otherwise)
  - run IDs are regex-validated, question length is limited to 10-500 characters, and logs redact PII and secrets
- Supply chain:
  - `uv.lock`, pip-audit, GitHub Actions pinned by full SHA with `permissions: contents: read`, Docker images pinned by digest
  - non-root multi-stage image with a healthcheck, scanned by Trivy

**Residual risk.**
- An injected page can still shape the *claims* extracted from that page and how the writer words them. Citations stay real, but the content can be wrong or slanted. The per-domain cap (2) limits how much one site can dominate a report.
- Faithfulness is not verified (see above).
- DNS pinning relies on the TLS SNI and hostname check in httpx/httpcore.
- The spend-cap check and the ledger record are not atomic across processes. A concurrent call can overshoot by at most one call's worst-case cost.
- There is no robots.txt handling.

**OWASP LLM Top 10 (2025) mapping**

| Risk | Status |
|---|---|
| LLM01 Prompt injection | Data delimiting, no tools for models that read page text, verbatim-snippet provenance, citation gate, URL stripping. Residual: claim slanting |
| LLM02 Sensitive info disclosure | No secrets in prompts; secrets only from the env file; PII/secret log redaction; runs scoped per API key |
| LLM03 Supply chain | Lockfile, pip-audit, SHA-pinned actions, digest-pinned images, Trivy, gitleaks |
| LLM04 Data/model poisoning | No training. Poisoned web content is handled under LLM01; per-domain cap |
| LLM05 Improper output handling | Pydantic-validated outputs, escaped markdown, code-rendered links only |
| LLM06 Excessive agency | No model has tools; search and fetch are budgeted code paths |
| LLM07 System prompt leakage | Prompts hold no secrets; leaking one has no impact |
| LLM08 Vector/embedding weaknesses | N/A (no vector store) |
| LLM09 Misinformation | Provenance-checked findings and validated citations. Residual: faithfulness not measured |
| LLM10 Unbounded consumption | Graph budgets (tokens, wall clock, searches, sub-questions, send-backs, run USD), shared ledger cap, per-key rate limit, input size limits |

## Cost

The real total spend for this project, read from the shared ledger (`project = p2-research`, Groq paid rates), is **$0.0214 over 140 paid Groq calls (as of the final commit)**. That covers development smoke runs, all three benchmark rounds, and the Groq crash test. Ollama calls cost $0. Prices live in [`src/research_assistant/pricing.json`](src/research_assistant/pricing.json) (an estimate; verify against the provider pricing page).

## Resume line

> Built a multi-agent research system with supervisor and worker topology, durable run state in Redis, per-run cost caps, and step-level tracing for debugging. 15 of 16 real research runs finished in under 5 minutes (p95 117 s) at $0.0009 per run, all 72 report citations were validated against provenance records, and a hard-killed process resumed from its last committed step with zero replayed nodes.

## Demo video

TODO (human): record a 90 s demo. Shot list:
1. `POST /research` returns a run_id immediately.
2. The SSE stream shows planner, researcher and reviewer events live.
3. Kill the server mid-run, restart it, and show the `resumed at step N` log line.
4. Open `GET /research/{id}/trace` and step through one researcher entry (tool calls, statuses, tokens).
5. Show the final report with footnote citations, and the citation check (`rate: 1.0`).

## Deviations from the guide
- **Claude API replaced by Groq `gpt-oss`.** This was the owner's decision. A Claude adapter exists, but it is dormant unless `ANTHROPIC_API_KEY` is set, it stays behind the same spend cap, and its model IDs are placeholders to be verified via `GET /v1/models`.
- **Tavily replaced by DuckDuckGo (`ddgs`) by default.** The Tavily adapter is auto-selected when `TAVILY_API_KEY` is set. It is unit-tested with a mock transport but has not been run live, because no key is available.
- **Own Redis state store instead of the official checkpointer** (reason above).
- Local docker compose only; cloud IaC not yet built.

## Setup and run

Requires Python 3.12 via uv, plus Docker. Secrets come from `F:/.secrets/portfolio.env`, or set `PORTFOLIO_ENV_FILE`. The variable names are in [`.env.example`](.env.example).

```bash
uv venv --python 3.12 .venv && uv sync --frozen --extra anthropic
docker compose -p p2-research up -d redis                      # redis:7.4.7 on 127.0.0.1:16379
export RESEARCH_API_KEYS="local-$(openssl rand -hex 16)"       # >= 16 chars
uv run uvicorn research_assistant.api:app_factory --factory --host 127.0.0.1 --port 8102

curl -s -X POST localhost:8102/research -H "X-API-Key: $RESEARCH_API_KEYS" -H 'content-type: application/json' \
     -d '{"question":"How do sodium-ion batteries compare with lithium-ion on cost?"}'
curl -N localhost:8102/research/<run_id>/events -H "X-API-Key: $RESEARCH_API_KEYS"   # SSE progress
curl -s localhost:8102/research/<run_id>        -H "X-API-Key: $RESEARCH_API_KEYS"   # status + report
curl -s localhost:8102/research/<run_id>/trace  -H "X-API-Key: $RESEARCH_API_KEYS"   # per-step log
```

Other entry points:
- CLI without the API: `uv run research-assistant run "question"`, then `resume <run_id>` or `show <run_id>`.
- Container: `RESEARCH_API_KEYS=... docker compose -p p2-research --profile api up -d --build`.
- Offline/red-team mode: set `SEARCH_FIXTURE_FILE=fixtures/redteam_corpus.example.json` (see [`docs/REDTEAM.md`](docs/REDTEAM.md)).
- Local model: `LLM_PROVIDER=ollama` (llama3.1:8b, $0).
- Tests: `uv run pytest -q`. Live re-measurement: `python scripts/live_eval.py bench` or `python scripts/live_eval.py crash`.
- Stop: `docker compose -p p2-research stop`.
