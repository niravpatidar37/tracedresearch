"""LLM client: retries honour Retry-After, spend cap refuses before the call, disk cache, ledger."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest

from research_assistant.llm.base import DeadlineExceeded, LLMBadOutput, LLMRequest, LLMResponse, RateLimited
from research_assistant.llm.cache import DiskCache
from research_assistant.llm.openai_compat import OpenAICompatClient
from research_assistant.llm.structured import strict_schema
from research_assistant.logging_utils import redact
from research_assistant.schemas import ReportDraft
from research_assistant.spend import BudgetExceeded, SpendLedger, cost_usd

OK = {
    "choices": [{"message": {"content": '{"a": 1}'}}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
}
REQ = LLMRequest(model="openai/gpt-oss-120b", system="s", user="u", max_tokens=100)


def ledger(tmp_path: Path, project_cap: float = 3.0, global_cap: float = 25.0) -> SpendLedger:
    return SpendLedger(tmp_path / "ledger.jsonl", "p2-research", global_cap, project_cap)


def client(
    handler,
    tmp_path: Path,
    led: SpendLedger | None = None,
    sleeps: list[float] | None = None,
    cache: bool = True,
) -> OpenAICompatClient:  # type: ignore[no-untyped-def]
    return OpenAICompatClient(
        provider="groq",
        base_url="https://api.test/v1",
        api_key="k",
        ledger=led or ledger(tmp_path),
        cache=DiskCache(tmp_path / "c") if cache else None,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
    )


def test_429_retry_after_is_honoured(tmp_path: Path) -> None:
    calls = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json=OK)

    sleeps: list[float] = []
    r = client(h, tmp_path, sleeps=sleeps).complete(REQ)
    assert r.text == '{"a": 1}' and len(calls) == 3
    assert all(2.0 <= s <= 2.5 for s in sleeps) and len(sleeps) == 2


def test_ledger_records_paid_rate_and_cache_makes_rerun_free(tmp_path: Path) -> None:
    n = []

    def h(req: httpx.Request) -> httpx.Response:
        n.append(1)
        return httpx.Response(200, json=OK)

    led = ledger(tmp_path)
    c = client(h, tmp_path, led)
    r1 = c.complete(REQ)
    r2 = c.complete(REQ)
    assert len(n) == 1 and r2.cached and r2.usd == 0
    lines = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["usd"] == pytest.approx(cost_usd("groq", "openai/gpt-oss-120b", 1000, 500))
    assert r1.usd == pytest.approx(1000 * 0.15 / 1e6 + 500 * 0.60 / 1e6)
    assert set(lines[0]) == {"ts", "project", "provider", "model", "input_tokens", "output_tokens", "usd"}


def test_cap_refuses_before_any_http_call(tmp_path: Path) -> None:
    led = ledger(tmp_path, project_cap=0.001)
    led.record("groq", "m", 0, 0, 0.00099)

    def h(req: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    with pytest.raises(BudgetExceeded) as ei:
        client(h, tmp_path, led, cache=False).complete(REQ)
    assert ei.value.kind == "project_usd"


def test_global_cap_counts_other_projects(tmp_path: Path) -> None:
    led = ledger(tmp_path, global_cap=0.01)
    other = SpendLedger(led.path, "p1-kgrag", 25, 5)
    other.record("groq", "m", 0, 0, 0.00999)
    with pytest.raises(BudgetExceeded) as ei:
        led.check(0.0001)
    assert ei.value.kind == "global_usd"


def test_deadline_stops_retry_wait(tmp_path: Path) -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "10"})

    c = client(h, tmp_path, cache=False)
    with pytest.raises(DeadlineExceeded):
        c.complete(REQ, deadline=c.clock() + 5)


def test_provider_schema_failure_is_typed(tmp_path: Path) -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "json_validate_failed", "message": "bad"}})

    with pytest.raises(LLMBadOutput):
        client(h, tmp_path, cache=False).complete(REQ)


def test_concurrent_ledger_writes_are_not_corrupted(tmp_path: Path) -> None:
    led = ledger(tmp_path)
    threads = [
        threading.Thread(target=lambda: [led.record("groq", "m", 1, 1, 0.0001) for _ in range(20)]) for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    assert len(lines) == 100 and all(json.loads(x)["usd"] == 0.0001 for x in lines)
    assert led.totals()[1] == pytest.approx(0.01)


def test_strict_schema_inlines_refs_and_requires_all() -> None:
    s = strict_schema(ReportDraft.model_json_schema())
    assert "$defs" not in json.dumps(s) and "$ref" not in json.dumps(s)
    assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
    assert s["properties"]["summary"]["additionalProperties"] is False


def test_log_redaction() -> None:
    out = redact("mail bob@example.com key gsk_ABCDEFGHIJKLMNOP1234 Bearer abcdefghijklmnop call 555-123-4567")
    assert "bob@" not in out and "gsk_" not in out and "abcdefghijklmnop" not in out and "555-123" not in out


def test_long_retry_after_raises_rate_limited(tmp_path: Path) -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "60"})

    sleeps: list[float] = []
    with pytest.raises(RateLimited) as ei:
        client(h, tmp_path, sleeps=sleeps, cache=False).complete(REQ)
    assert ei.value.retry_after == 60 and sleeps == []  # did not block for a minute


def test_big_model_rate_limit_fails_over_to_small_model() -> None:
    import time as _t

    from research_assistant.agents import planner
    from research_assistant.config import Budgets, Models
    from research_assistant.runtime import AgentContext

    class BigIsLimited:
        provider = "fake"

        def __init__(self) -> None:
            self.models: list[str] = []

        def complete(self, req: LLMRequest, *, deadline: float | None = None) -> LLMResponse:
            self.models.append(req.model)
            if req.model.endswith("120b"):
                raise RateLimited(90, "limited")
            text = json.dumps({"sub_questions": [{"question": "What is X?", "search_queries": ["x"]}]})
            return LLMResponse(text, req.model, "fake", 10, 10, 1.0)

    llm = BigIsLimited()
    ctx = AgentContext(
        llm=llm, models=Models(), budgets=Budgets(), tokens_used=0, usd_spent=0.0, deadline=_t.monotonic() + 60
    )
    plan = planner.plan(ctx, "What is X and why does it matter?")
    assert plan.sub_questions[0].question == "What is X?"
    assert llm.models == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    assert ctx.llm_calls[0].error.startswith("rate_limited") and ctx.llm_calls[1].model == "openai/gpt-oss-20b"


def test_strict_schema_rejection_retries_in_json_mode() -> None:
    import time as _t

    from research_assistant.config import Budgets, Models
    from research_assistant.runtime import AgentContext
    from research_assistant.schemas import ReportDraft

    seen: list[LLMRequest] = []

    class StrictRejects:
        provider = "fake"

        def complete(self, req: LLMRequest, *, deadline: float | None = None) -> LLMResponse:
            seen.append(req)
            if req.strict:
                raise LLMBadOutput("provider schema validation failed")
            # omits optional 'title' and 'limitations': Pydantic defaults accept it
            text = json.dumps({"summary": {"text": "s", "citations": ["F1"]}, "sections": []})
            return LLMResponse(text, req.model, "fake", 10, 10, 1.0)

    ctx = AgentContext(
        llm=StrictRejects(),
        models=Models(),
        budgets=Budgets(),
        tokens_used=0,
        usd_spent=0.0,
        deadline=_t.monotonic() + 60,
    )
    out = ctx.structured(role="writer", model="m", system="sys", user="u", schema=ReportDraft, max_tokens=100)
    assert out.title == "Research report"
    assert [r.strict for r in seen] == [True, False]
    assert "JSON Schema of the required output" in seen[1].system


def test_fallback_also_limited_waits_within_deadline_then_succeeds() -> None:
    import time as _t

    from research_assistant.config import Budgets, Models
    from research_assistant.runtime import AgentContext
    from research_assistant.schemas import PlanOutput

    calls: list[str] = []

    class BothLimitedOnce:
        provider = "fake"

        def complete(self, req: LLMRequest, *, deadline: float | None = None) -> LLMResponse:
            calls.append(req.model)
            if req.model.endswith("120b"):
                raise RateLimited(300, "big limited")
            if calls.count(req.model) == 1:
                raise RateLimited(25, "small limited")
            return LLMResponse(json.dumps({"sub_questions": []}), req.model, "fake", 5, 5, 1.0)

    slept: list[float] = []
    ctx = AgentContext(
        llm=BothLimitedOnce(),
        models=Models(),
        budgets=Budgets(),
        tokens_used=0,
        usd_spent=0.0,
        deadline=_t.monotonic() + 120,
        sleep=slept.append,
    )
    ctx.structured(
        role="planner",
        model="openai/gpt-oss-120b",
        system="s",
        user="u",
        schema=PlanOutput,
        max_tokens=50,
        fallback_model="openai/gpt-oss-20b",
    )
    assert calls == ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "openai/gpt-oss-20b"] and slept == [25]
