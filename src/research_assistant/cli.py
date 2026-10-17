"""CLI: run / resume / show a research run without the API."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from research_assistant.config import Settings
from research_assistant.logging_utils import setup_logging
from research_assistant.wiring import build_runner


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
    args = ap.parse_args(argv)

    settings = Settings.from_env()
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
