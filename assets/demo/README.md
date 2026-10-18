# Demo recording

`demo.gif` is a real session, recorded on Windows 11 on 2026-10-18 with the scripts in this folder.
Nothing in it is mocked, cached or edited.

## What was real

- **The service:** the API from this repo (`uvicorn research_assistant.api:app_factory`), two separate
  server processes, real Redis 7.4.7 (`docker compose -p p2-research up -d redis`, database 5, flushed before the
  recording).
- **The model:** local `llama3.1:8b` through Ollama, live. The LLM cache directory was empty, so every LLM call in the
  session ran on the GPU. Cost: $0.
- **The kill:** `kill -9` on server #1 while the researcher step was running. `server1.log` in [`logs/`](logs) ends
  mid-run with no shutdown lines; [`logs/server2.log`](logs/server2.log) has
  `run 5a674df42ee8496e resumed at step 3 (pid 38192); completed nodes: ['supervisor:0', 'planner', 'supervisor:2']`.
- **The output:** `demo.cast` holds exactly the bytes the session printed, with real timestamps.

## What was not live

- **Search and fetch** come from the offline fixture corpus `fixtures/redteam_corpus.example.json`
  (`SEARCH_FIXTURE_FILE`): three hand-written pages, one of them poisoned with a prompt injection and an attacker URL,
  one paywalled. No web traffic.
- **Budgets** were lowered for a short run: `BUDGET_MAX_SUB_QUESTIONS=2` (default 4) and `RUN_LEASE_TTL_S=10`.
- **The writer fell back.** Both of the 8B model's writer attempts failed schema validation (two `ValidationError`
  LLM calls in the trace), so the run ended with the deterministic fallback report (`fallback report: True`), whose
  4 citations are all valid. (`attempts=0` in that step's summary is a known reporting quirk: fallback reports do
  not record the writer's attempt count.) The benchmark in the main README used
  Groq `gpt-oss-120b` as the writer.
- **Playback** is sped up: agg caps idle gaps at 1.5 s and plays at 1.6×. The real session took 94 s (planner 7.4 s,
  researcher 7.2 s, writer 27.9 s on the GPU, as the trace in the GIF shows).
- `session.sh` prints the teal `#` comments and the `$` prompt and paces the typing. `client.py` turns the API's JSON
  into one line per event or step; it prints only values from the API responses. The pid in the trace is the
  Python interpreter's, which differs from the `$!` that bash kills (the `uvicorn` launcher), so it is not shown.

## Re-record

Needs Redis on 127.0.0.1:16379, Ollama with `llama3.1:8b`, Git Bash, [agg](https://github.com/asciinema/agg) 1.5
and the project venv (`uv sync`). From the repo root:

```sh
docker compose -p p2-research up -d redis
docker exec p2-research-redis-1 redis-cli -n 5 flushdb
rm -rf assets/demo/.work
.venv/Scripts/python.exe assets/demo/record.py assets/demo/demo.cast     # runs session.sh, writes asciicast v2
agg --theme 0a1418,e8f1f2,0a1418,f47067,3fb950,2dd4bf,58a6ff,bc8cff,79c0ff,c9d1d9,5c6f73,ff7b72,56d364,5eead4,79c0ff,d2a8ff,a5d6ff,e8f1f2 \
    --font-family Consolas --font-size 18 --idle-time-limit 1.5 --speed 1.6 --last-frame-duration 6 \
    assets/demo/demo.cast assets/demo/.work/raw.gif
uv run --no-project --with pillow python assets/demo/frame.py assets/demo/.work/raw.gif assets/demo/demo.gif "tracedresearch  —  real session"
```

`record.py` exists because asciinema does not run on Windows: it runs the session as a plain subprocess (no pty),
timestamps each chunk of output as it arrives and writes an asciicast v2 file, turning lone `\n` into `\r\n` as a
terminal would. The theme is the brand palette (Ink `0a1418` background, Mist `e8f1f2` text, Teal `2dd4bf` for
comments) with GitHub-dark ANSI colours; `frame.py` adds the window chrome (28 px padding, 48 px title bar, 20 px
rounded corners, transparent outside).
