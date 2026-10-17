"""API: auth, per-key rate limit, ownership, run lifecycle, trace and SSE."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from helpers import make_runner
from research_assistant.api import create_app
from research_assistant.config import Settings

KEY, OTHER = "test-key-aaaaaaaaaaaa", "test-key-bbbbbbbbbbbb"


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(api_keys=(KEY, OTHER), rate_limit_per_min=3)
    app = create_app(runner=make_runner(), settings=settings, start_workers=False)
    with TestClient(app) as c:
        yield c


def h(key: str = KEY) -> dict[str, str]:
    return {"X-API-Key": key}


def wait_done(c: TestClient, rid: str, key: str = KEY) -> dict:  # type: ignore[type-arg]
    for _ in range(100):
        body = c.get(f"/research/{rid}", headers=h(key)).json()
        if body["status"] in ("completed", "failed"):
            return body  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_auth_required(client: TestClient) -> None:
    assert client.post("/research", json={"question": "How efficient are solar panels?"}).status_code == 401
    assert (
        client.post("/research", json={"question": "How efficient are solar panels?"}, headers=h("wrong")).status_code
        == 401
    )


def test_fails_closed_without_configured_keys() -> None:
    app = create_app(runner=make_runner(), settings=Settings(api_keys=()), start_workers=False)
    with TestClient(app) as c:
        r = c.post("/research", json={"question": "How efficient are solar panels?"}, headers=h())
        assert r.status_code == 503


def test_full_lifecycle_report_trace_and_sse(client: TestClient) -> None:
    r = client.post("/research", json={"question": "How efficient are solar panels today?"}, headers=h())
    assert r.status_code == 202
    rid = r.json()["run_id"]
    body = wait_done(client, rid)
    assert body["status"] == "completed"
    assert body["report"]["citation_check"]["rate"] == 1.0
    assert body["findings"] and all({"claim", "url", "snippet", "retrieved_at"} <= set(f) for f in body["findings"])
    trace = client.get(f"/research/{rid}/trace", headers=h()).json()["steps"]
    agents = {s["agent"] for s in trace}
    assert {"planner", "researcher", "supervisor/reviewer", "writer", "supervisor"} <= agents
    for s in trace:
        assert {"input", "output", "tool_calls", "llm_calls", "tokens", "latency_ms"} <= set(s)
    with client.stream("GET", f"/research/{rid}/events", headers=h()) as resp:
        text = "".join(resp.iter_text())
    assert "event: node_completed" in text and "event: completed" in text


def test_runs_are_private_to_the_creating_key(client: TestClient) -> None:
    rid = client.post("/research", json={"question": "How efficient are solar panels today?"}, headers=h()).json()[
        "run_id"
    ]
    assert client.get(f"/research/{rid}", headers=h(OTHER)).status_code == 404
    assert client.get(f"/research/{rid}/trace", headers=h(OTHER)).status_code == 404
    assert client.get("/research/..%2Fetc", headers=h()).status_code == 404
    assert client.get("/research/ZZZZ", headers=h()).status_code == 404


def test_per_key_rate_limit(client: TestClient) -> None:
    codes = [
        client.post("/research", json={"question": f"Solar question number {i}?"}, headers=h()).status_code
        for i in range(4)
    ]
    assert codes[:3] == [202, 202, 202] and codes[3] == 429
    # another key has its own bucket
    assert client.post("/research", json={"question": "Solar question other key?"}, headers=h(OTHER)).status_code == 202


def test_input_validation(client: TestClient) -> None:
    assert client.post("/research", json={"question": "short"}, headers=h()).status_code == 422
    assert client.post("/research", json={"question": "x" * 501}, headers=h()).status_code == 422


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
