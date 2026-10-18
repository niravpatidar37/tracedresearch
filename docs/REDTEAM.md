# Red-teaming this service (Project 6 target)

Scope: this service is owned by the same author and runs locally only. Point the Project 6 harness at it,
never at third-party systems.

## Start the target

```bash
cd tracedresearch   # your clone
docker compose -p p2-research up -d redis                       # redis:7 on 127.0.0.1:16379
export RESEARCH_API_KEYS="redteam-$(openssl rand -hex 16)"     # >= 16 chars, else the API fails closed (503)
# Option A - live web (DuckDuckGo + real fetches):
.venv/Scripts/python.exe -m uvicorn research_assistant.api:app_factory --factory --host 127.0.0.1 --port 8102
# Option B - poisoned offline corpus (deterministic, no SSRF relaxation needed):
SEARCH_FIXTURE_FILE=fixtures/redteam_corpus.example.json \
  .venv/Scripts/python.exe -m uvicorn research_assistant.api:app_factory --factory --host 127.0.0.1 --port 8102
```

Or the container: `RESEARCH_API_KEYS=... docker compose -p p2-research --profile api up -d --build`.

Stop: `docker compose -p p2-research stop` and stop uvicorn (Ctrl-C, or `Stop-Process -Id <pid>`).

## Target adapter contract

| Step | Call |
|---|---|
| submit | `POST /research` `{"question": "..."}` with header `X-API-Key` -> `202 {"run_id"}` |
| wait | `GET /research/{id}/events` (SSE, ends with `event: completed`/`failed`) or poll `GET /research/{id}` |
| response | `GET /research/{id}` -> `report.markdown`, `report.citations`, `findings[]` (claim/url/snippet/retrieved_at) |
| metadata | `GET /research/{id}/trace` -> per-step agent, input, output, tool calls, LLM calls, tokens, latency |

Rate limit: `RATE_LIMIT_PER_MIN` POSTs per key per minute (default 10), 240 reads/min. A 429 carries `Retry-After`.

## Attack surfaces worth testing

1. **Indirect prompt injection via web content** (primary threat). Plant instructions in a fixture page (Option B)
   or a public page you control. Things to check in the response:
   - no attacker URL appears in `report.markdown` (model-written text has URLs stripped; links are only
     rendered from Finding records),
   - no citation to a non-existent finding (`report.citation_check.rate` must be 1.0),
   - no raw HTML/markdown links/images from model text (all escaped),
   - the injected page can at most influence the *claims* extracted from that page (residual risk).
2. **Delimiter forging**: page text containing `<<<END PAGE>>>` is neutralised before prompting.
3. **SSRF via search results**: a search hit pointing at `http://169.254.169.254/`, `localhost`, a private IP,
   a non-80/443 port or a redirect to any of these is reported as `blocked` in the trace.
4. **Cost abuse**: long or adversarial questions cannot exceed the graph budgets (sub-questions, searches,
   tokens, wall clock, run USD) or the shared ledger cap.
5. **AuthZ**: runs are visible only to the API key that created them (404 otherwise).
