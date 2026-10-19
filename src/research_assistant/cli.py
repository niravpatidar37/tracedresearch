"""CLI: run / resume / show a research run without the API, plus ops commands (migrate, kill-switch)."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from research_assistant import migrations
from research_assistant.config import Settings
from research_assistant.logging_utils import setup_logging
from research_assistant.store import RunStore


def _scale(args: argparse.Namespace, settings: Settings, store: RunStore) -> int:
    import os

    import httpx

    from research_assistant.control import KillSwitch
    from research_assistant.scaler import WorkerPoolScaler, metadata_token, run_once

    scaler = None
    if args.cmd == "scale-workers":
        pool = os.environ.get("WORKER_POOL_NAME", "")
        if not pool:
            print("WORKER_POOL_NAME (projects/P/locations/L/workerPools/NAME) is required", file=sys.stderr)
            return 2
        http = httpx.Client(trust_env=False)
        scaler = WorkerPoolScaler(pool, http, lambda: metadata_token(http))
    out = run_once(
        store,
        scaler,
        runs_per_instance=settings.max_concurrent_runs,
        min_n=int(os.environ.get("SCALER_MIN_INSTANCES", "1")),
        max_n=int(os.environ.get("SCALER_MAX_INSTANCES", "4")),
        kill_switch=KillSwitch(settings, store).active(),
        dry_run=getattr(args, "dry_run", False),
    )
    print(json.dumps(out))
    return 0


def _ops(args: argparse.Namespace, settings: Settings) -> int:
    store = RunStore.from_settings(settings)  # ops commands never need an LLM key
    if args.cmd in ("queue-stats", "scale-workers"):
        return _scale(args, settings, store)
    if args.cmd == "migrate":
        before = migrations.current_version(store.r)
        if args.check:
            todo = [m.name for m in migrations.pending(store.r)]
            print(json.dumps({"schema_version": before, "required": migrations.REQUIRED_VERSION, "pending": todo}))
            return 1 if todo else 0
        applied = migrations.migrate(store.r)
        after = migrations.current_version(store.r)
        print(json.dumps({"from": before, "to": after, "applied": applied}))
        return 0
    if args.state != "status":
        store.set_kill_switch(args.state == "on", reason=args.reason)
    print(json.dumps({"kill_switch_redis": store.kill_switch_set(), "kill_switch_env": settings.kill_switch}))
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    ap = argparse.ArgumentParser(prog="research-assistant")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a research question to completion")
    r.add_argument("question")
    s = sub.add_parser("resume", help="resume an unfinished run by id")
    s.add_argument("run_id")
    sh = sub.add_parser("show", help="print state summary + report")
    sh.add_argument("run_id")
    mg = sub.add_parser("migrate", help="apply pending Redis keyspace migrations (deploy step, not app startup)")
    mg.add_argument("--check", action="store_true", help="only report pending migrations; exit 1 if any")
    ks = sub.add_parser("kill-switch", help="engage/release the runtime kill switch stored in Redis")
    ks.add_argument("state", choices=["on", "off", "status"])
    ks.add_argument("--reason", default="", help="free text stored with the switch (no secrets/PII)")
    sub.add_parser("queue-stats", help="print queue depth / oldest queued run age (also logged as queue_stats)")
    sc = sub.add_parser("scale-workers", help="set the worker pool instance count from the queue (Cloud Run job)")
    sc.add_argument("--dry-run", action="store_true", help="compute and log, do not call the Admin API")
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    if args.cmd in ("migrate", "kill-switch", "queue-stats", "scale-workers"):
        return _ops(args, settings)

    from research_assistant.wiring import build_runner

    runner = build_runner(settings)
    if args.cmd == "run":
        rid = runner.create_run(args.question)
        print(f"run_id={rid}", flush=True)
        final = runner.execute(rid)
    elif args.cmd == "resume":
        final = runner.execute(args.run_id)
    else:
        final = runner.store.load(args.run_id)
    if final is None:
        print("run not found or owned by another worker", file=sys.stderr)
        return 2
    summary = {
        "run_id": final.run_id,
        "status": final.status,
        "tokens": final.tokens_used,
        "usd": round(final.usd_spent, 6),
        "elapsed_s": round(final.elapsed_s, 1),
        "searches": final.searches_used,
        "findings": len(final.findings),
        "budget_tripped": final.budget_tripped,
        "error": final.error,
    }
    print(json.dumps(summary, indent=2))
    if final.report:
        print(final.report.markdown)
    logging.shutdown()
    return 0 if final.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
