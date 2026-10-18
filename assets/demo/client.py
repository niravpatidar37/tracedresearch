"""Tiny API client used in the demo recording: talks to the real tracedresearch HTTP API and
prints compact, coloured one-line summaries of the JSON it gets back (so it fits 100 columns).

    python assets/demo/client.py events RUN_ID [--until NODE] [--since-last]
    python assets/demo/client.py trace RUN_ID
    python assets/demo/client.py report RUN_ID

Reads the API base URL from $API (default http://127.0.0.1:8102) and the key from $KEY.
It only formats; every value printed comes from the API response.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

BASE = os.environ.get("API", "http://127.0.0.1:8102")
if not BASE.startswith("http"):
    BASE = "http://" + BASE
HEAD = {"X-API-Key": os.environ.get("KEY", "")}
LAST = Path(__file__).resolve().parent / ".work" / "last-event-id"
G, R, Y, C, D, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[1m", "\033[0m"


def cut(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def show_event(ev: dict[str, Any]) -> None:
    t = ev.get("type")
    if t in ("started", "resumed"):
        col = G if t == "started" else Y
        line = f"{col}{B}{t:<9}{X} at step {ev['step']}  worker pid {ev['pid']}"
        if t == "resumed":
            line += f"  {D}skips {len(ev.get('completed_nodes', []))} committed nodes{X}"
        print(line, flush=True)
    elif t == "node_completed":
        st = ev.get("status", "")
        col = G if st == "ok" else Y
        msg = cut(ev.get("message", ""), 52)
        print(f"  step {ev['step']:>2}  {C}{ev['key']:<16}{X} {col}{st:<6}{X} {D}{msg}{X}", flush=True)
    elif t == "node_cached":
        print(f"  {Y}replayed from snapshot{X} {ev.get('key')}", flush=True)
    elif t == "preempted":
        print(f"{Y}preempted{X} at step {ev.get('step')} ({ev.get('reason')})", flush=True)
    elif t in ("completed", "failed"):
        col = G if t == "completed" else R
        usd = float(ev.get("usd", 0))
        print(f"{col}{B}{t}{X} at step {ev['step']}  tokens {ev.get('tokens_used')}  usd ${usd:.4f}", flush=True)


def events(run_id: str, until: str | None, since_last: bool) -> None:
    head = dict(HEAD)
    if since_last and LAST.exists():
        head["Last-Event-ID"] = LAST.read_text(encoding="utf-8").strip()
    timeout = httpx.Timeout(900, read=900)
    with httpx.stream("GET", f"{BASE}/research/{run_id}/events", headers=head, timeout=timeout) as r:
        r.raise_for_status()
        eid = ""
        for line in r.iter_lines():
            if line.startswith("id: "):
                eid = line[4:]
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            show_event(ev)
            LAST.parent.mkdir(exist_ok=True)
            LAST.write_text(eid, encoding="utf-8")
            if ev.get("type") in ("completed", "failed"):
                return
            if until and ev.get("type") == "node_completed" and ev.get("node") == until:
                return


def trace(run_id: str) -> None:
    data = httpx.get(f"{BASE}/research/{run_id}/trace", headers=HEAD, timeout=30).json()
    steps = data["steps"]
    print(f"{D}step  node_key          agent                pid     latency  tokens  tools{X}")
    for s in steps:
        tools: dict[str, int] = {}
        for t in s["tool_calls"]:
            k = f"{t['tool']}:{t['status']}"
            tools[k] = tools.get(k, 0) + 1
        tl = " ".join(f"{k}x{v}" for k, v in tools.items()) or "-"
        print(
            f"{s['step']:>4}  {C}{s['node_key']:<16}{X}  {s['agent']:<19}  {s['pid']:<6} "
            f"{s['latency_ms'] / 1000:>7.1f}s  {s['tokens']:>6}  {D}{cut(tl, 24)}{X}"
        )
    keys = [s["node_key"] for s in steps]
    pids: dict[int, int] = {}
    for s in steps:
        pids[s["pid"]] = pids.get(s["pid"], 0) + 1
    dup = len(keys) - len(set(keys))
    col = G if dup == 0 else R
    per_pid = ", ".join(f"pid {p}: {n} steps" for p, n in pids.items())
    print(f"{B}{len(keys)} steps, {col}{dup} node keys executed twice{X}{B}; {per_pid}{X}")


def report(run_id: str) -> None:
    run = httpx.get(f"{BASE}/research/{run_id}", headers=HEAD, timeout=30).json()
    rep = run.get("report") or {}
    cc = rep.get("citation_check") or {}
    prog = run["progress"]
    md = rep.get("markdown", "")
    print(f"{B}{cut(rep.get('title', '(no report)'), 90)}{X}")
    print(
        f"status {run['status']}  findings {prog['findings']}  tokens {prog['tokens_used']}  "
        f"usd ${prog['usd_spent']:.4f}  active {prog['elapsed_s']:.0f}s  fallback report: {rep.get('fallback')}"
    )
    col = G if cc.get("rate") == 1.0 else R
    print(f"citations valid: {col}{B}{cc.get('valid')}/{cc.get('total')}{X} (every [^n] resolves to a Finding record)")
    hits = md.count("attacker.example")
    print(f"attacker URL from the poisoned fixture page in the report: {G if hits == 0 else R}{B}{hits}{X}")
    for c in rep.get("citations", [])[:3]:
        print(f"  {D}[^{c.get('number')}] {cut(c.get('url', ''), 80)}{X}")


def main(argv: list[str]) -> None:
    cmd, run_id = argv[0], argv[1]
    if cmd == "events":
        until = argv[argv.index("--until") + 1] if "--until" in argv else None
        events(run_id, until, "--since-last" in argv)
    elif cmd == "trace":
        trace(run_id)
    elif cmd == "report":
        report(run_id)
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main(sys.argv[1:])
