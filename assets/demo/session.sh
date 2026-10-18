#!/usr/bin/env bash
# The recorded session (run by record.py from the repo root). Every command and all of its output
# are real; this script only prints the amber comments and the prompt, and paces the typing.
# Stack: Redis 7 (docker compose, 127.0.0.1:16379), the offline fixture corpus, local llama3.1:8b via
# Ollama. $0, no API keys. The server logs go to assets/demo/.work/, not to the screen.
set -u
W=assets/demo/.work
mkdir -p "$W"
export PATH="$PWD/.venv/Scripts:$PWD/.venv/bin:$PATH"
export SEARCH_FIXTURE_FILE=fixtures/redteam_corpus.example.json LLM_PROVIDER=ollama
export LLM_CACHE_DIR="$W/llm-cache" REDIS_URL="${DEMO_REDIS_URL:-redis://127.0.0.1:16379/5}"
export PORTFOLIO_ENV_FILE=/nonexistent RUN_LEASE_TTL_S=10 BUDGET_MAX_SUB_QUESTIONS=2 LOG_FORMAT=text
export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
KEY="demo-$(python -c 'import secrets; print(secrets.token_hex(16))')"
export KEY API=127.0.0.1:8102 RESEARCH_API_KEYS="$KEY"   # throwaway key, never printed
H="X-API-Key: $KEY"
J='content-type: application/json'

A=$'\e[1;33m' G=$'\e[1;32m' Rd=$'\e[1;31m' R=$'\e[0m'
typed() { printf '%s$%s ' "$G" "$R"; local s=$1 i; for ((i = 0; i < ${#s}; i++)); do printf '%s' "${s:i:1}"; sleep 0.022; done; printf '\n'; sleep 0.3; }
note() { printf '%s# %s%s\n' "$A" "$1" "$R"; sleep 1.0; }
run() { typed "$1"; eval "$1"; sleep "${2:-1.0}"; }
APP="research_assistant.api:app_factory --factory --host 127.0.0.1 --port 8102"
serve() {  # $1 = log file; start the API in the background and wait for /readyz
  uvicorn $APP >"$1" 2>&1 &
  SERVER=$!
  for _ in $(seq 60); do curl -sf "$API/readyz" >/dev/null && break; sleep 0.5; done
}

note "tracedresearch: offline fixture corpus + local llama3.1:8b (Ollama). \$0, no API keys."
run "research-assistant migrate" 0.8
typed "uvicorn research_assistant.api:app_factory --factory --port 8102 &   # server #1"
serve "$W/server1.log"
run "curl -s \$API/readyz; echo" 0.8
note "1. submit a question; the API answers 202 at once and a worker starts the run"
typed "curl -s -X POST \$API/research -H \"\$H\" -H \"\$J\" -d '{\"question\":\"How efficient are solar panels?\"}'"
RESP=$(curl -s -X POST "$API/research" -H "$H" -H "$J" -d '{"question":"How efficient are solar panels?"}')
echo "$RESP"
RUN=$(printf '%s' "$RESP" | python -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
sleep 0.8
note "2. stream progress (SSE). Every step is committed to Redis before the next one starts"
run "python assets/demo/client.py events \$RUN --until planner" 0.1
printf '%s# 3. hard-kill the server mid-run: SIGKILL, no graceful shutdown, no drain%s\n' "$A" "$R"
run "kill -9 \$SERVER; sleep 1; curl -s \$API/healthz || echo \"${Rd}server is dead${R}\"" 1.2
note "4. start a fresh server. Once the dead worker's lease expires, it resumes the run"
typed "uvicorn research_assistant.api:app_factory --factory --port 8102 &   # server #2"
serve "$W/server2.log"
run "python assets/demo/client.py events \$RUN --since-last" 1.2
note "5. the step-level trace: who ran each node, how long, how many tokens"
run "python assets/demo/client.py trace \$RUN" 3.0
note "6. the report: every citation is checked against the stored findings"
run "python assets/demo/client.py report \$RUN" 4.0
kill "$SERVER" 2>/dev/null
wait 2>/dev/null
printf '%s# server #2 stopped. that was one run, killed and resumed.%s\n' "$A" "$R"
sleep 1
