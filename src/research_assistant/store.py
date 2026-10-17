"""Durable run state in plain Redis (no modules needed).

Keys (prefix ra:):
  run:{id}:state        JSON RunState, rewritten after every node (atomic MULTI with the rest)
  run:{id}:node:{key}   JSON RunState snapshot after node `key` -> idempotent re-entry
  run:{id}:trace        list of JSON step records
  run:{id}:events       stream of progress events (SSE source)
  run:{id}:lease        worker ownership lease (SET NX EX + heartbeat)
  runs:active           set of unfinished run ids (resumed on startup)
"""

from __future__ import annotations

import json
import secrets
from typing import Any, cast

import redis

from research_assistant.schemas import RunState

P = "ra:"
DONE_TTL_S = 7 * 24 * 3600


class RunStore:
    def __init__(self, client: redis.Redis) -> None:
        self.r = client

    @classmethod
    def from_url(cls, url: str) -> RunStore:
        return cls(redis.Redis.from_url(url, decode_responses=True, socket_timeout=10))

    # ------------------------------------------------------------ keys
    @staticmethod
    def k(run_id: str, part: str) -> str:
        return f"{P}run:{run_id}:{part}"

    # ------------------------------------------------------------ state
    def create(self, state: RunState) -> None:
        pipe = self.r.pipeline(transaction=True)
        pipe.set(self.k(state.run_id, "state"), state.model_dump_json())
        pipe.sadd(f"{P}runs:active", state.run_id)
        pipe.xadd(
            self.k(state.run_id, "events"),
            {"data": json.dumps({"type": "queued", "step": 0})},
            maxlen=2000,
            approximate=True,
        )
        pipe.execute()

    def load(self, run_id: str) -> RunState | None:
        raw = self.r.get(self.k(run_id, "state"))
        return RunState.model_validate_json(cast(str, raw)) if raw else None

    def save(self, state: RunState) -> None:
        self.r.set(self.k(state.run_id, "state"), state.model_dump_json())

    def get_node(self, run_id: str, node_key: str) -> RunState | None:
        raw = self.r.get(self.k(run_id, f"node:{node_key}"))
        return RunState.model_validate_json(cast(str, raw)) if raw else None

    def commit_node(self, state: RunState, node_key: str, step: dict[str, Any], event: dict[str, Any]) -> None:
        """Atomically persist state + node snapshot + trace step + progress event."""
        rid = state.run_id
        blob = state.model_dump_json()
        pipe = self.r.pipeline(transaction=True)
        pipe.set(self.k(rid, "state"), blob)
        pipe.set(self.k(rid, f"node:{node_key}"), blob, ex=DONE_TTL_S)
        pipe.rpush(self.k(rid, "trace"), json.dumps(step, default=str))
        pipe.xadd(self.k(rid, "events"), {"data": json.dumps(event, default=str)}, maxlen=2000, approximate=True)
        pipe.execute()

    def event(self, run_id: str, event: dict[str, Any]) -> None:
        self.r.xadd(self.k(run_id, "events"), {"data": json.dumps(event, default=str)}, maxlen=2000, approximate=True)

    def finish(self, state: RunState) -> None:
        pipe = self.r.pipeline(transaction=True)
        pipe.set(self.k(state.run_id, "state"), state.model_dump_json())
        pipe.srem(f"{P}runs:active", state.run_id)
        for part in ("state", "trace", "events"):
            pipe.expire(self.k(state.run_id, part), DONE_TTL_S)
        pipe.execute()

    def trace(self, run_id: str) -> list[dict[str, Any]]:
        return [json.loads(x) for x in cast(list[str], self.r.lrange(self.k(run_id, "trace"), 0, -1))]

    def events(self, run_id: str, last_id: str = "0", block_ms: int | None = None) -> list[tuple[str, dict[str, Any]]]:
        res = self.r.xread({self.k(run_id, "events"): last_id}, block=block_ms, count=100)
        out: list[tuple[str, dict[str, Any]]] = []
        for _stream, entries in cast(list[Any], res or []):
            for eid, fields in entries:
                out.append((eid, json.loads(fields["data"])))
        return out

    def active_runs(self) -> list[str]:
        return sorted(cast(set[str], self.r.smembers(f"{P}runs:active")))

    # ------------------------------------------------------------ lease (one worker per run)
    def acquire_lease(self, run_id: str, ttl_s: int) -> str | None:
        token = secrets.token_hex(8)
        ok = self.r.set(self.k(run_id, "lease"), token, nx=True, ex=ttl_s)
        return token if ok else None

    def renew_lease(self, run_id: str, token: str, ttl_s: int) -> bool:
        key = self.k(run_id, "lease")
        if self.r.get(key) != token:
            return False
        return bool(self.r.expire(key, ttl_s))

    def release_lease(self, run_id: str, token: str) -> None:
        key = self.k(run_id, "lease")
        if self.r.get(key) == token:
            self.r.delete(key)

    # ------------------------------------------------------------ rate limiting (fixed window)
    def hit_rate_limit(self, key_id: str, window: int, limit: int) -> bool:
        """Return True if this request is allowed."""
        k = f"{P}rl:{key_id}:{window}"
        pipe = self.r.pipeline(transaction=True)
        pipe.incr(k)
        pipe.expire(k, 120)
        count = int(pipe.execute()[0])
        return count <= limit
