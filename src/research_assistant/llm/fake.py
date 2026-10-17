"""Deterministic fake LLM for unit tests (recorded responses keyed by schema name)."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from research_assistant.llm.base import LLMRequest, LLMResponse
from research_assistant.spend import estimate_tokens

Responder = Callable[[LLMRequest], "str | dict[str, Any]"]


class FakeLLM:
    provider = "fake"

    def __init__(
        self,
        responses: dict[str, list[str | dict[str, Any]] | Responder] | None = None,
        *,
        output_tokens: int | None = None,
    ) -> None:
        self.responses: dict[str, list[str | dict[str, Any]] | Responder] = dict(responses or {})
        self.calls: list[LLMRequest] = []
        self._idx: dict[str, int] = defaultdict(int)
        self.output_tokens = output_tokens

    def complete(self, req: LLMRequest, *, deadline: float | None = None) -> LLMResponse:
        self.calls.append(req)
        spec = self.responses.get(req.schema_name)
        if spec is None:
            raise AssertionError(f"FakeLLM has no response for schema {req.schema_name!r}")
        if callable(spec):
            out = spec(req)
        else:
            i = min(self._idx[req.schema_name], len(spec) - 1)
            self._idx[req.schema_name] += 1
            out = spec[i]
        text = out if isinstance(out, str) else json.dumps(out)
        return LLMResponse(
            text=text,
            model=req.model,
            provider=self.provider,
            input_tokens=estimate_tokens(req.system + req.user),
            output_tokens=self.output_tokens if self.output_tokens is not None else estimate_tokens(text),
            latency_ms=1.0,
            usd=0.0,
        )

    def count(self, schema_name: str) -> int:
        return sum(1 for c in self.calls if c.schema_name == schema_name)
