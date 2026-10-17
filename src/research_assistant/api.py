"""FastAPI service: POST /research, GET /research/{id}, SSE progress, GET /research/{id}/trace."""

# NOTE: no `from __future__ import annotations` here: FastAPI must resolve closure-local Depends().

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from research_assistant.config import Settings
from research_assistant.graph import Runner
from research_assistant.logging_utils import setup_logging
from research_assistant.schemas import RunState

log = logging.getLogger(__name__)
RUN_ID = re.compile(r"^[a-f0-9]{16}$")
READ_LIMIT_PER_MIN = 240


class ResearchRequest(BaseModel):
    question: str = Field(min_length=10, max_length=500)


class ResearchAccepted(BaseModel):
    run_id: str
    status: str


class Service:
    """Owns the worker pool and the orphan-resume loop."""

    def __init__(self, runner: Runner, settings: Settings) -> None:
        self.runner = runner
        self.settings = settings
        self.pool = ThreadPoolExecutor(max_workers=settings.max_concurrent_runs, thread_name_prefix="run")
        self.inflight: set[str] = set()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.resumer: threading.Thread | None = None

    def submit(self, run_id: str) -> None:
        with self.lock:
            if run_id in self.inflight:
                return
            self.inflight.add(run_id)
        self.pool.submit(self._run, run_id)

    def _run(self, run_id: str) -> None:
        try:
            self.runner.execute(run_id)
        except Exception:
            log.exception("run %s crashed in worker", run_id)
        finally:
            with self.lock:
                self.inflight.discard(run_id)

    def resume_loop(self, interval_s: float = 3.0) -> None:
        """Pick up unfinished runs (e.g. after a crash) once their previous owner's lease expires."""
        while not self.stop.is_set():
            try:
                for rid in self.runner.store.active_runs():
                    if rid not in self.inflight and self.runner.store.r.get(self.runner.store.k(rid, "lease")) is None:
                        log.info("resuming orphaned run %s", rid)
                        self.submit(rid)
            except Exception:
                log.exception("resume loop error")
            self.stop.wait(interval_s)

    def start(self) -> None:
        self.resumer = threading.Thread(target=self.resume_loop, daemon=True, name="resumer")
        self.resumer.start()

    def shutdown(self) -> None:
        self.stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)


def key_id(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def create_app(runner: Runner | None = None, settings: Settings | None = None, start_workers: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging()
        if runner is None:
            from research_assistant.wiring import build_runner

            app.state.service = Service(build_runner(settings), settings)
        else:
            app.state.service = Service(runner, settings)
        if start_workers:
            app.state.service.start()
        yield
        app.state.service.shutdown()

    app = FastAPI(title="Multi-Agent Research Assistant", version="0.1.0", lifespan=lifespan)

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
        if not svc.runner.store.hit_rate_limit(bucket, window, limit):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(60 - int(time.time()) % 60)})
        return kid

    def load_owned(svc: Service, run_id: str, kid: str) -> RunState:
        if not RUN_ID.match(run_id):
            raise HTTPException(404, "run not found")
        st = svc.runner.store.load(run_id)
        if st is None or not hmac.compare_digest(st.owner, kid):
            raise HTTPException(404, "run not found")
        return st

    @app.get("/healthz")
    def healthz(svc: Annotated[Service, Depends(service)]) -> dict[str, Any]:
        try:
            ok = bool(svc.runner.store.r.ping())
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            raise HTTPException(503, "redis unavailable")
        return {"status": "ok"}

    @app.post("/research", status_code=202, response_model=ResearchAccepted)
    def create_research(
        body: ResearchRequest, svc: Annotated[Service, Depends(service)], kid: Annotated[str, Depends(auth)]
    ) -> ResearchAccepted:
        rid = svc.runner.create_run(body.question.strip(), owner=kid)
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
            },
            "findings": [f.model_dump(mode="json") for f in st.findings] if rep is not None else [],
        }

    @app.get("/research/{run_id}/trace")
    def get_trace(
        run_id: str, svc: Annotated[Service, Depends(service)], kid: Annotated[str, Depends(auth)]
    ) -> dict[str, Any]:
        st = load_owned(svc, run_id, kid)
        return {"run_id": st.run_id, "status": st.status, "steps": svc.runner.store.trace(run_id)}

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
            while time.monotonic() < idle_deadline:
                batch = svc.runner.store.events(run_id, cursor, block_ms=5000)
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
