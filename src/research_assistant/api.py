"""FastAPI service: POST /research, GET /research/{id}, SSE progress, GET /research/{id}/trace."""

# NOTE: no `from __future__ import annotations` here: FastAPI must resolve closure-local Depends().

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from research_assistant import migrations
from research_assistant.config import Settings
from research_assistant.control import KillSwitch
from research_assistant.graph import Runner, new_run
from research_assistant.http_security import SECURITY_HEADERS, BodySizeLimitMiddleware, SecurityHeadersMiddleware
from research_assistant.logging_utils import setup_logging
from research_assistant.schemas import RunState, utcnow
from research_assistant.store import RunStore

log = logging.getLogger(__name__)
RUN_ID = re.compile(r"^[a-f0-9]{16}$")
READ_LIMIT_PER_MIN = 240
MAX_BODY_BYTES = 16 * 1024  # a request is one question (<= 500 chars); anything bigger is rejected with 413


class ResearchRequest(BaseModel):
    question: str = Field(min_length=10, max_length=500)


class ResearchAccepted(BaseModel):
    run_id: str
    status: str


class Service:
    """Owns the run executor, the queue consumer and the orphan-resume loop, and the drain on shutdown.

    Roles (SERVICE_ROLE):
      all    - local/dev: POST executes the run in this process (no queue); resume loop recovers crashes.
      api    - HTTP only: POST enqueues on Redis; this process never runs graph code or holds LLM keys.
      worker - Cloud Run worker pool: consumes the queue + resumes orphans; serves only /healthz, /readyz.
    """

    def __init__(
        self,
        store: RunStore,
        settings: Settings,
        runner: Runner | None = None,
        kill_switch: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.settings = settings
        self.role = settings.role
        self.kill_switch: Callable[[], bool] = kill_switch or KillSwitch(settings, store)
        self.pool = ThreadPoolExecutor(max_workers=settings.max_concurrent_runs, thread_name_prefix="run")
        self.inflight: set[str] = set()
        self.futures: dict[str, Future[None]] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.draining = threading.Event()
        self.threads: list[threading.Thread] = []

    @property
    def uses_queue(self) -> bool:
        return self.role != "all"

    def submit(self, run_id: str) -> bool:
        if self.runner is None or self.draining.is_set():
            return False
        with self.lock:
            if run_id in self.inflight:
                return False
            self.inflight.add(run_id)
            self.futures[run_id] = self.pool.submit(self._run, run_id)
        return True

    def _run(self, run_id: str) -> None:
        assert self.runner is not None
        try:
            self.runner.execute(run_id)
        except Exception:
            log.exception("run %s crashed in worker", run_id)
        finally:
            with self.lock:
                self.inflight.discard(run_id)
                self.futures.pop(run_id, None)

    def has_capacity(self) -> bool:
        with self.lock:
            return len(self.inflight) < self.settings.max_concurrent_runs

    def consume_loop(self) -> None:
        """Worker role: pop run ids from the Redis queue while there is a free slot."""
        while not self.stop.is_set():
            try:
                if self.kill_switch() or not self.has_capacity():
                    self.stop.wait(1.0)
                    continue
                rid = self.store.pop_queued(timeout_s=2)
                if rid is None:
                    continue
                if not self.submit(rid) and self.draining.is_set():
                    self.store.requeue(rid)  # popped during shutdown: give it back
            except Exception:
                log.exception("queue consumer error")
                self.stop.wait(2.0)

    def resume_candidates(self) -> list[str]:
        out: list[str] = []
        for rid in self.store.active_runs():
            if rid in self.inflight or self.store.has_lease(rid):
                continue
            if self.uses_queue:
                st = self.store.load(rid)
                if st is None:
                    continue
                age = (utcnow() - st.updated_at).total_seconds()
                if st.status == "queued" and age < self.settings.orphan_grace_s:
                    continue  # waiting in the queue, not orphaned (yet)
            out.append(rid)
        return out

    def resume_loop(self, interval_s: float = 3.0) -> None:
        """Pick up unfinished runs (e.g. after a crash) once their previous owner's lease expires."""
        while not self.stop.is_set():
            try:
                if not self.kill_switch():
                    for rid in self.resume_candidates():
                        log.info("resuming orphaned run %s", rid)
                        self.submit(rid)
            except Exception:
                log.exception("resume loop error")
            self.stop.wait(interval_s)

    def start(self) -> None:
        if self.runner is None:
            return
        loops: list[Callable[[], None]] = [self.resume_loop]
        if self.role == "worker":
            loops.append(self.consume_loop)
        for fn in loops:
            t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
            t.start()
            self.threads.append(t)

    def shutdown(self, grace_s: float | None = None) -> list[str]:
        """Graceful drain (SIGTERM): stop taking work, checkpoint and hand in-flight runs to another instance.

        Runs stop at their next node boundary or LLM/tool call (Preempted), keep their last committed
        checkpoint, release their lease and go back on the queue. Runs still inside a long call when the
        grace window ends keep their lease until the process dies; it then expires (RUN_LEASE_TTL_S) and
        another instance's resume loop continues them from the last checkpoint. Returns those run ids.
        """
        grace = self.settings.shutdown_grace_s if grace_s is None else grace_s
        self.draining.set()
        self.stop.set()
        if self.runner is not None:
            self.runner.draining.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        with self.lock:
            never_started = [rid for rid, fut in self.futures.items() if fut.cancelled()]
            for rid in never_started:
                self.inflight.discard(rid)
                self.futures.pop(rid, None)
        for rid in never_started:
            if self.uses_queue:
                self.store.requeue(rid)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            with self.lock:
                if not self.inflight:
                    break
            time.sleep(0.05)
        with self.lock:
            stuck = sorted(self.inflight)
        if stuck:
            log.warning(
                "shutdown: %d run(s) still inside a call after %.1fs; their leases expire in <= %ds and another "
                "instance resumes them from the last checkpoint: %s",
                len(stuck),
                grace,
                self.settings.lease_ttl_s,
                stuck,
            )
        else:
            log.info("shutdown: drained cleanly (%d never-started run(s) requeued)", len(never_started))
        return stuck


def key_id(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def create_app(
    runner: Runner | None = None,
    settings: Settings | None = None,
    start_workers: bool = True,
    store: RunStore | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging()
        if runner is not None:
            svc = Service(runner.store, settings, runner=runner)
        elif settings.role == "api":
            svc = Service(store or RunStore.from_settings(settings), settings)  # no LLM client, no keys
        else:
            from research_assistant.wiring import build_runner

            r = build_runner(settings, store)
            svc = Service(r.store, settings, runner=r)
        app.state.service = svc
        if start_workers:
            svc.start()
        log.info("service started: role=%s kill_switch=%s", settings.role, svc.kill_switch())
        yield
        svc.shutdown()  # uvicorn runs this after SIGTERM, once HTTP connections are closed

    # No interactive docs: this is a JSON API behind an API key, and the CSP below forbids their HTML/JS anyway.
    app = FastAPI(
        title="tracedresearch", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Never leak a stack trace or exception text to the client; it goes to the (redacted) log only."""
        log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        # This handler runs in Starlette's outermost ServerErrorMiddleware, outside SecurityHeadersMiddleware.
        return JSONResponse({"detail": "internal server error"}, status_code=500, headers=SECURITY_HEADERS)

    def service(request: Request) -> Service:
        svc: Service = request.app.state.service
        return svc

    def auth(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> str:
        if not settings.api_keys:
            raise HTTPException(503, "API keys not configured (set RESEARCH_API_KEYS)")
        if not x_api_key or not any(hmac.compare_digest(x_api_key, k) for k in settings.api_keys):
            raise HTTPException(401, "invalid or missing X-API-Key")
        kid = key_id(x_api_key)
        svc: Service = request.app.state.service
        window = int(time.time() // 60)
        is_write = request.method == "POST"
        limit = settings.rate_limit_per_min if is_write else READ_LIMIT_PER_MIN
        bucket = f"{kid}:{'w' if is_write else 'r'}"
        if not svc.store.hit_rate_limit(bucket, window, limit):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(60 - int(time.time()) % 60)})
        return kid

    def load_owned(svc: Service, run_id: str, kid: str) -> RunState:
        if not RUN_ID.match(run_id):
            raise HTTPException(404, "run not found")
        st = svc.store.load(run_id)
        if st is None or not hmac.compare_digest(st.owner, kid):
            raise HTTPException(404, "run not found")
        return st

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """Liveness: the process is up and serving. No dependency checks (a Redis blip must not restart us)."""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(svc: Annotated[Service, Depends(service)]) -> dict[str, Any]:
        """Readiness: Redis reachable, keyspace migrated, not draining. Used by startup probe + deploy smoke."""
        if svc.draining.is_set():
            raise HTTPException(503, "draining")
        try:
            svc.store.r.ping()
            version = migrations.current_version(svc.store.r)
        except Exception:  # noqa: BLE001
            raise HTTPException(503, "redis unavailable") from None
        if version < migrations.REQUIRED_VERSION:
            raise HTTPException(503, f"schema version {version} < {migrations.REQUIRED_VERSION}: run migrate")
        return {
            "status": "ready",
            "role": svc.role,
            "schema_version": version,
            "kill_switch": svc.kill_switch(),
        }

    if settings.role == "worker":
        return app  # the worker pool exposes no API surface, only probes

    @app.post("/research", status_code=202, response_model=ResearchAccepted)
    def create_research(
        body: ResearchRequest, svc: Annotated[Service, Depends(service)], kid: Annotated[str, Depends(auth)]
    ) -> ResearchAccepted:
        if svc.kill_switch():
            raise HTTPException(503, "research is temporarily disabled (kill switch)", headers={"Retry-After": "300"})
        rid = new_run(svc.store, body.question.strip(), owner=kid, enqueue=svc.uses_queue)
        if not svc.uses_queue:
            svc.submit(rid)
        return ResearchAccepted(run_id=rid, status="queued")

    @app.get("/research/{run_id}")
    def get_research(
        run_id: str, svc: Annotated[Service, Depends(service)], kid: Annotated[str, Depends(auth)]
    ) -> dict[str, Any]:
        st = load_owned(svc, run_id, kid)
        rep = st.report
        return {
            "run_id": st.run_id,
            "status": st.status,
            "question": st.question,
            "error": st.error,
            "progress": {
                "step": st.step,
                "sub_questions": {
                    k: {
                        "status": v.status,
                        "attempts": v.attempts,
                        "searches": v.searches_used,
                        "send_backs": v.send_backs,
                        "reason": v.reason,
                    }
                    for k, v in st.subqs.items()
                },
                "findings": len(st.findings),
                "tokens_used": st.tokens_used,
                "usd_spent": round(st.usd_spent, 6),
                "searches_used": st.searches_used,
                "elapsed_s": round(st.elapsed_s, 2),
                "budget_tripped": st.budget_tripped,
            },
            "report": None
            if rep is None
            else {
                "title": rep.title,
                "markdown": rep.markdown,
                "citations": [c.model_dump() for c in rep.citations],
                "citation_check": rep.final_check.model_dump() | {"rate": rep.final_check.rate},
                "first_draft_citation_check": None
                if rep.first_draft_check is None
                else rep.first_draft_check.model_dump() | {"rate": rep.first_draft_check.rate},
                "fallback": rep.fallback,
                "degraded": rep.fallback,
                "fallback_reason": rep.fallback_reason if rep.fallback else None,
            },
            "findings": [f.model_dump(mode="json") for f in st.findings] if rep is not None else [],
        }

    @app.get("/research/{run_id}/trace")
    def get_trace(
        run_id: str, svc: Annotated[Service, Depends(service)], kid: Annotated[str, Depends(auth)]
    ) -> dict[str, Any]:
        st = load_owned(svc, run_id, kid)
        return {"run_id": st.run_id, "status": st.status, "steps": svc.store.trace(run_id)}

    @app.get("/research/{run_id}/events")
    def stream_events(
        run_id: str,
        svc: Annotated[Service, Depends(service)],
        kid: Annotated[str, Depends(auth)],
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        load_owned(svc, run_id, kid)
        start = last_event_id if last_event_id and re.match(r"^\d+-\d+$", last_event_id) else "0"

        def gen() -> Iterator[str]:
            cursor = start
            idle_deadline = time.monotonic() + 600
            while time.monotonic() < idle_deadline and not svc.draining.is_set():
                batch = svc.store.events(run_id, cursor, block_ms=5000)
                if not batch:
                    yield ": keep-alive\n\n"
                    continue
                for eid, ev in batch:
                    cursor = eid
                    yield f"id: {eid}\nevent: {ev.get('type', 'message')}\ndata: {json.dumps(ev)}\n\n"
                    if ev.get("type") in ("completed", "failed"):
                        return

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def app_factory() -> FastAPI:
    return create_app()
