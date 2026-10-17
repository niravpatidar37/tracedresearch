"""Print planner/writer steps for every run in a benchmark dir: python scripts/inspect_runs.py results/benchmark"""

import json
import sys
from pathlib import Path

for d in sorted(Path(sys.argv[1]).glob("run*_*")):
    trace = json.loads((d / "trace.json").read_text(encoding="utf-8"))
    for s in trace["steps"]:
        if s["node"] in ("planner", "writer"):
            calls = [(c["model"].split("/")[-1], c["ok"], c["error"][:70]) for c in s["llm_calls"]]
            print(d.name[:5], s["node"], round(s["latency_ms"] / 1000, 1), s["output"][:80], calls)
