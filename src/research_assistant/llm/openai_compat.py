"""OpenAI-compatible chat client (Groq, Ollama) with cap checks, retries and a disk cache."""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from research_assistant.llm.base import (
    DeadlineExceeded,
    LLMBadOutput,
    LLMError,
    LLMRequest,
    LLMResponse,
    RateLimited,
)
from research_assistant.llm.cache import DiskCache
from research_assistant.spend import SpendLedger, cost_usd, estimate_tokens

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class OpenAICompatClient:
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str | None,
        ledger: SpendLedger | None,
        cache: DiskCache | None,
        http: httpx.Client | None = None,
        max_retries: int = 6,
        max_backoff_s: float = 45.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        request_timeout_s: float = 90.0,
        long_wait_s: float | None = 20.0,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.ledger = ledger
        self.cache = cache
        self.http = http or httpx.Client(timeout=request_timeout_s)
        self.max_retries = max_retries
        self.max_backoff_s = max_backoff_s
        self.sleep = sleep
        self.clock = clock
        self._json_object_only: set[str] = set()
        # A 429 asking for a longer wait than this raises RateLimited so the caller can fail over to another model.
        self.long_wait_s = long_wait_s

    # -- helpers -----------------------------------------------------------------
    def _body(self, req: LLMRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "temperature": req.temperature,
            "max_completion_tokens": req.max_tokens,
        }
        if self.provider == "groq" and req.model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = req.reasoning_effort
        if req.json_schema is not None:
            if req.model in self._json_object_only or self.provider == "ollama":
                body["response_format"] = {"type": "json_object"}
            else:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": req.schema_name, "schema": req.json_schema, "strict": True},
                }
        return body

    def _wait(self, attempt: int, retry_after: float | None, deadline: float | None) -> None:
        backoff = min(self.max_backoff_s, (2**attempt) * 0.75)
        delay = (retry_after if retry_after is not None else backoff) + random.uniform(0, 0.5)  # noqa: S311
        delay = min(delay, self.max_backoff_s)
        if deadline is not None and self.clock() + delay > deadline:
            raise DeadlineExceeded(f"retry wait {delay:.1f}s would cross the run deadline")
        log.info("llm retry in %.1fs (attempt %d)", delay, attempt + 1)
        self.sleep(delay)

    # -- main --------------------------------------------------------------------
    def complete(self, req: LLMRequest, *, deadline: float | None = None) -> LLMResponse:
        key = DiskCache.key(req.cache_payload(self.provider)) if self.cache else ""
        if self.cache and (hit := self.cache.get(key)):
            return LLMResponse(
                text=hit["text"],
                model=req.model,
                provider=self.provider,
                input_tokens=int(hit["input_tokens"]),
                output_tokens=int(hit["output_tokens"]),
                latency_ms=0.0,
                usd=0.0,
                cached=True,
            )

        if self.ledger is not None:
            est_in = estimate_tokens(req.system + req.user)
            self.ledger.check(cost_usd(self.provider, req.model, est_in, req.max_tokens))

        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        url = f"{self.base_url}/chat/completions"
        last_err = "unknown"
        for attempt in range(self.max_retries + 1):
            if deadline is not None and self.clock() > deadline:
                raise DeadlineExceeded("run deadline passed before LLM call")
            t0 = time.perf_counter()
            try:
                r = self.http.post(url, json=self._body(req), headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = f"{type(exc).__name__}"
                if attempt >= self.max_retries:
                    break
                self._wait(attempt, None, deadline)
                continue
            latency_ms = (time.perf_counter() - t0) * 1000
            if r.status_code in RETRYABLE_STATUS:
                last_err = f"HTTP {r.status_code}"
                ra = _parse_retry_after(r.headers.get("retry-after"))
                if r.status_code == 429 and ra is not None and self.long_wait_s is not None and ra > self.long_wait_s:
                    raise RateLimited(ra, f"{self.provider} {req.model} rate limited; retry-after {ra:.0f}s")
                if attempt >= self.max_retries:
                    break
                self._wait(attempt, ra, deadline)
                continue
            if r.status_code == 400:
                err = _error_payload(r)
                code = str(err.get("code", ""))
                msg = str(err.get("message", ""))
                if code == "json_validate_failed":
                    raise LLMBadOutput(f"provider schema validation failed: {msg[:200]}")
                if "response_format" in msg and req.model not in self._json_object_only:
                    self._json_object_only.add(req.model)
                    continue
                raise LLMError(f"HTTP 400 from {self.provider}: {msg[:200]}")
            if r.status_code >= 400:
                raise LLMError(f"HTTP {r.status_code} from {self.provider}")
            data = r.json()
            text = data["choices"][0]["message"].get("content") or ""
            usage = data.get("usage") or {}
            in_tok = int(usage.get("prompt_tokens", 0))
            out_tok = int(usage.get("completion_tokens", 0))
            usd = cost_usd(self.provider, req.model, in_tok, out_tok)
            if self.ledger is not None and usd > 0:
                self.ledger.record(self.provider, req.model, in_tok, out_tok, usd)
            if self.cache:
                self.cache.set(key, {"text": text, "input_tokens": in_tok, "output_tokens": out_tok})
            return LLMResponse(
                text=text,
                model=req.model,
                provider=self.provider,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=latency_ms,
                usd=usd,
                extra={"attempts": attempt + 1},
            )
        raise LLMError(f"{self.provider} call failed after retries: {last_err}")


def _error_payload(r: httpx.Response) -> dict[str, Any]:
    try:
        data = r.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    err = data.get("error") if isinstance(data, dict) else None
    return err if isinstance(err, dict) else {}
