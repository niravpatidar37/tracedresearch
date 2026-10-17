"""Baseline: adopt the v0.1 key layout and drop dangling ids from the active-run set.

A run id can be left in `ra:runs:active` without a state key if its state expired or was deleted by hand;
the resume loop would then poll it forever. Idempotent.
"""

from __future__ import annotations

from typing import cast

import redis

VERSION = 1
DESCRIPTION = "baseline key layout; prune active-run ids without state"


def up(r: redis.Redis) -> None:
    for rid in cast(set[str], r.smembers("ra:runs:active")):
        if not r.exists(f"ra:run:{rid}:state"):
            r.srem("ra:runs:active", rid)
