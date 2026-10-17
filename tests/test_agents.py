"""Each specialist tested alone with recorded / fake LLM responses (no network)."""

from __future__ import annotations

import time

import pytest

from helpers import MODELS, corpus, default_llm, extraction_responder, plan_responder, writer_responder
from research_assistant.agents import planner, researcher, supervisor, writer
from research_assistant.agents.researcher import ResearchEnv
from research_assistant.config import Budgets
from research_assistant.llm.fake import FakeLLM
from research_assistant.runtime import AgentContext, StructuredOutputError
from research_assistant.schemas import (
    Finding,
    Plan,
    PlanOutput,
    ResearchResult,
    RunState,
    SearchStatus,
    SubQState,
    SubQuestion,
    utcnow,
)
from research_assistant.search.providers import SearchOutcome


def ctx(llm: FakeLLM, budgets: Budgets | None = None) -> AgentContext:
    return AgentContext(
        llm=llm,
        models=MODELS,
        budgets=budgets or Budgets(),
        tokens_used=0,
        usd_spent=0.0,
        deadline=time.monotonic() + 600,
        sleep=lambda s: None,
    )


# ------------------------------------------------------------------ planner
def test_planner_returns_typed_plan_with_big_model_and_no_persona() -> None:
    llm = default_llm()
    p = planner.plan(ctx(llm), "How efficient are modern solar panels?")
    assert isinstance(p, Plan)
    assert [s.id for s in p.sub_questions] == ["sq1", "sq2"]
    assert all(s.search_queries for s in p.sub_questions)
    call = llm.calls[0]
    assert call.model == MODELS.planner == "openai/gpt-oss-120b"
    assert "you are" not in call.system.lower()  # schema-defined specialist, not a persona
    assert call.json_schema is not None and call.json_schema["additionalProperties"] is False


def test_planner_cap_is_enforced_in_code_not_prompt() -> None:
    out = PlanOutput.model_validate(plan_responder(9)(None))  # type: ignore[arg-type]
    p = planner.build_plan("q" * 20, out, max_sub_questions=4)
    assert len(p.sub_questions) == 4
    assert p.proposed_count == 9


def test_planner_dedupes_and_falls_back() -> None:
    out = PlanOutput.model_validate(
        {
            "sub_questions": [
                {"question": "Same question?", "search_queries": []},
                {"question": "same   QUESTION?", "search_queries": ["x"]},
            ]
        }
    )
    p = planner.build_plan("original question here", out, 4)
    assert len(p.sub_questions) == 1 and p.sub_questions[0].search_queries == ["Same question?"]
    empty = planner.build_plan("original question here", PlanOutput(sub_questions=[]), 4)
    assert empty.sub_questions[0].question == "original question here"


def test_structured_output_repairs_once_then_fails() -> None:
    llm = FakeLLM({"PlanOutput": ["not json", plan_responder(2)(None)]})  # type: ignore[arg-type]
    assert len(planner.plan(ctx(llm), "question about solar panels").sub_questions) == 2
    assert llm.count("PlanOutput") == 2
    bad = FakeLLM({"PlanOutput": ["nope", '{"wrong": 1}', "never reached"]})
    with pytest.raises(StructuredOutputError):
        planner.plan(ctx(bad), "question about solar panels")
    assert bad.count("PlanOutput") == 2  # exactly one repair attempt


# ------------------------------------------------------------------ researcher
def _state_with(subq: SubQuestion) -> RunState:
    st = RunState(run_id="r1", question="q")
    st.plan = Plan(question="q", sub_questions=[subq])
    st.subqs = {subq.id: SubQState()}
    return st


SQ = SubQuestion(
    id="sq1",
    question="What is the record efficiency of solar cells?",
    search_queries=["solar efficiency record", "perovskite tandem efficiency"],
)


def test_researcher_findings_carry_provenance_and_verbatim_snippets() -> None:
    c = corpus()
    st = _state_with(SQ)
    res = researcher.research(ctx(default_llm()), ResearchEnv(c, c), st, SQ, st.subqs["sq1"])
    assert res.status == "ok" and res.findings
    for f in res.findings:
        assert f.retrieved_at is not None
        page_text = " ".join(c.fetch(f.url).text.split()).lower()
        assert page_text and f.snippet.lower() in page_text  # snippet really is on the fetched page
    assert st.findings == res.findings
    assert st.findings[0].id == "F1"


def test_researcher_drops_fabricated_snippets() -> None:
    def liar(req):  # type: ignore[no-untyped-def]
        return {
            "findings": [
                {
                    "page_id": "P1",
                    "claim": "Solar is 99% efficient",
                    "snippet": "Solar panels are ninety-nine percent efficient in all conditions.",
                },
                {"page_id": "P9", "claim": "x", "snippet": "y" * 40},
            ]
        }

    c = corpus()
    st = _state_with(SQ)
    res = researcher.research(ctx(default_llm(ExtractionOutput=liar)), ResearchEnv(c, c), st, SQ, st.subqs["sq1"])
    assert res.findings == [] and res.status == "no_results"
    assert "rejected" in res.notes


def test_researcher_dedupes_urls_and_caps_per_domain() -> None:
    c = corpus()
    st = _state_with(SQ)
    st.seen_urls = ["https://energy.example.org/solar-efficiency"]
    b = Budgets(max_results_per_domain=1, max_pages_per_subq=5)
    researcher.research(ctx(default_llm(), b), ResearchEnv(c, c), st, SQ, st.subqs["sq1"])
    fetched = [u for u in st.seen_urls[1:]]
    assert "https://energy.example.org/solar-efficiency" not in fetched  # already seen -> not refetched
    domains = [u.split("/")[2] for u in fetched]
    assert domains.count("news.example.com") <= 1


def test_researcher_respects_search_budget() -> None:
    calls: list[str] = []

    class Counting:
        name = "count"

        def search(self, q: str, max_results: int) -> SearchOutcome:
            calls.append(q)
            return SearchOutcome(SearchStatus.no_results, q, "count")

    st = _state_with(SQ)
    res = researcher.research(
        ctx(default_llm(), Budgets(max_searches_per_subq=1)),
        ResearchEnv(Counting(), corpus()),
        st,
        SQ,
        st.subqs["sq1"],
    )
    assert len(calls) == 1 and res.status == "no_results"


@pytest.mark.parametrize("status", [SearchStatus.rate_limited, SearchStatus.timeout, SearchStatus.blocked])
def test_researcher_turns_search_failures_into_statuses(status: SearchStatus) -> None:
    class Failing:
        name = "fail"

        def search(self, q: str, max_results: int) -> SearchOutcome:
            return SearchOutcome(status, q, "fail")

    st = _state_with(SQ)
    llm = default_llm()
    res = researcher.research(ctx(llm), ResearchEnv(Failing(), corpus()), st, SQ, st.subqs["sq1"])
    assert res.status == status.value
    assert llm.count("ExtractionOutput") == 0  # no LLM spend without pages


def test_researcher_paywalled_pages_status() -> None:
    c = corpus(
        [
            {
                "url": "https://paywall.example/a",
                "title": "solar efficiency record",
                "html": "solar",
                "status": "paywalled",
            }
        ]
    )
    only = type(c)([p for p in c.pages if p["url"].startswith("https://paywall")])
    st = _state_with(SQ)
    res = researcher.research(ctx(default_llm()), ResearchEnv(only, only), st, SQ, st.subqs["sq1"])
    assert res.status == "paywalled"


def test_researcher_uses_small_model_and_delimits_page_text() -> None:
    llm = default_llm(ExtractionOutput=extraction_responder)
    c = corpus()
    st = _state_with(SQ)
    researcher.research(ctx(llm), ResearchEnv(c, c), st, SQ, st.subqs["sq1"])
    call = next(x for x in llm.calls if x.schema_name == "ExtractionOutput")
    assert call.model == "openai/gpt-oss-20b"
    assert "<<<PAGE id=P1" in call.user and "<<<END PAGE>>>" in call.user
    assert "untrusted" in call.system.lower()


# ------------------------------------------------------------------ supervisor / reviewer
def _finding(fid: str, sq: str = "sq1") -> Finding:
    return Finding(
        id=fid,
        sub_question_id=sq,
        claim="c",
        url="https://a.example/x",
        snippet="s" * 30,
        retrieved_at=utcnow(),
    )


def test_reviewer_is_deterministic_on_failure_status() -> None:
    llm = default_llm()
    st = _state_with(SQ)
    sq = st.subqs["sq1"]
    sq.last_result = ResearchResult(
        sub_question_id="sq1", attempt=0, status="rate_limited", queries=["solar efficiency record"]
    )
    rv = supervisor.review(ctx(llm), st, SQ, sq)
    assert rv.by_llm is False and not rv.answered
    assert rv.follow_up_query == "perovskite tandem efficiency"  # unused planner query
    assert llm.calls == []


def test_reviewer_llm_path_uses_small_model() -> None:
    llm = default_llm()
    st = _state_with(SQ)
    st.findings = [_finding("F1")]
    sq = st.subqs["sq1"]
    sq.last_result = ResearchResult(sub_question_id="sq1", attempt=0, status="ok", findings=st.findings)
    rv = supervisor.review(ctx(llm), st, SQ, sq)
    assert rv.answered and rv.by_llm
    assert llm.calls[0].model == "openai/gpt-oss-20b"


def test_route_sequence() -> None:
    b = Budgets()
    st = RunState(run_id="r", question="q")
    assert supervisor.route(st, b).next_node == "planner"
    st = _state_with(SQ)
    assert supervisor.route(st, b).next_node == "researcher"
    st.subqs["sq1"].status = "researched"
    assert supervisor.route(st, b).next_node == "reviewer"
    st.subqs["sq1"].status = "answered"
    assert supervisor.route(st, b).next_node == "writer"


# ------------------------------------------------------------------ writer
def _writer_state() -> RunState:
    st = _state_with(SQ)
    st.findings = [_finding("F1"), _finding("F2")]
    st.subqs["sq1"].status = "answered"
    return st


def test_writer_produces_validated_citations_with_big_model() -> None:
    llm = default_llm()
    rep = writer.write(ctx(llm), _writer_state())
    assert not rep.fallback and rep.final_check.rate == 1.0 and rep.final_check.total > 0
    assert rep.first_draft_check is not None and rep.first_draft_check.rate == 1.0
    assert llm.calls[0].model == "openai/gpt-oss-120b"
    assert "[^1]" in rep.markdown and "[^1]: [" in rep.markdown


def test_writer_regenerates_once_on_bad_citations_then_drops_them() -> None:
    bad = {
        "title": "t",
        "summary": {"text": "s", "citations": ["F1"]},
        "sections": [
            {
                "heading": "h",
                "paragraphs": [
                    {"text": "invented", "citations": ["F42"]},
                    {"text": "ok", "citations": ["F2"]},
                ],
            }
        ],
        "limitations": [],
    }
    llm = default_llm(ReportDraft=[bad, bad, bad])
    rep = writer.write(ctx(llm), _writer_state())
    assert llm.count("ReportDraft") == 2  # one regeneration, never more
    assert rep.first_draft_check is not None and rep.first_draft_check.invalid_ids == ["F42"]
    assert "invented" not in rep.markdown and "ok" in rep.markdown
    assert rep.final_check.rate == 1.0


def test_writer_fallback_without_findings_or_budget() -> None:
    st = _state_with(SQ)
    rep = writer.write(ctx(default_llm()), st)
    assert rep.fallback
    tiny = Budgets(max_total_tokens=100)
    rep2 = writer.write(ctx(default_llm(ReportDraft=writer_responder), tiny), _writer_state())
    assert rep2.fallback and rep2.final_check.rate == 1.0
