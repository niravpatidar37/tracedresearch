"""Deployment controls: liveness vs readiness, migrations, kill switch, graceful drain + requeue,
api/worker split over the Redis queue, Redis-backed spend ledger and cache, JSON logs, Redis TLS/AUTH.

Each test would fail if the control it names were removed.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from typing import Any

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from helpers import MODELS, default_llm, make_runner, make_store, plan_responder
from research_assistant import migrations
from research_assistant.api import Service, create_app, key_id
from research_assistant.config import Budgets, Settings
from research_assistant.control import KillSwitch
from research_assistant.graph import new_run
from research_assistant.llm.base import LLMRequest
from research_assistant.llm.cache import RedisCache
from research_assistant.logging_utils import JsonFormatter, RedactingFilter
from research_assistant.runtime import AgentContext, Preempted
from research_assistant.spend import BudgetExceeded, RedisSpendLedger
from research_assistant.store import QUEUE, RunStore, redis_client

KEY = "test-key-aaaaaaaaaaaa"
Q = "How efficient are solar panels today?"


def h() -> dict[str, str]:
    return {"X-API-Key": KEY}


def migrated_store() -> RunStore:
    store = make_store()
    migrations.migrate(store.r)
    return store


# ------------------------------------------------------------------ /healthz vs /readyz
class BrokenRedis:
    def __getattr__(self, name: str) -> Any:
        def boom(*a: Any, **k: Any) -> Any:
            raise redis.ConnectionError("down")

        return boom


def test_healthz_is_liveness_only_and_readyz_checks_redis() -> None:
    runner = make_runner(store=RunStore(BrokenRedis()))  # type: ignore[arg-type]
    app = create_app(runner=runner, settings=Settings(api_keys=(KEY,)), start_workers=False)
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200  # a Redis outage must not trigger liveness restarts
        r = c.get("/readyz")
        assert r.status_code == 503 and "redis" in r.json()["detail"]


def test_readyz_requires_migrated_schema_then_ready() -> None:
    store = make_store()
    app = create_app(runner=make_runner(store=store), settings=Settings(api_keys=(KEY,)), start_workers=False)
    with TestClient(app) as c:
        r = c.get("/readyz")
        assert r.status_code == 503 and "migrate" in r.json()["detail"]
        migrations.migrate(store.r)
        body = c.get("/readyz").json()
        assert body["status"] == "ready" and body["schema_version"] == migrations.REQUIRED_VERSION


def test_readyz_reports_not_ready_while_draining() -> None:
    store = migrated_store()
    app = create_app(runner=make_runner(store=store), settings=Settings(api_keys=(KEY,)), start_workers=False)
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 200
        c.app.state.service.draining.set()  # type: ignore[attr-defined]
        assert c.get("/readyz").status_code == 503


# ------------------------------------------------------------------ migrations
def test_migrations_are_versioned_idempotent_and_prune_dangling_runs() -> None:
    store = make_store()
    rid = new_run(store, Q)
    store.r.sadd("ra:runs:active", "deadbeefdeadbeef")  # id without state
    assert migrations.current_version(store.r) == 0
    applied = migrations.migrate(store.r)
    assert applied == ["m0001_baseline"]
    assert store.active_runs() == [rid]
    assert migrations.migrate(store.r) == []  # second run: nothing pending
    assert store.r.hget(migrations.MIGRATIONS_KEY, "0001")


def test_migration_lock_blocks_concurrent_runs() -> None:
    store = make_store()
    store.r.set(migrations.LOCK_KEY, "held")
    with pytest.raises(RuntimeError, match="another migration"):
        migrations.migrate(store.r)
    assert migrations.current_version(store.r) == 0


def test_migrations_do_not_run_at_app_startup() -> None:
    store = make_store()
    app = create_app(runner=make_runner(store=store), settings=Settings(api_keys=(KEY,)), start_workers=False)
    with TestClient(app):
        pass
    assert migrations.current_version(store.r) == 0


# ------------------------------------------------------------------ kill switch
def test_env_kill_switch_refuses_new_runs_but_reads_work() -> None:
    store = migrated_store()
    rid = new_run(store, Q, owner=key_id(KEY))
    app = create_app(
        runner=make_runner(store=store), settings=Settings(api_keys=(KEY,), kill_switch=True), start_workers=False
    )
    with TestClient(app) as c:
        r = c.post("/research", json={"question": Q}, headers=h())
        assert r.status_code == 503 and "kill switch" in r.json()["detail"]
        assert c.get(f"/research/{rid}", headers=h()).status_code == 200
        assert c.get("/readyz").json()["kill_switch"] is True


def test_redis_kill_switch_pauses_inflight_run_before_next_llm_call_and_resumes_after_release() -> None:
    store = migrated_store()
    ks = KillSwitch(Settings(), store)
    ks.CACHE_S = 0.0

    def engage(phase: str, key: str) -> None:
        if phase == "after_commit" and key == "research:sq1:0":
            store.set_kill_switch(True, "incident test")

    llm1 = default_llm()
    r1 = make_runner(llm=llm1, store=store, kill_switch=ks, fault=engage, use_queue=True)
    rid = r1.create_run(Q)
    store.r.delete(QUEUE)
    out = r1.execute(rid)
    assert out is not None and out.status == "queued"
    reviews_before = llm1.count("ReviewOutput")
    assert llm1.count("ExtractionOutput") == 1  # sq2 never extracted: no LLM/tool call after the switch
    assert not store.has_lease(rid) and store.r.lrange(QUEUE, 0, -1) == [rid]
    assert any(e["type"] == "preempted" and e["reason"] == "kill_switch" for _, e in store.events(rid))

    # while engaged, a new attempt is refused before any work
    assert r1.execute(rid) is not None and llm1.count("ReviewOutput") == reviews_before

    store.set_kill_switch(False)
    llm2 = default_llm()
    final = make_runner(llm=llm2, store=store, kill_switch=ks).execute(rid)
    assert final is not None and final.status == "completed"
    assert llm2.count("PlanOutput") == 0
    keys = [s["node_key"] for s in store.trace(rid)]
    assert [k for k, n in Counter(keys).items() if n > 1] == []


def test_kill_switch_stops_worker_from_consuming_queue() -> None:
    store = migrated_store()
    settings = Settings(role="worker", kill_switch=True)
    svc = Service(store, settings, runner=make_runner(store=store, use_queue=True))
    new_run(store, Q, enqueue=True)
    t = threading.Thread(target=svc.consume_loop, daemon=True)
    t.start()
    time.sleep(0.3)
    svc.stop.set()
    t.join(3)
    assert store.queue_depth() == 1 and not svc.inflight


def test_agent_context_preempts_before_llm_call() -> None:
    llm = default_llm()
    ctx = AgentContext(
        llm=llm,
        models=MODELS,
        budgets=Budgets(),
        tokens_used=0,
        usd_spent=0.0,
        deadline=time.monotonic() + 60,
        stop_reason=lambda: "draining",
    )
    from research_assistant.schemas import PlanOutput

    with pytest.raises(Preempted):
        ctx.structured(role="planner", model="m", system="s", user="u", schema=PlanOutput, max_tokens=10)
    assert llm.calls == []


# ------------------------------------------------------------------ graceful shutdown
def test_drain_checkpoints_and_requeues_then_other_instance_resumes_without_replay() -> None:
    store = migrated_store()
    r1 = make_runner(store=store, use_queue=True)

    def sigterm(phase: str, key: str) -> None:
        if phase == "after_commit" and key == "research:sq1:0":
            r1.draining.set()

    r1.fault = sigterm
    llm1 = r1.llm
    rid = r1.create_run(Q)
    assert store.pop_queued(timeout_s=1) == rid  # the worker took it from the queue
    out = r1.execute(rid)
    assert out is not None and out.status == "queued" and "research:sq1:0" in out.completed_nodes
    assert not store.has_lease(rid)  # released immediately, not after TTL
    assert store.r.lrange(QUEUE, 0, -1) == [rid]  # handed back for another instance

    llm2 = default_llm()
    r2 = make_runner(llm=llm2, store=store, use_queue=True)
    assert store.pop_queued(timeout_s=1) == rid
    final = r2.execute(rid)
    assert final is not None and final.status == "completed"
    assert llm2.count("PlanOutput") == 0 and llm1.count("ExtractionOutput") + llm2.count("ExtractionOutput") == 2  # type: ignore[attr-defined]
    keys = [s["node_key"] for s in store.trace(rid)]
    assert [k for k, n in Counter(keys).items() if n > 1] == []


def test_drain_stops_at_node_boundary_even_without_llm_calls() -> None:
    """The supervisor node makes no LLM/tool call, so only the node-boundary check can stop it."""
    store = migrated_store()
    r1 = make_runner(store=store)

    def sigterm(phase: str, key: str) -> None:
        if phase == "after_commit" and key == "planner":
            r1.draining.set()

    r1.fault = sigterm
    rid = r1.create_run(Q)
    out = r1.execute(rid)
    assert out is not None and out.status == "queued"
    assert out.completed_nodes[-1] == "planner"  # no further node committed after SIGTERM


def test_service_shutdown_requeues_unstarted_and_reports_stuck_runs() -> None:
    store = migrated_store()
    gate = threading.Event()
    entered = threading.Event()

    def slow_plan(req: LLMRequest) -> dict[str, Any]:
        entered.set()
        gate.wait(5)
        return plan_responder(2)(req)  # type: ignore[no-any-return]

    runner = make_runner(llm=default_llm(PlanOutput=slow_plan), store=store, use_queue=True)
    settings = Settings(role="worker", max_concurrent_runs=1, shutdown_grace_s=0.3)
    svc = Service(store, settings, runner=runner)
    a = new_run(store, Q)
    b = new_run(store, Q + " (second)")
    assert svc.submit(a) and svc.submit(b)  # b waits behind a (1 slot)
    assert entered.wait(3)
    stuck = svc.shutdown()
    assert stuck == [a]  # a is inside a long LLM call: keeps its lease, will expire -> resumed elsewhere
    assert store.has_lease(a)
    assert store.r.lrange(QUEUE, 0, -1) == [b]  # b never started: handed back right away
    assert not svc.submit(new_run(store, Q))  # no new work while draining
    gate.set()


# ------------------------------------------------------------------ api / worker split
def test_api_role_enqueues_and_worker_role_executes() -> None:
    store = migrated_store()
    api = create_app(settings=Settings(role="api", api_keys=(KEY,)), store=store, start_workers=False)
    with TestClient(api) as c:
        assert c.app.state.service.runner is None  # type: ignore[attr-defined]  # API holds no LLM client
        rid = c.post("/research", json={"question": Q}, headers=h()).json()["run_id"]
        assert store.queue_depth() == 1 and store.load(rid).status == "queued"  # type: ignore[union-attr]

    runner = make_runner(store=store, use_queue=True)
    svc = Service(store, Settings(role="worker"), runner=runner)
    t = threading.Thread(target=svc.consume_loop, daemon=True)
    t.start()
    for _ in range(200):
        st = store.load(rid)
        if st and st.status == "completed":
            break
        time.sleep(0.05)
    svc.stop.set()
    t.join(5)
    assert store.load(rid).status == "completed" and store.queue_depth() == 0  # type: ignore[union-attr]


def test_worker_role_exposes_only_probes() -> None:
    store = migrated_store()
    app = create_app(
        runner=make_runner(store=store), settings=Settings(role="worker", api_keys=(KEY,)), start_workers=False
    )
    with TestClient(app) as c:
        assert c.get("/readyz").status_code == 200
        assert c.post("/research", json={"question": Q}, headers=h()).status_code == 404


def test_queued_runs_are_not_stolen_by_resume_loop_until_grace() -> None:
    store = migrated_store()
    svc = Service(store, Settings(role="worker", orphan_grace_s=30), runner=make_runner(store=store))
    rid = new_run(store, Q, enqueue=True)
    assert svc.resume_candidates() == []
    st = store.load(rid)
    assert st is not None
    st.updated_at = st.updated_at.replace(year=st.updated_at.year - 1)
    store.save(st)
    assert svc.resume_candidates() == [rid]  # lost queue message: recovered after the grace period


def test_invalid_service_role_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTFOLIO_ENV_FILE", "does-not-exist.env")
    monkeypatch.setenv("SERVICE_ROLE", "wroker")
    with pytest.raises(ValueError, match="SERVICE_ROLE"):
        Settings.from_env()


# ------------------------------------------------------------------ spend ledger + cache in Redis
def test_redis_spend_ledger_enforces_caps_per_month() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    now = [1_790_000_000.0]  # 2026-09
    led = RedisSpendLedger(
        client=r, project="p2-research", global_cap_usd=1.0, project_cap_usd=0.5, clock=lambda: now[0]
    )
    led.check(0.4)
    led.record("groq", "m", 100, 100, 0.45)
    with pytest.raises(BudgetExceeded) as exc:
        led.check(0.1)
    assert exc.value.kind == "project_usd"
    rec = json.loads(r.xrange("ra:spend:ledger")[0][1]["data"])
    assert rec["project"] == "p2-research" and rec["usd"] == 0.45
    now[0] += 40 * 86_400  # next month: fresh window
    led.check(0.1)


def test_redis_spend_ledger_is_shared_across_instances() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    a = RedisSpendLedger(client=r, project="p2-research", global_cap_usd=1.0, project_cap_usd=0.5)
    b = RedisSpendLedger(client=r, project="p2-research", global_cap_usd=1.0, project_cap_usd=0.5)
    a.record("groq", "m", 1, 1, 0.45)
    with pytest.raises(BudgetExceeded):
        b.check(0.1)  # a per-instance file ledger would have allowed this


def test_redis_cache_roundtrip_with_ttl() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    c = RedisCache(r, ttl_s=60)
    c.set("abc", {"text": "x", "input_tokens": 1, "output_tokens": 2})
    assert c.get("abc") == {"text": "x", "input_tokens": 1, "output_tokens": 2}
    assert 0 < r.ttl("ra:llmcache:abc") <= 60
    assert c.get("missing") is None


# ------------------------------------------------------------------ logs + redis client
def test_json_logs_are_structured_and_redacted() -> None:
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "user bob@example.com key gsk_%s", ("A" * 20,), None)
    try:
        raise ValueError("rediss://:hunter2pass@10.0.0.3:6378 failed for bob@example.com")
    except ValueError:
        import sys

        rec.exc_info = sys.exc_info()
    RedactingFilter().filter(rec)
    line = JsonFormatter().format(rec)
    entry = json.loads(line)
    assert entry["severity"] == "ERROR" and "message" in entry
    assert "bob@example.com" not in line and "gsk_AAAA" not in line and "hunter2pass" not in line
    assert "[email]" in entry["message"] and "[secret]" in entry["message"]


def test_redis_client_uses_tls_auth_and_keeps_password_out_of_url() -> None:
    s = Settings(redis_url="rediss://10.1.2.3:6378/0", redis_password="pw-from-secret-manager", redis_ca_cert_pem="PEM")
    c = redis_client(s)
    kw = c.connection_pool.connection_kwargs
    assert c.connection_pool.connection_class is redis.connection.SSLConnection
    assert kw["password"] == "pw-from-secret-manager" and kw["ssl_ca_data"] == "PEM"
    assert kw["ssl_cert_reqs"] == "required"
    plain = redis_client(Settings(redis_url="redis://127.0.0.1:16379/0"))
    assert plain.connection_pool.connection_kwargs.get("password") is None
