"""Provider-agnostic LLM interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(RuntimeError):
    """Non-retryable provider failure (after retries were exhausted)."""


class LLMBadOutput(LLMError):
    """Provider rejected / could not produce output matching the schema (retryable once by caller)."""


class DeadlineExceeded(RuntimeError):
    """A retry wait would cross the run's wall-clock deadline."""


@dataclass(frozen=True)
class LLMRequest:
    model: str
    system: str
    user: str
    max_tokens: int
    json_schema: dict[str, Any] | None = None
    schema_name: str = "output"
    temperature: float = 0.0
    reasoning_effort: str = "low"

    def cache_payload(self, provider: str) -> dict[str, Any]:
        return {
            "provider": provider,
            "model": self.model,
            "system": self.system,
            "user": self.user,
            "max_tokens": self.max_tokens,
            "schema": self.json_schema,
            "temperature": self.temperature,
            "effort": self.reasoning_effort,
        }


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    usd: float = 0.0
    cached: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    provider: str

    def complete(self, req: LLMRequest, *, deadline: float | None = None) -> LLMResponse:
        """Run one completion. `deadline` is a time.monotonic() value retries must not cross."""
        ...
