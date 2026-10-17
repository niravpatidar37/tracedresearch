"""Claude adapter. Dormant unless ANTHROPIC_API_KEY is set; always behind the spend ledger.

Model ids are NOT hard-coded: set ANTHROPIC_MODEL_* after verifying with GET https://api.anthropic.com/v1/models.
"""

from __future__ import annotations

import json
import time
from typing import Any

from research_assistant.llm.base import DeadlineExceeded, LLMError, LLMRequest, LLMResponse
from research_assistant.llm.cache import DiskCache
from research_assistant.spend import SpendLedger, cost_usd, estimate_tokens

PLACEHOLDER_MODEL = "VERIFY-VIA-GET-v1-models"


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, *, api_key: str, ledger: SpendLedger, cache: DiskCache | None) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional extra
            raise LLMError("install the 'anthropic' extra to use the Claude adapter") from exc
        self._client: Any = anthropic.Anthropic(api_key=api_key, max_retries=4, timeout=90.0)
        self.ledger = ledger
        self.cache = cache

    def complete(self, req: LLMRequest, *, deadline: float | None = None) -> LLMResponse:
        if req.model == PLACEHOLDER_MODEL:
            raise LLMError("Claude model id is a placeholder; verify via GET /v1/models and set ANTHROPIC_MODEL_*")
        if deadline is not None and time.monotonic() > deadline:
            raise DeadlineExceeded("run deadline passed before LLM call")
        key = DiskCache.key(req.cache_payload(self.provider)) if self.cache else ""
        if self.cache and (hit := self.cache.get(key)):
            return LLMResponse(
                hit["text"],
                req.model,
                self.provider,
                hit["input_tokens"],
                hit["output_tokens"],
                0.0,
                0.0,
                cached=True,
            )
        system = req.system
        if req.json_schema is not None:
            system += "\nReturn ONLY a JSON object matching this JSON Schema:\n" + json.dumps(req.json_schema)
        self.ledger.check(cost_usd(self.provider, req.model, estimate_tokens(system + req.user), req.max_tokens))
        t0 = time.perf_counter()
        msg = self._client.messages.create(
            model=req.model,
            max_tokens=req.max_tokens,
            system=system,
            temperature=req.temperature,
            messages=[{"role": "user", "content": req.user}],
        )
        text = "".join(getattr(b, "text", "") for b in msg.content)
        in_tok, out_tok = int(msg.usage.input_tokens), int(msg.usage.output_tokens)
        usd = cost_usd(self.provider, req.model, in_tok, out_tok)
        self.ledger.record(self.provider, req.model, in_tok, out_tok, usd)
        if self.cache:
            self.cache.set(key, {"text": text, "input_tokens": in_tok, "output_tokens": out_tok})
        return LLMResponse(text, req.model, self.provider, in_tok, out_tok, (time.perf_counter() - t0) * 1000, usd)
