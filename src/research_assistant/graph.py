"""LangGraph state graph + durable runner (persist after every node, resume by re-routing from state)."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from research_assistant.agents import planner as planner_agent
from research_assistant.agents import researcher as researcher_agent
from research_assistant.agents import supervisor as supervisor_agent
from research_assistant.agents import writer as writer_agent
from research_assistant.agents.researcher import ResearchEnv
from research_assistant.config import Budgets, Models
from research_assistant.llm.base import LLMClient, LLMError
from research_assistant.runtime import AgentContext
from research_assistant.schemas import ResearchResult, Review, RunState, SubQState, utcnow
from research_assistant.spend import BudgetExceeded
from research_assistant.store import RunStore

log = logging.getLogger(__name__)

NodeFn = Callable[[RunState, AgentContext], tuple[RunState, str, str, str]]
FaultHook = Callable[[str, str], None]


def _trip(state: RunState, kind: str) -> None:
    if kind not in state.budget_tripped:
        state.budget_tripped.append(kind)


# ---------------------------------------------------------------------------- node bodies
# Each returns (new_state, input_summary, output_summary, status)


def planner_node(state: RunState, ctx: AgentContext) -> tuple[RunState, str, str, str]:
    inp = f"question={state.question[:200]!r}"
    try:
        plan = planner_agent.plan(ctx, state.question)
    except BudgetExceeded as exc:
        _trip(state, exc.kind)
        state.status, state.error = "failed", f"planner refused by budget: {exc}"
        return state, inp, state.error, "budget"
    except LLMError as exc:
        state.status, state.error = "failed", f"planner failed: {exc}"
        return state, inp, state.error, "error"
    state.plan = plan
    state.subqs = {sq.id: SubQState() for sq in plan.sub_questions}
    out = f"{len(plan.sub_questions)} sub-questions (proposed {plan.proposed_count}): " + " | ".join(
        sq.question for sq in plan.sub_questions
    )
    return state, inp, out, "ok"


def make_researcher_node(env: ResearchEnv) -> NodeFn:
    def researcher_node(state: RunState, ctx: AgentContext) -> tuple[RunState, str, str, str]:
        assert state.plan is not None and state.current_subq is not None
        subq = next(s for s in state.plan.sub_questions if s.id == state.current_subq)
        sq = state.subqs[subq.id]
        inp = f"{subq.id}: {subq.question[:200]} queries={sq.next_queries or subq.search_queries}"
        status = "ok"
        try:
            result = researcher_agent.research(ctx, env, state, subq, sq)
        except BudgetExceeded as exc:
            _trip(state, exc.kind)
            result = ResearchResult(
                sub_question_id=subq.id, attempt=sq.attempts, status="budget_exhausted", notes=str(exc)
            )
            status = "budget"
        except LLMError as exc:
            result = ResearchResult(sub_question_id=subq.id, attempt=sq.attempts, status="error", notes=str(exc)[:300])
            status = "error"
        sq.attempts += 1
        sq.last_result = result
        sq.status = "researched"
        sq.next_queries = []
        out = f"status={result.status} findings={len(result.findings)} " + (result.notes[:200] if result.notes else "")
        return state, inp, out, status

    return researcher_node


def reviewer_node(state: RunState, ctx: AgentContext) -> tuple[RunState, str, str, str]:
    assert state.plan is not None and state.current_subq is not None
    subq = next(s for s in state.plan.sub_questions if s.id == state.current_subq)
    sq = state.subqs[subq.id]
    res = sq.last_result
    assert res is not None
    has = any(f.sub_question_id == subq.id for f in state.findings)
    inp = f"{subq.id}: result={res.status} findings={len(res.findings)}"
    status = "ok"
    try:
        rv = supervisor_agent.review(ctx, state, subq, sq)
    except BudgetExceeded as exc:
        _trip(state, exc.kind)
        rv = Review(
            sub_question_id=subq.id,
            attempt=res.attempt,
            answered=has,
            missing=f"{exc.kind} budget",
            follow_up_query="",
            by_llm=False,
        )
        status = "budget"
    except LLMError as exc:
        rv = Review(
            sub_question_id=subq.id,
            attempt=res.attempt,
            answered=has,
            missing="review failed",
            follow_up_query="",
            by_llm=False,
        )
        status = "error"
        log.warning("review failed: %s", exc)
    sq.last_review = rv
    sq.status = "reviewed"
    out = f"answered={rv.answered} by_llm={rv.by_llm} missing={rv.missing[:120]!r} follow_up={rv.follow_up_query!r}"
    return state, inp, out, status


def make_supervisor_node(budgets: Budgets) -> NodeFn:
    def supervisor_node(state: RunState, ctx: AgentContext) -> tuple[RunState, str, str, str]:
        inp = ", ".join(f"{k}:{v.status}" for k, v in state.subqs.items()) or "no plan"
        new = supervisor_agent.route(state, budgets)
        out = f"next={new.next_node}" + (f" subq={new.current_subq}" if new.current_subq else "")
        if new.budget_tripped:
            out += f" budget_tripped={new.budget_tripped}"
        return new, inp, out, "ok"

    return supervisor_node


def writer_node(state: RunState, ctx: AgentContext) -> tuple[RunState, str, str, str]:
    inp = f"findings={len(state.findings)} subqs={len(state.subqs)}"
    report = writer_agent.write(ctx, state)
    state.report = report
    state.status = "completed"
    fc = report.first_draft_check
    out = (
        f"fallback={report.fallback} attempts={report.writer_attempts} citations_final={report.final_check.valid}/"
        f"{report.final_check.total}" + (f" first_draft={fc.valid}/{fc.total}" if fc else "")
    )
    return state, inp, out, "ok"


# ---------------------------------------------------------------------------- runner
class Runner:
    def __init__(
        self,
        *,
        store: RunStore,
        llm: LLMClient,
        models: Models,
        budgets: Budgets,
        env: ResearchEnv,
        lease_ttl_s: int = 15,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        fault: FaultHook | None = None,
    ) -> None:
        self.store = store
        self.llm = llm
        self.models = models
        self.budgets = budgets
        self.env = env
        self.lease_ttl_s = lease_ttl_s
        self.clock = clock
        self.sleep = sleep
        self.fault = fault
        self.graph = self._build()

    # -- graph ---------------------------------------------------------------
    def _build(self) -> Any:
        g: Any = StateGraph(RunState)  # Any: langgraph overloads reject wrapped callables under mypy
        g.add_node("supervisor", self._durable("supervisor", "supervisor", make_supervisor_node(self.budgets)))
        g.add_node("planner", self._durable("planner", "planner", planner_node))
        g.add_node("researcher", self._durable("researcher", "researcher", make_researcher_node(self.env)))
        g.add_node("reviewer", self._durable("reviewer", "supervisor/reviewer", reviewer_node))
        g.add_node("writer", self._durable("writer", "writer", writer_node))
        g.add_edge(START, "supervisor")
        g.add_conditional_edges(
            "supervisor",
            lambda s: s.next_node,
            {
                "planner": "planner",
                "researcher": "researcher",
                "reviewer": "reviewer",
                "writer": "writer",
                "end": END,
            },
        )
        g.add_edge("planner", "supervisor")
        g.add_edge("researcher", "reviewer")
        g.add_edge("reviewer", "supervisor")
        g.add_edge("writer", END)
        return g.compile()

    @staticmethod
    def node_key(node: str, s: RunState) -> str:
        if node == "researcher":
            return f"research:{s.current_subq}:{s.subqs[s.current_subq or ''].attempts}"
        if node == "reviewer":
            sq = s.subqs[s.current_subq or ""]
            att = sq.last_result.attempt if sq.last_result else sq.attempts
            return f"review:{s.current_subq}:{att}"
        if node == "supervisor":
            return f"supervisor:{s.step}"
        return node

    def _durable(self, node: str, agent: str, fn: NodeFn) -> Callable[[RunState], dict[str, Any]]:
        def wrapped(state: RunState) -> dict[str, Any]:
            key = self.node_key(node, state)
            cached = self.store.get_node(state.run_id, key)
            if cached is not None:  # idempotent re-entry: return the recorded result, do no work
                self.store.event(state.run_id, {"type": "node_cached", "node": node, "key": key})
                return {f: getattr(cached, f) for f in RunState.model_fields}
            b = self.budgets
            limit = (
                b.max_wall_clock_s if node == "writer" else b.max_wall_clock_s - supervisor_agent.WRITER_TIME_RESERVE_S
            )
            ctx = AgentContext(
                llm=self.llm,
                models=self.models,
                budgets=b,
                tokens_used=state.tokens_used,
                usd_spent=state.usd_spent,
                deadline=self.clock() + max(0.0, limit - state.elapsed_s),
                clock=self.clock,
                sleep=self.sleep,
            )
            started = utcnow()
            t0 = self.clock()
            new, inp, out, status = fn(state.model_copy(deep=True), ctx)
            latency = self.clock() - t0
            new.elapsed_s = state.elapsed_s + latency
            new.tokens_used = ctx.tokens_used
            new.usd_spent = ctx.usd_spent
            new.step = state.step + 1
            new.updated_at = utcnow()
            new.completed_nodes = [*state.completed_nodes, key]
            step = {
                "step": new.step,
                "node": node,
                "agent": agent,
                "node_key": key,
                "started_at": started.isoformat(),
                "latency_ms": round(latency * 1000, 1),
                "input": inp,
                "output": out,
                "status": status,
                "tool_calls": [t.model_dump() for t in ctx.tool_calls],
                "llm_calls": [c.model_dump() for c in ctx.llm_calls],
                "tokens": sum(c.input_tokens + c.output_tokens for c in ctx.llm_calls),
                "usd": round(sum(c.usd for c in ctx.llm_calls), 6),
                "pid": os.getpid(),
                "tokens_used_total": new.tokens_used,
                "elapsed_s_total": round(new.elapsed_s, 2),
            }
            event = {
                "type": "node_completed",
                "node": node,
                "key": key,
                "step": new.step,
                "status": status,
                "message": out[:300],
                "tokens_used": new.tokens_used,
            }
            if self.fault:
                self.fault("before_commit", key)
            self.store.commit_node(new, key, step, event)
            if self.fault:
                self.fault("after_commit", key)
            return {f: getattr(new, f) for f in RunState.model_fields}

        return wrapped

    # -- lifecycle -------------------------------------------------------------
    def create_run(self, question: str, owner: str = "") -> str:
        run_id = uuid.uuid4().hex[:16]
        self.store.create(RunState(run_id=run_id, question=question, owner=owner))
        return run_id

    def execute(self, run_id: str) -> RunState | None:
        token = self.store.acquire_lease(run_id, self.lease_ttl_s)
        if token is None:
            return None  # another worker owns this run
        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(self.lease_ttl_s / 3):
                self.store.renew_lease(run_id, token, self.lease_ttl_s)

        hb = threading.Thread(target=heartbeat, daemon=True)
        hb.start()
        try:
            state = self.store.load(run_id)
            if state is None:
                return None
            if state.status in ("completed", "failed"):
                self.store.finish(state)
                return state
            resumed = state.step > 0
            state.status = "running"
            self.store.save(state)
            self.store.event(
                run_id,
                {
                    "type": "resumed" if resumed else "started",
                    "step": state.step,
                    "pid": os.getpid(),
                    "completed_nodes": state.completed_nodes,
                },
            )
            if resumed:
                log.info(
                    "run %s resumed at step %d (pid %d); completed nodes: %s",
                    run_id,
                    state.step,
                    os.getpid(),
                    state.completed_nodes,
                )
            try:
                self.graph.invoke(state, config={"recursion_limit": 200})
                final = self.store.load(run_id) or state
                if final.report is None and final.status != "failed":
                    final.status, final.error = "failed", "graph ended without a report"
            except BudgetExceeded as exc:
                final = self.store.load(run_id) or state
                final.status, final.error = "failed", f"budget: {exc}"
            except (LLMError, ValueError, KeyError, RuntimeError) as exc:
                final = self.store.load(run_id) or state
                final.status, final.error = "failed", f"{type(exc).__name__}: {str(exc)[:300]}"
            self.store.finish(final)
            self.store.event(
                run_id,
                {
                    "type": final.status,
                    "step": final.step,
                    "tokens_used": final.tokens_used,
                    "usd": final.usd_spent,
                },
            )
            return final
        finally:
            stop.set()
            self.store.release_lease(run_id, token)
