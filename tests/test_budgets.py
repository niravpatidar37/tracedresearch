"""Every budget trips, and is enforced by graph code (not prompts)."""

from __future__ import annotations

from collections import Counter

from helpers import default_llm, make_runner, plan_responder, review_reject
from research_assistant.config import Budgets
from research_assistant.llm.fake import FakeLLM


def run(runner, q: str = "How efficient are solar panels today?"):  # type: ignore[no-untyped-def]
    rid = runner.create_run(q)
    final = runner.execute(rid)
    assert final is not None
    return final, runner.store.trace(rid)


def test_max_sub_questions() -> None:
    llm = default_llm(PlanOutput=plan_responder(7))
    final, trace = run(make_runner(llm=llm, budgets=Budgets(max_sub_questions=3)))
    assert final.plan is not None and len(final.plan.sub_questions) == 3 and final.plan.proposed_count == 7
    researched = {s["node_key"].split(":")[1] for s in trace if s["node"] == "researcher"}
    assert researched <= {"sq1", "sq2", "sq3"}


def test_send_back_exactly_once_per_sub_question() -> None:
    llm = default_llm(ReviewOutput=review_reject)  # reviewer never satisfied
    final, trace = run(make_runner(llm=llm, budgets=Budgets(max_searches_per_subq=5)))
    assert final.status == "completed"
    for sq in final.subqs.values():
        assert sq.send_backs == 1 and sq.attempts == 2
    attempts = Counter(s["node_key"].split(":")[1] for s in trace if s["node"] == "researcher")
    assert set(attempts.values()) == {2}


def test_max_searches_per_sub_question() -> None:
    llm = default_llm(ReviewOutput=review_reject)
    final, trace = run(make_runner(llm=llm, budgets=Budgets(max_searches_per_subq=1)))
    per_sq: Counter[str] = Counter()
    for s in trace:
        if s["node"] == "researcher":
            per_sq[s["node_key"].split(":")[1]] += sum(1 for t in s["tool_calls"] if t["tool"] == "search")
    assert per_sq and max(per_sq.values()) == 1
    assert all(sq.send_backs == 0 for sq in final.subqs.values())  # no budget left for a send-back


def test_max_total_tokens() -> None:
    llm = default_llm()
    llm.output_tokens = 1500  # every call is expensive
    b = Budgets(max_total_tokens=12_000, writer_token_reserve=5_000)
    final, _ = run(make_runner(llm=llm, budgets=b))
    assert "tokens" in final.budget_tripped
    assert final.tokens_used <= b.max_total_tokens
    assert final.report is not None and final.report.final_check.rate == 1.0


def test_wall_clock_ceiling() -> None:
    t = [0.0]

    def clock() -> float:
        t[0] += 20.0  # each clock read = 20 simulated seconds
        return t[0]

    b = Budgets(max_wall_clock_s=120.0)
    final, trace = run(make_runner(budgets=b, clock=clock))
    assert "wall_clock" in final.budget_tripped
    assert final.status == "completed" and final.report is not None
    assert len([s for s in trace if s["node"] == "researcher"]) < 4


def test_run_usd_cap_refuses_before_calling() -> None:
    llm = default_llm()
    llm.provider = "groq"  # priced at Groq paid rates for the estimate
    final, _ = run(make_runner(llm=llm, budgets=Budgets(max_run_usd=0.0001)))
    assert final.status == "failed" and "run_usd" in final.budget_tripped
    assert llm.calls == []  # refused before any call


def test_routing_budget_checks_are_code_not_prompt() -> None:
    llm = FakeLLM(
        {
            "PlanOutput": plan_responder(2),
            "ExtractionOutput": [{"findings": []}],
            "ReviewOutput": review_reject,
            "ReportDraft": [{}],
        }
    )
    final, _ = run(make_runner(llm=llm))
    for call in llm.calls:
        assert "budget" not in call.system.lower()
    assert final.status == "completed"
