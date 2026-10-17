"""LLM client: retries honour Retry-After, spend cap refuses before the call, disk cache, ledger."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest

from research_assistant.llm.base import DeadlineExceeded, LLMBadOutput, LLMRequest
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
        return httpx.Response(429, headers={"retry-after": "30"})

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
