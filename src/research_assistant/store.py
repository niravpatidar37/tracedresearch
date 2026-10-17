"""Durable run state in plain Redis (no modules needed).

Keys (prefix ra:):
  run:{id}:state        JSON RunState, rewritten after every node (atomic MULTI with the rest)
  run:{id}:node:{key}   JSON RunState snapshot after node `key` -> idempotent re-entry
  run:{id}:trace        list of JSON step records
  run:{id}:events       stream of progress events (SSE source)
  run:{id}:lease        worker ownership lease (SET NX EX + heartbeat)
  runs:active           set of unfinished run ids (resumed on startup)
  queue                 list of run ids waiting for a worker (LPUSH new, RPUSH preempted, BRPOP)
  schema:*              keyspace migrations (see research_assistant.migrations)
  control:kill_switch   runtime kill switch (set/cleared by `research-assistant kill-switch`)
"""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING, Any, cast

import redis

from research_assistant.schemas import RunState

if TYPE_CHECKING:
    from research_assistant.config import Settings

P = "ra:"
DONE_TTL_S = 7 * 24 * 3600
QUEUE = f"{P}queue"
KILL_SWITCH_KEY = f"{P}control:kill_switch"


def redis_client(settings: Settings) -> redis.Redis:
    """Build the Redis client. AUTH and the TLS CA come from separate env vars, never from the URL.

    Memorystore with in-transit encryption: REDIS_URL=rediss://<host>:6378/0, REDIS_PASSWORD from
    Secret Manager, REDIS_CA_CERT_PEM = the instance's server CA (public). The CA is verified and
    hostname checking is off because Memorystore certificates are issued for the instance IP.
    """
    kwargs: dict[str, Any] = {
        "decode_responses": True,
        "socket_timeout": 10,
        "socket_connect_timeout": 5,
        "health_check_interval": 30,
    }
    if settings.redis_password:
        kwargs["password"] = settings.redis_password
    if settings.redis_url.startswith("rediss://"):
        kwargs["ssl_cert_reqs"] = "required"
        kwargs["ssl_check_hostname"] = False
        if settings.redis_ca_cert_pem:
            kwargs["ssl_ca_data"] = settings.redis_ca_cert_pem
    return redis.Redis.from_url(settings.redis_url, **kwargs)


class RunStore:
    def __init__(self, client: redis.Redis) -> None:
        self.r = client

    @classmethod
    def from_url(cls, url: str) -> RunStore:
        return cls(redis.Redis.from_url(url, decode_responses=True, socket_timeout=10))

    @classmethod
    def from_settings(cls, settings: Settings) -> RunStore:
        return cls(redis_client(settings))

    # ------------------------------------------------------------ keys
    @staticmethod
    def k(run_id: str, part: str) -> str:
        return f"{P}run:{run_id}:{part}"

    # ------------------------------------------------------------ state
    def create(self, state: RunState, *, enqueue: bool = False) -> None:
        pipe = self.r.pipeline(transaction=True)
        pipe.set(self.k(state.run_id, "state"), state.model_dump_json())
        pipe.sadd(f"{P}runs:active", state.run_id)
        pipe.xadd(
            self.k(state.run_id, "events"),
            {"data": json.dumps({"type": "queued", "step": 0})},
            maxlen=2000,
            approximate=True,
        )
        if enqueue:
            pipe.lpush(QUEUE, state.run_id)
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

    # ------------------------------------------------------------ work queue (api -> worker pool)
    def requeue(self, run_id: str) -> None:
        """Put a preempted run at the consuming end of the queue so it is picked up next."""
        self.r.rpush(QUEUE, run_id)

    def pop_queued(self, timeout_s: int = 2) -> str | None:
        res = self.r.brpop([QUEUE], timeout=timeout_s)
        if not res:
            return None
        return cast(str, cast(Any, res)[1])

    def queue_depth(self) -> int:
        return int(self.r.llen(QUEUE))

    # ------------------------------------------------------------ runtime kill switch
    def kill_switch_set(self) -> bool:
        return bool(self.r.exists(KILL_SWITCH_KEY))

    def set_kill_switch(self, on: bool, reason: str = "") -> None:
        if on:
            self.r.set(KILL_SWITCH_KEY, reason or "on")
        else:
            self.r.delete(KILL_SWITCH_KEY)

    # ------------------------------------------------------------ lease (one worker per run)
    def acquire_lease(self, run_id: str, ttl_s: int) -> str | None:
        token = secrets.token_hex(8)
        ok = self.r.set(self.k(run_id, "lease"), token, nx=True, ex=ttl_s)
        return token if ok else None

    def has_lease(self, run_id: str) -> bool:
        return self.r.get(self.k(run_id, "lease")) is not None

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
