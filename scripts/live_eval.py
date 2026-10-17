"""Live end-to-end evaluation through the real HTTP API (real Groq + DuckDuckGo + Redis).

  python scripts/live_eval.py bench   # runs results/questions.json, writes results/benchmark/*
  python scripts/live_eval.py crash   # kills the API mid-run, restarts it, proves resume (results/crash_resume/*)

Starts/stops its own uvicorn on 127.0.0.1:8102. Requires the Redis container (docker compose -p p2-research up -d redis).
A throwaway API key is generated per invocation and never written to disk.
"""

from __future__ import annotations

import json
import os
import secrets
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
BASE = "http://127.0.0.1:8102"
PY = sys.executable
API_KEY = "eval-" + secrets.token_hex(16)
HEAD = {"X-API-Key": API_KEY}


def ledger_total(project: str = "p2-research") -> float:
    sys.path.insert(0, str(ROOT / "src"))
    from research_assistant.config import Settings

    p = Path(Settings.from_env().ledger_path)
    if not p.exists():
        return 0.0
    tot = 0.0
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if rec.get("project") == project:
            tot += float(rec.get("usd", 0))
    return tot


def start_server(log_path: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ, RESEARCH_API_KEYS=API_KEY, RATE_LIMIT_PER_MIN="30", RUN_LEASE_TTL_S="10")
    log = log_path.open("ab")
    proc = subprocess.Popen(
        [
            PY,
            "-m",
            "uvicorn",
            "research_assistant.api:app_factory",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8102",
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    for _ in range(60):
        try:
            if httpx.get(BASE + "/healthz", timeout=2).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.kill()
    raise RuntimeError("server did not become healthy")


def stop_server(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_sse(run_id: str, timeout_s: float = 600) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with httpx.stream(
        "GET", f"{BASE}/research/{run_id}/events", headers=HEAD, timeout=httpx.Timeout(timeout_s, read=60)
    ) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                events.append(ev)
                if ev.get("type") in ("completed", "failed"):
                    break
    return events


def save_run(dirpath: Path, run: dict[str, Any], trace: dict[str, Any]) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "run.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
    (dirpath / "trace.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
    if run.get("report"):
        (dirpath / "report.md").write_text(run["report"]["markdown"], encoding="utf-8")


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def make_row(
    i: int,
    q: str,
    rid: str,
    run: dict[str, Any],
    trace: dict[str, Any],
    wall: float,
    accepted_ms: float | None,
    sse_events: int | None,
) -> dict[str, Any]:
    steps = trace["steps"]
    llm_calls = [c for s in steps for c in s["llm_calls"]]
    rep = run.get("report") or {}
    fd = rep.get("first_draft_citation_check") or {}
    fc = rep.get("citation_check") or {}
    prog = run["progress"]
    return {
        "i": i,
        "run_id": rid,
        "question": q,
        "status": run["status"],
        "wall_s": round(wall, 1),
        "post_accept_ms": None if accepted_ms is None else round(accepted_ms, 1),
        "active_s": prog["elapsed_s"],
        "tokens": prog["tokens_used"],
        "usd": prog["usd_spent"],
        "searches": prog["searches_used"],
        "fetches": sum(1 for s in steps for t in s["tool_calls"] if t["tool"] == "fetch"),
        "findings": prog["findings"],
        "llm_calls": sum(1 for c in llm_calls if c["ok"] or c["input_tokens"]),
        "cached_llm_calls": sum(1 for c in llm_calls if c["cached"]),
        "rate_limit_failovers": sum(1 for c in llm_calls if c["error"].startswith("rate_limited")),
        "subq_total": len(prog["sub_questions"]),
        "subq_answered": sum(1 for v in prog["sub_questions"].values() if v["status"] == "answered"),
        "send_backs": sum(v["send_backs"] for v in prog["sub_questions"].values()),
        "budget_tripped": prog["budget_tripped"],
        "fallback_report": rep.get("fallback"),
        "first_draft_citations_valid": fd.get("valid"),
        "first_draft_citations_total": fd.get("total"),
        "final_citations_valid": fc.get("valid"),
        "final_citations_total": fc.get("total"),
        "sse_events": sse_events,
        "search_statuses": [t["status"] for s in steps for t in s["tool_calls"] if t["tool"] == "search"],
        "fetch_statuses": [t["status"] for s in steps for t in s["tool_calls"] if t["tool"] == "fetch"],
        "models_used": sorted({c["model"] for c in llm_calls if c["ok"]}),
    }


def summarize(out: Path, ledger_delta: float | None = None) -> dict[str, Any]:
    rows = [json.loads(ln) for ln in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    done = [r for r in rows if r["status"] == "completed"]
    walls = [r["wall_s"] for r in done]

    def tot(key: str) -> int:
        return sum(r[key] or 0 for r in done)

    from collections import Counter

    summary = {
        "runs": len(rows),
        "completed": len(done),
        "wall_s_p50": round(pct(walls, 0.5), 1),
        "wall_s_p95": round(pct(walls, 0.95), 1),
        "wall_s_max": max(walls) if walls else None,
        "active_s_p50": round(pct([r["active_s"] for r in done], 0.5), 1),
        "tokens_mean": round(statistics.mean(r["tokens"] for r in done)) if done else None,
        "tokens_p95": round(pct([r["tokens"] for r in done], 0.95)),
        "usd_per_run_mean": round(statistics.mean(r["usd"] for r in done), 6) if done else None,
        "usd_per_run_max": round(max(r["usd"] for r in done), 6) if done else None,
        "usd_total_runs": round(sum(r["usd"] for r in rows), 6),
        "ledger_delta_usd": None if ledger_delta is None else round(ledger_delta, 6),
        "searches_per_run_mean": round(statistics.mean(r["searches"] for r in done), 2) if done else None,
        "fetches_per_run_mean": round(statistics.mean(r["fetches"] for r in done), 2) if done else None,
        "findings_per_run_mean": round(statistics.mean(r["findings"] for r in done), 2) if done else None,
        "first_draft_citation_validity": round(
            tot("first_draft_citations_valid") / tot("first_draft_citations_total"), 4
        )
        if tot("first_draft_citations_total")
        else None,
        "first_draft_citations": f"{tot('first_draft_citations_valid')}/{tot('first_draft_citations_total')}",
        "final_citation_validity": round(tot("final_citations_valid") / tot("final_citations_total"), 4)
        if tot("final_citations_total")
        else None,
        "final_citations": f"{tot('final_citations_valid')}/{tot('final_citations_total')}",
        "subq_answered": f"{tot('subq_answered')}/{tot('subq_total')}",
        "send_backs_total": tot("send_backs"),
        "fallback_reports": sum(1 for r in done if r["fallback_report"]),
        "runs_with_budget_tripped": sum(1 for r in rows if r["budget_tripped"]),
        "budget_trips": dict(Counter(b for r in rows for b in r["budget_tripped"])),
        "rate_limit_failovers": sum(r.get("rate_limit_failovers", 0) for r in rows),
        "cached_llm_calls": sum(r["cached_llm_calls"] for r in rows),
        "llm_calls": sum(r["llm_calls"] for r in rows),
        "search_status_counts": dict(Counter(x for r in rows for x in r["search_statuses"])),
        "fetch_status_counts": dict(Counter(x for r in rows for x in r["fetch_statuses"])),
        "runs_under_5_min": sum(1 for w in walls if w < 300),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def bench() -> None:
    questions = json.loads((RESULTS / "questions.json").read_text(encoding="utf-8"))
    out = RESULTS / "benchmark"
    out.mkdir(parents=True, exist_ok=True)
    (out / "runs.jsonl").write_text("", encoding="utf-8")
    spend_before = ledger_total()
    proc = start_server(out / "server.log")
    try:
        for i, q in enumerate(questions, 1):
            t0 = time.perf_counter()
            r = httpx.post(BASE + "/research", json={"question": q}, headers=HEAD, timeout=30)
            r.raise_for_status()
            rid = r.json()["run_id"]
            accepted_ms = (time.perf_counter() - t0) * 1000
            events = wait_sse(rid)
            wall = time.perf_counter() - t0
            run = httpx.get(f"{BASE}/research/{rid}", headers=HEAD, timeout=30).json()
            trace = httpx.get(f"{BASE}/research/{rid}/trace", headers=HEAD, timeout=30).json()
            save_run(out / f"run{i:02d}_{rid}", run, trace)
            row = make_row(i, q, rid, run, trace, wall, accepted_ms, len(events))
            with (out / "runs.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            print(
                json.dumps(
                    {
                        k: row[k]
                        for k in (
                            "i",
                            "status",
                            "wall_s",
                            "tokens",
                            "usd",
                            "searches",
                            "findings",
                            "final_citations_valid",
                            "final_citations_total",
                            "budget_tripped",
                        )
                    }
                ),
                flush=True,
            )
    finally:
        stop_server(proc)
    print(json.dumps(summarize(out, ledger_total() - spend_before), indent=2))


def rebuild_v1() -> None:
    """Rebuild runs.jsonl/summary.json for the first (pre-failover) benchmark from saved run dirs + console."""
    out = RESULTS / "benchmark_v1_no_failover"
    walls = {}
    for ln in (out / "console.txt").read_text(encoding="utf-8").splitlines():
        if ln.startswith("{"):
            d = json.loads(ln)
            walls[d["i"]] = d["wall_s"]
    rows = []
    for d in sorted(out.glob("run*_*")):
        i = int(d.name[3:5])
        run = json.loads((d / "run.json").read_text(encoding="utf-8"))
        trace = json.loads((d / "trace.json").read_text(encoding="utf-8"))
        rows.append(make_row(i, run["question"], run["run_id"], run, trace, walls[i], None, None))
    (out / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(json.dumps(summarize(out), indent=2))


def crash() -> None:
    out = RESULTS / "crash_resume"
    out.mkdir(parents=True, exist_ok=True)
    for f in ("server1.log", "server2.log"):
        (out / f).write_bytes(b"")
    log: list[str] = []

    def note(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
        log.append(line)
        print(line, flush=True)

    q = "What are the main differences between the Raft and Paxos consensus algorithms in practice?"
    p1 = start_server(out / "server1.log")
    note(f"server #1 started pid={p1.pid}")
    rid = httpx.post(BASE + "/research", json={"question": q}, headers=HEAD, timeout=30).json()["run_id"]
    note(f"POST /research -> run_id={rid}")
    killed_after: list[str] = []
    for _ in range(600):
        steps = httpx.get(f"{BASE}/research/{rid}/trace", headers=HEAD, timeout=10).json()["steps"]
        researched = [s for s in steps if s["node"] == "researcher"]
        if len(researched) >= 1:
            killed_after = [s["node_key"] for s in steps]
            break
        time.sleep(0.25)
    p1.kill()  # TerminateProcess: no graceful shutdown, like SIGKILL / a crashed pod
    p1.wait()
    note(f"server #1 KILLED (hard) pid={p1.pid} after {len(killed_after)} committed steps: {killed_after}")
    time.sleep(2)
    p2 = start_server(out / "server2.log")
    note(f"server #2 started pid={p2.pid}; waiting for orphaned-run resume (lease TTL 10s)")
    try:
        t0 = time.perf_counter()
        while True:
            run = httpx.get(f"{BASE}/research/{rid}", headers=HEAD, timeout=10).json()
            if run["status"] in ("completed", "failed") or time.perf_counter() - t0 > 600:
                break
            time.sleep(1)
        trace = httpx.get(f"{BASE}/research/{rid}/trace", headers=HEAD, timeout=10).json()
        with httpx.stream("GET", f"{BASE}/research/{rid}/events", headers=HEAD, timeout=30) as r:
            events = [json.loads(ln[6:]) for ln in r.iter_lines() if ln.startswith("data: ")]
    finally:
        stop_server(p2)
        note(f"server #2 stopped pid={p2.pid}")
    save_run(out / f"run_{rid}", run, trace)
    steps = trace["steps"]
    keys = [s["node_key"] for s in steps]
    pids = sorted({s["pid"] for s in steps})
    by_pid = {pid: [s["node_key"] for s in steps if s["pid"] == pid] for pid in pids}
    resumed = [e for e in events if e.get("type") == "resumed"]
    summary = {
        "run_id": rid,
        "final_status": run["status"],
        "steps_committed_before_kill": len(killed_after),
        "steps_total": len(steps),
        "distinct_node_keys": len(set(keys)),
        "duplicate_node_keys": sorted({k for k in keys if keys.count(k) > 1}),
        "pids_in_trace": pids,
        "server1_pid": p1.pid,
        "server2_pid": p2.pid,
        "steps_by_pid": by_pid,
        "resumed_event": resumed[0] if resumed else None,
        "planner_runs": sum(1 for s in steps if s["node"] == "planner"),
        "verdict": "RESUMED (no replay)"
        if resumed and len(set(keys)) == len(keys) and len(pids) == 2 and run["status"] == "completed"
        else "CHECK MANUALLY",
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    note("verdict: " + summary["verdict"])
    (out / "crash_resume.log").write_text("\n".join(log) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    {"bench": bench, "crash": crash, "rebuild_v1": rebuild_v1}[sys.argv[1]]()
