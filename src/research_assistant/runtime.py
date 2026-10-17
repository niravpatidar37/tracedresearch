"""Per-node agent context: the single choke point where token / USD / wall-clock budgets are enforced."""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from research_assistant.config import Budgets, Models
from research_assistant.llm.base import (
    DeadlineExceeded,
    LLMBadOutput,
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    RateLimited,
)
from research_assistant.llm.structured import extract_json, strict_schema
from research_assistant.schemas import LLMCall, ToolCall
from research_assistant.spend import BudgetExceeded, cost_usd, estimate_tokens

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(LLMError):
    """The model failed to produce schema-valid output after one repair attempt."""


@dataclass
class AgentContext:
    llm: LLMClient
    models: Models
    budgets: Budgets
    tokens_used: int
    usd_spent: float
    deadline: float  # time.monotonic() value
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    llm_calls: list[LLMCall] = field(default_factory=list)
    failover: dict[str, str] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)

    def remaining_s(self) -> float:
        return self.deadline - self.clock()

    def check_deadline(self) -> None:
        if self.clock() > self.deadline:
            raise BudgetExceeded("wall_clock", "wall-clock ceiling reached")

    def _guard(self, model: str, system: str, user: str, max_tokens: int, token_limit: int) -> None:
        self.check_deadline()
        est_in = estimate_tokens(system + user)
        if self.tokens_used + est_in + max_tokens > token_limit:
            raise BudgetExceeded(
                "tokens",
                f"token budget: used {self.tokens_used} + worst case {est_in + max_tokens} > limit {token_limit}",
            )
        est_usd = cost_usd(self.llm.provider, model, est_in, max_tokens)
        if self.usd_spent + est_usd > self.budgets.max_run_usd:
            raise BudgetExceeded(
                "run_usd",
                f"run USD budget: spent {self.usd_spent:.5f} + worst case {est_usd:.5f} > {self.budgets.max_run_usd}",
            )

    def _active_model(self, model: str) -> str:
        return self.failover.get(model, model)

    def _complete(self, req: LLMRequest, role: str, fallback_model: str | None) -> LLMResponse:
        """One call with rate-limit handling: fail over to `fallback_model`, or wait if the deadline allows."""
        try:
            return self.llm.complete(req, deadline=self.deadline)
        except RateLimited as exc:
            self.llm_calls.append(
                LLMCall(
                    role=role,
                    model=req.model,
                    provider=self.llm.provider,
                    input_tokens=0,
                    output_tokens=0,
                    usd=0.0,
                    cached=False,
                    latency_ms=0.0,
                    ok=False,
                    error=f"rate_limited {exc.retry_after:.0f}s",
                )
            )
            if fallback_model and fallback_model != req.model:
                self.failover[req.model] = fallback_model  # stick to the fallback for the rest of this node
                req = dataclasses.replace(req, model=fallback_model)
                try:
                    return self.llm.complete(req, deadline=self.deadline)
                except RateLimited as exc2:
                    exc = exc2  # the fallback is limited too: wait for it if the deadline allows
            if self.clock() + exc.retry_after > self.deadline:
                raise BudgetExceeded("wall_clock", f"rate limited for {exc.retry_after:.0f}s past deadline") from exc
            self.sleep(exc.retry_after)
            return self.llm.complete(req, deadline=self.deadline)

    def structured(
        self,
        *,
        role: str,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int,
        token_limit: int | None = None,
        fallback_model: str | None = None,
    ) -> T:
        limit = token_limit if token_limit is not None else self.budgets.max_total_tokens
        json_schema = strict_schema(schema.model_json_schema())
        prompt = user
        last_error = ""
        strict = True
        for attempt in range(2):
            if attempt == 1:
                prompt = (
                    user + "\n\nYour previous output was invalid (" + last_error[:300] + "). "
                    "Return only JSON that matches the schema."
                )
            self._guard(model, system, prompt, max_tokens, limit)
            req = LLMRequest(
                model=self._active_model(model),
                system=system
                if strict
                else system + "\nJSON Schema of the required output:\n" + json.dumps(json_schema),
                strict=strict,
                user=prompt,
                max_tokens=max_tokens,
                json_schema=json_schema,
                schema_name=schema.__name__,
            )
            try:
                resp = self._complete(req, role, fallback_model)
            except DeadlineExceeded as exc:
                raise BudgetExceeded("wall_clock", str(exc)) from exc
            except LLMBadOutput as exc:
                # Provider-side strict-schema rejection: retry once in plain JSON mode and let Pydantic
                # (which tolerates omitted optional keys) validate. Count the worst case for budgets.
                strict = False
                self.tokens_used += estimate_tokens(system + prompt)
                self.llm_calls.append(
                    LLMCall(
                        role=role,
                        model=self._active_model(model),
                        provider=self.llm.provider,
                        input_tokens=estimate_tokens(system + prompt),
                        output_tokens=0,
                        usd=0.0,
                        cached=False,
                        latency_ms=0.0,
                        ok=False,
                        error=str(exc)[:200],
                    )
                )
                last_error = str(exc)
                continue
            self.tokens_used += resp.input_tokens + resp.output_tokens
            self.usd_spent += resp.usd
            call = LLMCall(
                role=role,
                model=resp.model,
                provider=resp.provider,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                usd=resp.usd,
                cached=resp.cached,
                latency_ms=resp.latency_ms,
            )
            try:
                parsed = schema.model_validate(extract_json(resp.text))
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                call.ok = False
                call.error = type(exc).__name__
                self.llm_calls.append(call)
                last_error = str(exc)
                continue
            self.llm_calls.append(call)
            return parsed
        raise StructuredOutputError(f"{role}: no valid {schema.__name__} after repair: {last_error[:200]}")
