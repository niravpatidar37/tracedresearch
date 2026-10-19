"""Production hardening: HTTP headers, body limits, no stack traces, fake-LLM gate, fixture SSRF guard,
honest writer degradation, the queue-depth scaler, and the smoke script itself against a live server."""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from helpers import corpus, default_llm, make_runner, make_store
from research_assistant import migrations
from research_assistant.agents.researcher import ResearchEnv
from research_assistant.api import create_app
from research_assistant.config import Budgets, Settings
from research_assistant.fetch.fetcher import SafeFetcher
from research_assistant.llm.base import LLMError
from research_assistant.llm.factory import make_llm
from research_assistant.llm.fake import FakeLLM
from research_assistant.scaler import QueueStats, WorkerPoolScaler, desired_instances, queue_stats, run_once
from research_assistant.search.providers import FixtureCorpus
from research_assistant.store import KILL_SWITCH_KEY

ROOT = Path(__file__).resolve().parents[1]
SMOKE_CORPUS = ROOT / "fixtures" / "smoke_corpus.json"
KEY = "test-key-aaaaaaaaaaaa"
Q = "How efficient are silicon and perovskite solar cells today?"


def h() -> dict[str, str]:
    return {"X-API-Key": KEY}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(runner=make_runner(), settings=Settings(api_keys=(KEY,)), start_workers=False)
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------------------------------- HTTP hardening
@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("GET", "/healthz", None, 200),
        ("POST", "/research", {"question": Q}, 401),
        ("GET", "/research/0123456789abcdef", None, 404),
        ("GET", "/nope", None, 404),
    ],
)
def test_security_headers_on_every_response(
    client: TestClient, method: str, path: str, body: dict[str, str] | None, status: int
) -> None:
    headers = h() if path.startswith("/research/") else {}
    r = client.request(method, path, json=body, headers=headers)
    assert r.status_code == status
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"


def test_interactive_docs_are_not_exposed(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_oversize_body_rejected_with_413_by_content_length(client: TestClient) -> None:
    r = client.post("/research", content=json.dumps({"question": "y" * 50_000}), headers=h())
    assert r.status_code == 413
    assert r.json()["detail"].startswith("request body too large")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_oversize_chunked_body_rejected_with_413(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(40):
            yield b"x" * 1024

    r = client.post("/research", content=chunks(), headers=h() | {"content-type": "application/json"})
    assert r.status_code == 413


def test_declared_content_length_over_limit_is_rejected_before_reading_the_app_body() -> None:
    """Content-Length alone decides: a client announcing 1 MB is refused even if it sends little."""
    import asyncio

    from research_assistant.http_security import BodySizeLimitMiddleware

    reached_app: list[bool] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached_app.append(True)

    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "headers": [(b"content-length", b"1048576")]}
    asyncio.run(BodySizeLimitMiddleware(app, max_bytes=16 * 1024)(scope, receive, send))
    assert not reached_app and sent[0]["status"] == 413


def test_small_chunked_body_is_replayed_to_the_app(client: TestClient) -> None:
    payload = json.dumps({"question": Q}).encode()
    headers = h() | {"content-type": "application/json"}
    r = client.post("/research", content=iter([payload[:10], payload[10:]]), headers=headers)
    assert r.status_code == 202


def test_unhandled_error_returns_json_500_without_stack_trace() -> None:
    app = create_app(runner=make_runner(), settings=Settings(api_keys=(KEY,)), start_workers=False)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail at /srv/app.py line 3")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500
    assert r.json() == {"detail": "internal server error"}
    assert "Traceback" not in r.text and "secret internal detail" not in r.text
    assert r.headers["x-content-type-options"] == "nosniff"


# ------------------------------------------------------------------------------------------- fake LLM gate
@pytest.mark.parametrize("env", ["prod", "staging"])
def test_fake_llm_refused_in_prod_and_staging(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="LLM_PROVIDER=fake is refused"):
        Settings(llm_provider="fake", env=env)
    monkeypatch.setenv("PORTFOLIO_ENV_FILE", "does-not-exist.env")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("ENV", env)
    with pytest.raises(ValueError, match="refused"):
        Settings.from_env()


def test_make_llm_rechecks_the_gate_even_if_settings_were_tampered() -> None:
    s = Settings(llm_provider="fake", env="ci")
    object.__setattr__(s, "env", "prod")  # bypass __post_init__, as a hand-built object could
    with pytest.raises(LLMError, match="refused"):
        make_llm(s)


def test_unknown_env_value_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTFOLIO_ENV_FILE", "does-not-exist.env")
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(ValueError, match="ENV="):
        Settings.from_env()


def test_fake_llm_allowed_in_ci_and_needs_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    llm, _ = make_llm(Settings(llm_provider="fake", env="ci"))
    assert isinstance(llm, FakeLLM)


def test_smoke_key_is_accepted_alongside_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTFOLIO_ENV_FILE", "does-not-exist.env")
    main_key, smoke_key = "a" * 20, "s" * 20  # low-entropy test values (gitleaks flags realistic-looking keys)
    monkeypatch.setenv("RESEARCH_API_KEYS", main_key)
    monkeypatch.setenv("RESEARCH_SMOKE_API_KEY", smoke_key)
    assert Settings.from_env().api_keys == (main_key, smoke_key)


# ------------------------------------------------------------------------------------------- fixture SSRF guard
@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://127.0.0.1/admin", "non_public_address:127.0.0.1"),
        ("http://localhost/admin", "non_public_address:127.0.0.1"),
        ("http://169.254.169.254/computeMetadata/v1/", "non_public_address:169.254.169.254"),
        ("http://metadata.google.internal/", "non_public_address:169.254.169.254"),
        ("http://10.20.8.3/", "non_public_address:10.20.8.3"),
        ("http://[::1]/", "non_public_address:::1"),
        ("http://news.example.com:6379/", "port_not_allowed:6379"),
        ("file:///etc/passwd", "scheme_not_allowed:file"),
    ],
)
def test_fixture_fetch_applies_the_ssrf_guard(url: str, reason: str) -> None:
    c = FixtureCorpus([{"url": url, "title": "t", "html": "<p>" + "solar " * 80 + "</p>"}])
    out = c.fetch(url)
    assert out.status == "blocked" and out.detail == reason and not out.text


def test_fixture_fetch_still_serves_public_pages() -> None:
    assert corpus().fetch("https://news.example.com/perovskite").status == "ok"


def test_smoke_corpus_run_blocks_loopback_and_never_cites_it() -> None:
    fx = FixtureCorpus.from_file(SMOKE_CORPUS)
    runner = make_runner(env=ResearchEnv(search=fx, fetcher=fx))
    rid = runner.create_run(Q)
    final = runner.execute(rid)
    assert final is not None and final.status == "completed" and final.report is not None
    fetches = [t for s in runner.store.trace(rid) for t in s["tool_calls"] if t["tool"] == "fetch"]
    internal = [t for t in fetches if t["input"].startswith(("http://127.", "http://169.254."))]
    assert internal and all(t["status"] == "blocked" for t in internal)
    assert all(not f.url.startswith(("http://127.", "http://169.254.")) for f in final.findings)
    assert final.report.final_check.rate == 1.0 and final.report.citations


def test_safe_fetcher_and_fixture_agree_on_loopback() -> None:
    """The fixture guard is the real guard: SafeFetcher blocks the same URL for the same reason."""
    fx = FixtureCorpus([])
    real = SafeFetcher(resolver=lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    assert fx.fetch("http://127.0.0.1/admin").detail == real.fetch("http://127.0.0.1/admin").detail


# ------------------------------------------------------------------------------------------- honest degradation
class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_writer_failure_degrades_honestly_and_is_labelled() -> None:
    def broken_writer(req: Any) -> str:
        return "not json at all"

    runner = make_runner(llm=default_llm(ReportDraft=broken_writer))
    app = create_app(runner=runner, settings=Settings(api_keys=(KEY,)), start_workers=False)
    graph_log = logging.getLogger("research_assistant.graph")  # setup_logging() replaces only root handlers
    handler = _ListHandler()
    graph_log.addHandler(handler)
    with TestClient(app) as c:
        rid = c.post("/research", json={"question": Q}, headers=h()).json()["run_id"]
        for _ in range(200):
            body = c.get(f"/research/{rid}", headers=h()).json()
            if body["status"] in ("completed", "failed"):
                break
            time.sleep(0.02)
    rep = body["report"]
    assert body["status"] == "completed"
    assert rep["degraded"] is True and rep["fallback"] is True
    assert rep["fallback_reason"] == "writer_output_invalid"
    assert "degraded" in rep["title"].lower()
    assert rep["citation_check"]["rate"] == 1.0  # degraded, but still fully grounded
    graph_log.removeHandler(handler)
    finished = [m for m in handler.messages if m.startswith("run_finished")]
    assert finished and "fallback=True fallback_reason=writer_output_invalid" in finished[-1]


def test_normal_report_is_not_marked_degraded(client: TestClient) -> None:
    rid = client.post("/research", json={"question": Q}, headers=h()).json()["run_id"]
    for _ in range(200):
        body = client.get(f"/research/{rid}", headers=h()).json()
        if body["status"] == "completed":
            break
        time.sleep(0.02)
    assert body["report"]["degraded"] is False and body["report"]["fallback_reason"] is None


# ------------------------------------------------------------------------------------------- scaler
@pytest.mark.parametrize(
    ("active", "depth", "current", "want"),
    [
        (0, 0, 1, 1),  # floor
        (5, 5, 1, 3),  # ceil(5/2)
        (40, 40, 1, 4),  # ceiling
        (0, 0, 4, 3),  # scale in one step at a time
        (3, 0, 4, 3),
        (1, 7, 1, 4),  # queue depth counts even before runs are registered as active
    ],
)
def test_desired_instances(active: int, depth: int, current: int, want: int) -> None:
    st = QueueStats(queue_depth=depth, active_runs=active, queued_runs=depth, oldest_queued_age_s=0)
    assert desired_instances(st, runs_per_instance=2, current=current, min_n=1, max_n=4) == want


def test_desired_instances_rejects_bad_bounds() -> None:
    st = QueueStats(0, 0, 0, 0)
    with pytest.raises(ValueError):
        desired_instances(st, runs_per_instance=2, current=1, min_n=3, max_n=2)


def test_queue_stats_reports_depth_and_oldest_age() -> None:
    from research_assistant.graph import new_run
    from research_assistant.schemas import utcnow

    store = make_store()
    old = new_run(store, "an older question here?", enqueue=True)
    st = store.load(old)
    assert st is not None
    st.updated_at = utcnow().replace(microsecond=0) - __import__("datetime").timedelta(seconds=120)
    store.save(st)
    new_run(store, "a newer question here?", enqueue=True)
    qs = queue_stats(store)
    assert qs.queue_depth == 2 and qs.active_runs == 2 and qs.queued_runs == 2
    assert 119 <= qs.oldest_queued_age_s < 200
    assert qs.log_line().startswith("queue_stats queue_depth=2 ")


class FakeRunAPI:
    def __init__(self, count: int) -> None:
        self.count = count
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer tok"
        if request.method == "GET":
            body: dict[str, Any] = {"scaling": {"manualInstanceCount": self.count}} if self.count else {"scaling": {}}
            return httpx.Response(200, json=body)
        assert request.method == "PATCH"
        assert request.url.params["updateMask"] == "scaling.manualInstanceCount"
        self.count = json.loads(request.content)["scaling"]["manualInstanceCount"]
        return httpx.Response(200, json={"name": "operations/x"})


POOL = "projects/p/locations/us-central1/workerPools/research-prod-worker"


def _scaler(api: FakeRunAPI) -> WorkerPoolScaler:
    return WorkerPoolScaler(POOL, httpx.Client(transport=httpx.MockTransport(api)), lambda: "tok")


def test_scaler_patches_manual_instance_count_via_admin_api() -> None:
    from research_assistant.graph import new_run

    store = make_store()
    for i in range(5):
        new_run(store, f"queued question number {i}?", enqueue=True)
    api = FakeRunAPI(count=1)
    out = run_once(store, _scaler(api), runs_per_instance=2, min_n=1, max_n=4, kill_switch=False)
    assert out["action"] == "scaled" and out["desired_instances"] == 3 and api.count == 3
    patch = api.requests[-1]
    assert patch.url.path == f"/v2/{POOL}" and patch.url.host == "run.googleapis.com"


def test_scaler_reads_omitted_zero_count_and_does_not_patch_when_unchanged() -> None:
    api = FakeRunAPI(count=0)
    out = run_once(make_store(), _scaler(api), runs_per_instance=2, min_n=0, max_n=4, kill_switch=False)
    assert out["current_instances"] == 0 and out["action"] == "none"
    assert [r.method for r in api.requests] == ["GET"]


def test_scaler_never_scales_up_while_kill_switch_is_on() -> None:
    from research_assistant.graph import new_run

    store = make_store()
    for i in range(6):
        new_run(store, f"queued question number {i}?", enqueue=True)
    api = FakeRunAPI(count=1)
    out = run_once(store, _scaler(api), runs_per_instance=2, min_n=1, max_n=4, kill_switch=True)
    assert out["action"] == "none" and api.count == 1


def test_scaler_dry_run_does_not_patch() -> None:
    from research_assistant.graph import new_run

    store = make_store()
    for i in range(6):
        new_run(store, f"queued question number {i}?", enqueue=True)
    api = FakeRunAPI(count=1)
    out = run_once(store, _scaler(api), runs_per_instance=2, min_n=1, max_n=4, kill_switch=False, dry_run=True)
    assert out["action"] == "dry_run" and api.count == 1


def test_scaler_rejects_non_worker_pool_names() -> None:
    with pytest.raises(ValueError):
        WorkerPoolScaler("projects/p/locations/l/services/x", httpx.Client(), lambda: "t")


# ------------------------------------------------------------------------------------------- deploy rollback target
@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed (present on the CI runner)")
@pytest.mark.parametrize(
    ("payload", "want"),
    [
        (
            {
                "instanceSplitStatuses": [
                    {"type": "INSTANCE_SPLIT_ALLOCATION_TYPE_LATEST", "revision": "w-7", "percent": 100}
                ],
                "latestReadyRevision": "projects/p/locations/r/workerPools/w/revisions/w-9",
            },
            "w-7",
        ),
        ({"instanceSplitStatuses": [{"revision": "w-1", "percent": 90}, {"revision": "w-2", "percent": 10}]}, "w-1"),
        ({"latestReadyRevision": "projects/p/locations/r/workerPools/w/revisions/w-3"}, "w-3"),
        ({}, ""),
    ],
)
def test_worker_pool_rollback_target_jq(payload: dict[str, Any], want: str) -> None:
    out = subprocess.run(
        ["jq", "-r", "-f", str(ROOT / "scripts" / "worker_pool_revision.jq")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == want


# ------------------------------------------------------------------------------------------- smoke script
def _load_smoke() -> Any:
    spec = importlib.util.spec_from_file_location("smoke", ROOT / "scripts" / "smoke.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["smoke"] = mod
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live_server() -> Iterator[str]:
    fx = FixtureCorpus.from_file(SMOKE_CORPUS)
    runner = make_runner(env=ResearchEnv(search=fx, fetcher=fx), budgets=Budgets())
    migrations.migrate(runner.store.r)  # the deploy step; /readyz refuses an unmigrated keyspace
    app = create_app(runner=runner, settings=Settings(api_keys=(KEY,), rate_limit_per_min=8), start_workers=True)
    port = _free_port()
    # server_header=False mirrors the Dockerfile's --no-server-header (the smoke test fails on a uvicorn banner)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", server_header=False)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=10)


def test_smoke_script_passes_end_to_end_against_a_live_server(
    live_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = _load_smoke()
    monkeypatch.setenv("SMOKE_API_KEY", KEY)
    out = tmp_path / "smoke.json"
    rc = smoke.main(["--base-url", live_server, "--expect-ssrf-fixture", "--json-out", str(out), "--wait-ready", "10"])
    summary = json.loads(out.read_text(encoding="utf-8"))
    by_name = {c["name"]: c for c in summary["checks"]}
    assert rc == 0, [c for c in summary["checks"] if c["status"] == "FAIL"]
    for name in (
        "research_submit_returns_run_id",
        "sse_stream_emits_events",
        "run_completes_schema_valid",
        "citations_resolve_to_findings",
        "trace_returns_steps",
        "ssrf_guard_blocks_loopback_fixture",
        "rate_limit_429_retry_after",
        "oversize_input_rejected",
        "security_headers",
    ):
        assert by_name[name]["status"] == "PASS", by_name[name]
    # fakeredis is not reachable over TCP, so the kill-switch toggle is honestly reported as skipped
    assert by_name["kill_switch_refuses_new_runs"]["status"] == "SKIP"
    assert summary["failed"] == 0 and summary["total"] == len(smoke.CHECKS)


def test_smoke_citation_check_has_teeth() -> None:
    smoke = _load_smoke()
    ctx = smoke.Ctx(base="http://x", key="k", timeout=1, args=type("A", (), {"min_citations": 1})())
    finding = {"id": "F1", "url": "https://a.example/x", "snippet": "s"}
    good = {"number": 1, "url": "https://a.example/x", "finding_ids": ["F1"]}
    ctx.run = {
        "report": {"citations": [good], "markdown": "text [^1]", "citation_check": {"rate": 1.0}},
        "findings": [finding],
    }
    assert "resolve" in smoke.c_citations(ctx)
    for bad_report in (
        {"citations": [good | {"finding_ids": ["F9"]}], "markdown": "t [^1]", "citation_check": {"rate": 1.0}},
        {"citations": [good | {"url": "https://evil.example"}], "markdown": "t [^1]", "citation_check": {"rate": 1.0}},
        {"citations": [good], "markdown": "t [^1] [^2]", "citation_check": {"rate": 1.0}},
        {"citations": [], "markdown": "t", "citation_check": {"rate": 1.0}},
    ):
        ctx.run = {"report": bad_report, "findings": [finding]}
        with pytest.raises(smoke.Fail):
            smoke.c_citations(ctx)


def test_json_logs_carry_env_for_log_based_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    from research_assistant.logging_utils import JsonFormatter

    monkeypatch.setenv("ENV", "prod")
    rec = logging.LogRecord("research_assistant.graph", logging.INFO, __file__, 1, "run_finished x", None, None)
    entry = json.loads(JsonFormatter().format(rec))
    assert entry["env"] == "prod" and entry["message"] == "run_finished x"


def test_kill_switch_key_matches_the_app() -> None:
    assert _load_smoke().KILL_SWITCH_KEY == KILL_SWITCH_KEY
