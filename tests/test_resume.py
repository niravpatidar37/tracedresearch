"""Durable state: crash mid-run, resume from Redis without replaying completed nodes."""

from __future__ import annotations

from collections import Counter

import pytest

from helpers import default_llm, make_runner, make_store
from research_assistant.schemas import RunState


class SimulatedCrash(BaseException):
    """BaseException so no `except Exception` in the code under test can swallow it (like SIGKILL)."""


def crash_at(phase: str, key: str):  # type: ignore[no-untyped-def]
    def hook(p: str, k: str) -> None:
        if p == phase and k == key:
            raise SimulatedCrash(k)

    return hook


def test_resume_after_crash_between_nodes_does_not_replay() -> None:
    store = make_store()
    llm1 = default_llm()
    r1 = make_runner(llm=llm1, store=store, fault=crash_at("after_commit", "research:sq1:0"))
    rid = r1.create_run("How efficient are solar panels today?")
    with pytest.raises(SimulatedCrash):
        r1.execute(rid)
    mid = store.load(rid)
    assert mid is not None and mid.plan is not None and "research:sq1:0" in mid.completed_nodes
    assert mid.status == "running" and rid in store.active_runs()

    llm2 = default_llm()  # fresh process: new LLM client, new runner, same Redis
    r2 = make_runner(llm=llm2, store=store)
    final = r2.execute(rid)
    assert final is not None and final.status == "completed"
    assert llm2.count("PlanOutput") == 0  # planner not re-run
    # sq1 was extracted before the crash, sq2 after: 2 sub-questions -> exactly 2 extraction calls in total
    assert (llm1.count("ExtractionOutput"), llm2.count("ExtractionOutput")) == (1, 1)
    keys = [s["node_key"] for s in store.trace(rid)]
    dupes = [k for k, n in Counter(keys).items() if n > 1]
    assert dupes == []  # every node ran exactly once across both processes
    events = [e for _, e in store.events(rid)]
    resumed = [e for e in events if e["type"] == "resumed"]
    assert resumed and "research:sq1:0" in resumed[0]["completed_nodes"]
    assert rid not in store.active_runs()


def test_crash_inside_node_reruns_only_that_node() -> None:
    store = make_store()
    r1 = make_runner(store=store, fault=crash_at("before_commit", "research:sq2:0"))
    rid = r1.create_run("How efficient are solar panels today?")
    with pytest.raises(SimulatedCrash):
        r1.execute(rid)
    st = store.load(rid)
    assert st is not None and "research:sq1:0" in st.completed_nodes and "research:sq2:0" not in st.completed_nodes
    llm2 = default_llm()
    final = make_runner(llm=llm2, store=store).execute(rid)
    assert final is not None and final.status == "completed"
    assert llm2.count("PlanOutput") == 0
    assert llm2.count("ExtractionOutput") == 1  # only sq2 (whose work was lost) is redone


def test_idempotent_node_reentry_returns_cached_result() -> None:
    store = make_store()
    llm = default_llm()
    runner = make_runner(llm=llm, store=store)
    rid = runner.create_run("How efficient are solar panels today?")
    state = store.load(rid)
    assert state is not None
    node = runner._durable("planner", "planner", __import__("research_assistant.graph", fromlist=["x"]).planner_node)
    first = node(state)
    second = node(state)  # same key 'planner' re-entered
    assert llm.count("PlanOutput") == 1
    assert RunState.model_validate(first).plan == RunState.model_validate(second).plan


def test_lease_prevents_two_workers_on_one_run() -> None:
    store = make_store()
    runner = make_runner(store=store)
    rid = runner.create_run("How efficient are solar panels today?")
    token = store.acquire_lease(rid, 30)
    assert token is not None
    assert runner.execute(rid) is None  # another worker holds it
    store.release_lease(rid, token)
    final = runner.execute(rid)
    assert final is not None and final.status == "completed"


def test_state_persisted_after_every_node() -> None:
    store = make_store()
    runner = make_runner(store=store)
    rid = runner.create_run("How efficient are solar panels today?")
    runner.execute(rid)
    trace = store.trace(rid)
    final = store.load(rid)
    assert final is not None
    assert [s["step"] for s in trace] == list(range(1, final.step + 1))
    for s in trace:
        snap = store.get_node(rid, s["node_key"])
        assert snap is not None and snap.step == s["step"]
