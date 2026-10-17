"""Dump a run's trace from Redis: python scripts/show_trace.py <run_id> [--full]"""

import json
import sys

from research_assistant.config import Settings
from research_assistant.store import RunStore

store = RunStore.from_url(Settings.from_env().redis_url)
rid = sys.argv[1]
full = "--full" in sys.argv
for s in store.trace(rid):
    if full:
        print(json.dumps(s, indent=1))
        continue
    print(
        f"#{s['step']} {s['node']:<10} {s['node_key']:<16} {s['latency_ms']:>8.0f}ms tok={s['tokens']:<5} {s['status']} | {s['output'][:160]}"
    )
    for t in s["tool_calls"]:
        print(f"     tool {t['tool']:<6} {t['status']:<12} {t['detail'][:40]:<40} {t['input'][:90]}")
    for c in s["llm_calls"]:
        print(
            f"     llm  {c['role']:<10} {c['model']:<22} in={c['input_tokens']} out={c['output_tokens']} ok={c['ok']} {c['error'][:120]}"
        )
