#!/usr/bin/env python3
"""End-to-end smoke test for a running tracedresearch API (stdlib only, no repo imports).

    python scripts/smoke.py --base-url http://127.0.0.1:8102 --json-out smoke.json
    (API key read from the env var named by --api-key-env, default SMOKE_API_KEY; it is never printed)

Checks: liveness, readiness (Redis + schema really checked by /readyz), optional worker readiness, auth rejected
without / with a wrong key and accepted with the right one, input validation (422), oversize bodies (413),
security headers and no stack traces in error bodies, then the product path: POST /research returns a run_id,
the SSE stream emits events, the run completes, every citation resolves to a Finding, GET /trace returns steps,
the SSRF guard blocked the loopback/metadata pages of the smoke fixture corpus (--expect-ssrf-fixture), the kill
switch refuses new runs (--kill-switch-redis-url), and the per-key rate limit returns 429 with Retry-After.

Prints one PASS/FAIL/SKIP line per check and exits 1 if any check failed. Used by CI (compose stack with
LLM_PROVIDER=fake), by deploy.yml after the staging deploy and the prod canary, and locally (results/).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

RUN_ID = re.compile(r"^[a-f0-9]{16}$")
MARKER = re.compile(r"(?<!\\)\[\^(\d+)\](?!:)")
TRACE_HINTS = ("Traceback (most recent call last)", 'File "', "site-packages", '.py", line')
REQUIRED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy": "default-src 'none'",
    "referrer-policy": "no-referrer",
}
KILL_SWITCH_KEY = "ra:control:kill_switch"
LOOPBACK_OR_LINK_LOCAL = re.compile(r"^(127\.|169\.254\.|localhost$|0\.0\.0\.0$|\[?::1\]?$)")


class Fail(Exception):
    pass


class Skip(Exception):
    pass


@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except ValueError as exc:
            raise Fail(f"HTTP {self.status}: body is not JSON: {self.body[:120]!r}") from exc


@dataclass
class Check:
    name: str
    status: str
    detail: str
    ms: float


@dataclass
class Ctx:
    base: str
    key: str
    timeout: float
    args: argparse.Namespace
    run_id: str = ""
    run: dict[str, Any] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)
    error_bodies: list[tuple[str, Resp]] = field(default_factory=list)

    def req(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        key: str | None = "",
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        base: str | None = None,
    ) -> Resp:
        h = {"accept": "application/json", "user-agent": "tracedresearch-smoke/1"}
        if body is not None:
            h["content-type"] = "application/json"
        k = self.key if key == "" else key
        if k:
            h["x-api-key"] = k
        h.update(headers or {})
        url = (base or self.base) + path
        rq = urllib.request.Request(url, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(rq, timeout=timeout or self.timeout) as r:
                return Resp(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:
            return Resp(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())

    def post_question(self, question: str, key: str | None = "") -> Resp:
        return self.req("POST", "/research", body=json.dumps({"question": question}).encode(), key=key)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise Fail(msg)


# ----------------------------------------------------------------------------------------------- checks
def c_liveness(c: Ctx) -> str:
    r = c.req("GET", "/healthz", key=None)
    expect(r.status == 200 and r.json() == {"status": "ok"}, f"GET /healthz -> {r.status} {r.body[:80]!r}")
    return "200 {status: ok}"


def c_readiness(c: Ctx) -> str:
    deadline = time.monotonic() + c.args.wait_ready
    last = ""
    while True:
        try:
            r = c.req("GET", "/readyz", key=None)
            if r.status == 200:
                body = r.json()
                expect(body.get("status") == "ready", f"/readyz body {body}")
                expect(isinstance(body.get("schema_version"), int) and body["schema_version"] >= 1, "schema<1")
                expect(isinstance(body.get("kill_switch"), bool), "kill_switch flag missing")
                return f"ready role={body.get('role')} schema_version={body['schema_version']} (Redis pinged)"
            last = f"{r.status} {r.body[:100]!r}"
        except (OSError, Fail) as exc:
            last = str(exc)
        if time.monotonic() > deadline:
            raise Fail(f"/readyz not ready after {c.args.wait_ready}s: {last}")
        time.sleep(2)


def c_worker_readiness(c: Ctx) -> str:
    if not c.args.worker_url:
        raise Skip("no --worker-url (worker has no public URL in Cloud Run)")
    base = c.args.worker_url.rstrip("/")
    deadline = time.monotonic() + c.args.wait_ready
    while True:
        try:
            r = c.req("GET", "/readyz", key=None, base=base)
            if r.status == 200 and r.json().get("role") == "worker":
                post = c.req("POST", "/research", body=b"{}", base=base)
                expect(post.status == 404, f"worker exposes POST /research ({post.status})")
                return "worker ready; exposes probes only (POST /research -> 404)"
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise Fail("worker /readyz not ready")
        time.sleep(2)


def c_auth_rejected(c: Ctx) -> str:
    q = "How efficient are silicon solar panels today?"
    r1 = c.post_question(q, key=None)
    r2 = c.post_question(q, key="wrong-key-" + "x" * 20)
    r3 = c.req("GET", "/research/0123456789abcdef", key=None)
    for name, r in (("POST no key", r1), ("POST wrong key", r2), ("GET no key", r3)):
        expect(r.status == 401, f"{name} -> {r.status}, want 401")
        c.error_bodies.append((name, r))
    return "401 without a key and with a wrong key (POST and GET)"


def c_auth_accepted(c: Ctx) -> str:
    if not c.key:
        raise Fail("no API key: set the env var named by --api-key-env")
    r = c.req("GET", "/research/0123456789abcdef")
    expect(r.status == 404, f"GET unknown run with a valid key -> {r.status}, want 404 (authenticated)")
    c.error_bodies.append(("GET unknown run", r))
    return "valid key authenticated (unknown run -> 404, not 401)"


def c_input_validation(c: Ctx) -> str:
    cases = {
        "question too short": json.dumps({"question": "short"}).encode(),
        "missing field": b"{}",
        "wrong type": json.dumps({"question": 123456789012}).encode(),
        "malformed JSON": b'{"question": ',
    }
    for name, body in cases.items():
        r = c.req("POST", "/research", body=body)
        expect(r.status == 422, f"{name} -> {r.status}, want 422")
        c.error_bodies.append((name, r))
    return f"422 for {len(cases)} invalid bodies"


def c_oversize(c: Ctx) -> str:
    r1 = c.post_question("x" * 501)
    expect(r1.status == 422, f"501-char question -> {r1.status}, want 422")
    big = json.dumps({"question": "y" * 200_000}).encode()
    r2 = c.req("POST", "/research", body=big)
    expect(r2.status == 413, f"200 KB body -> {r2.status}, want 413")
    c.error_bodies.append(("oversize body", r2))
    return "501-char question -> 422; 200 KB body -> 413"


def c_security_headers(c: Ctx) -> str:
    probes = [("GET /healthz", c.req("GET", "/healthz", key=None)), *c.error_bodies[:2]]
    for name, r in probes:
        for h, want in REQUIRED_HEADERS.items():
            got = r.headers.get(h, "")
            expect(want in got, f"{name}: header {h}={got!r}, want {want!r}")
        server = r.headers.get("server", "").lower()
        expect("uvicorn" not in server and "python" not in server, f"{name}: server header leaks {server!r}")
    return f"{len(REQUIRED_HEADERS)} headers on {len(probes)} responses; no framework server banner"


def c_no_stack_traces(c: Ctx) -> str:
    probes = [*c.error_bodies, ("unknown path", c.req("GET", "/definitely-not-a-route", key=None))]
    for name, r in probes:
        text = r.body.decode("utf-8", "replace")
        expect(not any(h in text for h in TRACE_HINTS), f"{name}: error body leaks internals: {text[:160]!r}")
        expect(isinstance(r.json(), dict), f"{name}: error body is not a JSON object")
    return f"{len(probes)} error responses are JSON without stack traces"


def c_submit(c: Ctx) -> str:
    r = c.post_question(c.args.question)
    expect(r.status == 202, f"POST /research -> {r.status} {r.body[:120]!r}")
    body = r.json()
    expect(set(body) == {"run_id", "status"}, f"unexpected keys {sorted(body)}")
    expect(bool(RUN_ID.match(str(body["run_id"]))), f"run_id {body['run_id']!r} is not 16 hex chars")
    expect(body["status"] == "queued", f"status {body['status']!r}")
    c.run_id = body["run_id"]
    return f"202 run_id={c.run_id}"


def _need_run(c: Ctx) -> None:
    if not c.run_id:
        raise Fail("no run_id (submit failed)")


def c_sse(c: Ctx) -> str:
    _need_run(c)
    url = f"{c.base}/research/{c.run_id}/events"
    rq = urllib.request.Request(url, headers={"x-api-key": c.key, "accept": "text/event-stream"})
    wait_completion = not c.args.no_run_completion
    deadline = time.monotonic() + (c.args.run_timeout if wait_completion else 15)
    events: list[str] = []
    try:
        with urllib.request.urlopen(rq, timeout=10) as r:
            expect(r.headers.get("content-type", "").startswith("text/event-stream"), "not an event stream")
            ev_type, has_id = "", False
            while time.monotonic() < deadline:
                line = r.readline().decode("utf-8").rstrip("\n")
                if line.startswith("id: "):
                    has_id = True
                elif line.startswith("event: "):
                    ev_type = line[7:]
                elif line.startswith("data: "):
                    json.loads(line[6:])  # every data line is JSON
                elif line == "" and ev_type:
                    expect(has_id, "event without id (resume via Last-Event-ID impossible)")
                    events.append(ev_type)
                    if ev_type in ("completed", "failed") or not wait_completion:
                        break
                    ev_type, has_id = "", False
    except TimeoutError:
        pass
    expect(bool(events), "no SSE events received")
    if wait_completion:
        expect(events[-1] in ("completed", "failed"), f"stream ended without a terminal event: {events[-5:]}")
    return f"{len(events)} events: {', '.join(events[:3])} ... {events[-1]}"


def c_run_completes(c: Ctx) -> str:
    _need_run(c)
    if c.args.no_run_completion:
        r = c.req("GET", f"/research/{c.run_id}")
        expect(r.status == 200 and r.json().get("status") in ("queued", "running", "completed"), "bad status")
        raise Skip(f"--no-run-completion (status now {r.json().get('status')})")
    deadline = time.monotonic() + c.args.run_timeout
    while True:
        r = c.req("GET", f"/research/{c.run_id}")
        expect(r.status == 200, f"GET /research/id -> {r.status}")
        body = r.json()
        if body.get("status") in ("completed", "failed"):
            break
        if time.monotonic() > deadline:
            raise Fail(f"run not finished after {c.args.run_timeout}s (status {body.get('status')})")
        time.sleep(1)
    expect(body["status"] == "completed", f"run failed: {body.get('error')!r}")
    for k in ("run_id", "status", "question", "error", "progress", "report", "findings"):
        expect(k in body, f"response missing {k!r}")
    rep = body["report"]
    for k in ("title", "markdown", "citations", "citation_check", "fallback", "degraded"):
        expect(isinstance(rep, dict) and k in rep, f"report missing {k!r}")
    c.run = body
    p = body["progress"]
    return (
        f"completed in {p.get('elapsed_s')}s, findings={len(body['findings'])}, tokens={p.get('tokens_used')}, "
        f"degraded={rep['degraded']}" + (f" ({rep.get('fallback_reason')})" if rep["degraded"] else "")
    )


def c_citations(c: Ctx) -> str:
    if not c.run:
        raise Skip("run did not complete in this smoke run")
    rep, findings = c.run["report"], {f["id"]: f for f in c.run["findings"]}
    cites = {int(x["number"]): x for x in rep["citations"]}
    expect(len(cites) >= c.args.min_citations, f"{len(cites)} citations < --min-citations {c.args.min_citations}")
    for n, ci in cites.items():
        expect(bool(ci["finding_ids"]), f"citation [{n}] has no finding ids")
        for fid in ci["finding_ids"]:
            expect(fid in findings, f"citation [{n}] -> unknown finding {fid}")
            expect(findings[fid]["url"] == ci["url"], f"citation [{n}] url != finding {fid} url")
            expect(bool(findings[fid].get("snippet")), f"finding {fid} has no snippet")
    markers = [int(m) for m in MARKER.findall(rep["markdown"])]
    unknown = sorted({m for m in markers if m not in cites})
    expect(not unknown, f"markdown markers without a citation: {unknown}")
    expect(rep["citation_check"]["rate"] == 1.0, f"citation_check.rate={rep['citation_check']['rate']}")
    return f"{len(markers)} markers -> {len(cites)} sources -> all resolve to Findings (rate 1.0)"


def c_trace(c: Ctx) -> str:
    _need_run(c)
    r = c.req("GET", f"/research/{c.run_id}/trace")
    expect(r.status == 200, f"GET trace -> {r.status}")
    body = r.json()
    steps = body.get("steps")
    expect(isinstance(steps, list), "trace has no steps list")
    if not c.run and not steps:
        return "steps endpoint OK; 0 steps so far (run not awaited)"
    expect(len(steps) > 0, "no trace steps")
    for s in steps:
        for k in ("agent", "tool_calls", "llm_calls", "tokens", "latency_ms"):
            expect(k in s, f"trace step missing {k!r}")
    agents = {s["agent"] for s in steps}
    if c.run:
        expect({"planner", "writer"} <= agents, f"agents {sorted(agents)} lack planner/writer")
    c.trace = steps
    return f"{len(steps)} steps; agents: {', '.join(sorted(agents))}"


def c_ssrf_fixture(c: Ctx) -> str:
    if not c.args.expect_ssrf_fixture:
        raise Skip("not running on the smoke fixture corpus (--expect-ssrf-fixture)")
    expect(bool(c.trace), "no trace")
    fetches = [t for s in c.trace for t in s.get("tool_calls", []) if t.get("tool") == "fetch"]
    internal = [t for t in fetches if LOOPBACK_OR_LINK_LOCAL.match(urllib.parse.urlsplit(t["input"]).hostname or "")]
    expect(bool(internal), f"the fixture's loopback/metadata pages were never fetched ({len(fetches)} fetches)")
    leaked = [t["input"] for t in internal if t.get("status") != "blocked"]
    expect(not leaked, f"internal URLs NOT blocked: {leaked}")
    loop = [t for t in internal if (urllib.parse.urlsplit(t["input"]).hostname or "").startswith("127.")]
    expect(bool(loop), "no loopback fetch attempt in the trace")
    if c.run:
        urls = [f["url"] for f in c.run["findings"]] + [x["url"] for x in c.run["report"]["citations"]]
        bad = [u for u in urls if LOOPBACK_OR_LINK_LOCAL.match(urllib.parse.urlsplit(u).hostname or "")]
        expect(not bad, f"internal URLs cited: {bad}")
    reasons = sorted({t.get("detail", "") for t in internal})
    return f"{len(internal)} internal fetches blocked ({', '.join(reasons)}); none cited"


def _redis(url: str, *cmd: str) -> bytes:
    """Minimal RESP client (no TLS): enough for SET/DEL on a local/CI Redis."""
    p = urllib.parse.urlsplit(url)
    with socket.create_connection((p.hostname or "127.0.0.1", p.port or 6379), timeout=5) as s:
        f = s.makefile("rb")
        pre: list[tuple[str, ...]] = []
        if p.password:
            pre.append(("AUTH", urllib.parse.unquote(p.password)))
        db = (p.path or "/0").strip("/") or "0"
        pre.append(("SELECT", db))
        reply = b""
        for c in [*pre, cmd]:
            out = f"*{len(c)}\r\n" + "".join(f"${len(a.encode())}\r\n{a}\r\n" for a in c)
            s.sendall(out.encode())
            reply = f.readline()
            if reply.startswith(b"-"):
                raise Fail(f"redis {c[0]}: {reply.strip().decode()}")
        return reply


def _wait_kill_switch(c: Ctx, want: bool, limit: float = 10.0) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        if c.req("GET", "/readyz", key=None).json().get("kill_switch") is want:
            return time.monotonic() - t0
        time.sleep(0.2)
    raise Fail(f"/readyz kill_switch did not become {want} within {limit}s")


def c_kill_switch(c: Ctx) -> str:
    if not c.args.kill_switch_redis_url:
        r = c.req("GET", "/readyz", key=None).json()
        raise Skip(f"toggle needs --kill-switch-redis-url; /readyz reports kill_switch={r.get('kill_switch')}")
    url = c.args.kill_switch_redis_url
    _redis(url, "SET", KILL_SWITCH_KEY, "smoke-test")
    try:
        t_on = _wait_kill_switch(c, True)
        r = c.post_question(c.args.question)
        expect(r.status == 503, f"POST with kill switch on -> {r.status}, want 503")
        expect(r.headers.get("retry-after", "").isdigit(), "503 without Retry-After")
        expect(c.req("GET", "/healthz", key=None).status == 200, "liveness failed while kill switch on")
    finally:
        _redis(url, "DEL", KILL_SWITCH_KEY)
    t_off = _wait_kill_switch(c, False)
    return f"engaged in {t_on:.1f}s -> POST 503 + Retry-After; released in {t_off:.1f}s (cache <= 2 s)"


def c_rate_limit(c: Ctx) -> str:
    if c.args.skip_rate_limit:
        raise Skip("--skip-rate-limit")
    body = json.dumps({"question": "short"}).encode()  # 422 after auth: burns the bucket without creating runs
    for i in range(1, c.args.rate_limit_max + 1):
        r = c.req("POST", "/research", body=body)
        if r.status == 429:
            ra = r.headers.get("retry-after", "")
            expect(ra.isdigit() and 0 < int(ra) <= 60, f"Retry-After={ra!r}")
            return f"429 after {i} requests in this window, Retry-After={ra}s"
        expect(r.status == 422, f"request {i} -> {r.status}")
    raise Fail(f"no 429 after {c.args.rate_limit_max} POSTs")


CHECKS: list[tuple[str, Callable[[Ctx], str]]] = [
    ("liveness", c_liveness),
    ("readiness", c_readiness),
    ("worker_readiness", c_worker_readiness),
    ("auth_rejected_without_or_wrong_key", c_auth_rejected),
    ("auth_accepted_with_key", c_auth_accepted),
    ("input_validation_422", c_input_validation),
    ("oversize_input_rejected", c_oversize),
    ("security_headers", c_security_headers),
    ("no_stack_traces_in_errors", c_no_stack_traces),
    ("research_submit_returns_run_id", c_submit),
    ("sse_stream_emits_events", c_sse),
    ("run_completes_schema_valid", c_run_completes),
    ("citations_resolve_to_findings", c_citations),
    ("trace_returns_steps", c_trace),
    ("ssrf_guard_blocks_loopback_fixture", c_ssrf_fixture),
    ("kill_switch_refuses_new_runs", c_kill_switch),
    ("rate_limit_429_retry_after", c_rate_limit),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key-env", default="SMOKE_API_KEY", help="env var holding the API key (never printed)")
    ap.add_argument("--worker-url", default="", help="worker probe URL (local/compose only)")
    ap.add_argument("--json-out", default="", help="write a JSON summary here")
    ap.add_argument("--question", default="How efficient are silicon and perovskite solar cells today?")
    ap.add_argument("--run-timeout", type=float, default=45.0, help="seconds to wait for the run to complete")
    ap.add_argument("--no-run-completion", action="store_true", help="submit + SSE only; do not wait")
    ap.add_argument("--min-citations", type=int, default=1)
    ap.add_argument("--expect-ssrf-fixture", action="store_true", help="stack runs on fixtures/smoke_corpus.json")
    ap.add_argument("--kill-switch-redis-url", default="", help="redis://host:port/db to toggle the kill switch")
    ap.add_argument("--skip-rate-limit", action="store_true")
    ap.add_argument("--rate-limit-max", type=int, default=300)
    ap.add_argument("--wait-ready", type=float, default=60.0, help="seconds to wait for /readyz")
    ap.add_argument("--timeout", type=float, default=15.0, help="per-request timeout")
    args = ap.parse_args(argv)
    base = args.base_url.rstrip("/")
    if urllib.parse.urlsplit(base).scheme not in ("http", "https"):
        ap.error("--base-url must be http(s)")
    ctx = Ctx(base=base, key=os.environ.get(args.api_key_env, "").strip(), timeout=args.timeout, args=args)

    started = datetime.now(UTC)
    t0 = time.monotonic()
    results: list[Check] = []
    for name, fn in CHECKS:
        t = time.monotonic()
        try:
            status, detail = "PASS", fn(ctx)
        except Skip as exc:
            status, detail = "SKIP", str(exc)
        except Fail as exc:
            status, detail = "FAIL", str(exc)
        except Exception as exc:
            status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
        ms = (time.monotonic() - t) * 1000
        results.append(Check(name, status, detail, round(ms, 1)))
        print(f"{status:4} {name:38} {detail} ({ms:.0f} ms)", flush=True)

    counts = {s: sum(1 for r in results if r.status == s) for s in ("PASS", "FAIL", "SKIP")}
    summary = {
        "base_url": base,
        "started_at": started.isoformat(timespec="seconds"),
        "duration_s": round(time.monotonic() - t0, 2),
        "passed": counts["PASS"],
        "failed": counts["FAIL"],
        "skipped": counts["SKIP"],
        "total": len(results),
        "ok": counts["FAIL"] == 0,
        "run_id": ctx.run_id or None,
        "checks": [asdict(r) for r in results],
    }
    print(
        f"\nSMOKE {'OK' if summary['ok'] else 'FAILED'}: {counts['PASS']} passed, {counts['FAIL']} failed, "
        f"{counts['SKIP']} skipped of {len(results)} in {summary['duration_s']}s"
    )
    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(summary, fh, indent=2)
            fh.write("\n")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
